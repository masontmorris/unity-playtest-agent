using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Compilation;
using Assembly = System.Reflection.Assembly;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>
    /// Names the assemblies that contain authored scripts and the files the Editor loaded. The IL write
    /// scan itself runs outside Unity (<c>tools/ilscan</c>), and preflight merges its sections into
    /// discovery in place of <c>il_inputs</c>.
    /// </summary>
    public static class IlDiscoveryCollector
    {
        public const int ScriptLimit = 20000;
        public const int AssemblyLimit = 512;

        public static InspectionDocument Collect()
        {
            var document = new InspectionDocument();
            bool incomplete = false;
            var assemblyNames = new SortedSet<string>(StringComparer.Ordinal);
            List<string> paths;
            try
            {
                var found = AssetDatabase.FindAssets("t:MonoScript").Select(AssetDatabase.GUIDToAssetPath)
                    .Where(path => !string.IsNullOrEmpty(path) && ProjectSurveyCollector.IsAuthoredAsset(path))
                    .Distinct(StringComparer.Ordinal).OrderBy(path => path, StringComparer.Ordinal).ToList();
                paths = document.Take("il_script_files", found, ScriptLimit);
                incomplete = paths.Count != found.Count;
            }
            catch (Exception ex)
            {
                document.Unavailable("il_script_files", ScriptLimit, ex.GetType().Name);
                paths = new List<string>();
                incomplete = true;
            }

            foreach (string path in paths)
            {
                try
                {
                    string name = CompilationPipeline.GetAssemblyNameFromScriptPath(path);
                    if (string.IsNullOrEmpty(name)) throw new InvalidOperationException("no compilation assembly");
                    name = Path.GetFileName(name);
                    if (name.EndsWith(".dll", StringComparison.OrdinalIgnoreCase))
                        name = name.Substring(0, name.Length - 4);
                    assemblyNames.Add(name);
                }
                catch (Exception ex)
                {
                    incomplete = true;
                    document.Errors.Add("il_script:" + path + ": " + ex.GetType().Name);
                }
            }

            List<string> selectedAssemblies = document.Take("il_assemblies", assemblyNames.ToList(), AssemblyLimit);
            if (incomplete) MarkUncertain(document.Caps["il_assemblies"]);
            incomplete |= selectedAssemblies.Count != assemblyNames.Count;
            Assembly[] domain = AppDomain.CurrentDomain.GetAssemblies();
            var loaded = domain.GroupBy(a => a.GetName().Name)
                .ToDictionary(g => g.Key, g => g.First(), StringComparer.Ordinal);
            var assemblies = new List<object>();
            foreach (string name in selectedAssemblies)
            {
                if (!loaded.TryGetValue(name, out Assembly assembly))
                {
                    incomplete = true;
                    document.Errors.Add("il_assembly:" + name + ": not loaded");
                    continue;
                }

                string location = Location(assembly);
                if (location == null)
                {
                    incomplete = true;
                    document.Errors.Add("il_assembly:" + name + ": no file location");
                    continue;
                }

                assemblies.Add(new Dictionary<string, object> { ["name"] = name, ["path"] = location });
            }

            // Every loaded file, in load order, so the scan resolves a reference to the assembly the
            // Editor itself bound it to.
            var references = new List<object>();
            var listed = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (Assembly assembly in domain)
            {
                string location = Location(assembly);
                if (location != null && listed.Add(location)) references.Add(location);
            }

            document.Data["il_inputs"] = new Dictionary<string, object>
            {
                ["assemblies"] = assemblies, ["references"] = references, ["complete"] = !incomplete,
            };
            return document;
        }

        private static string Location(Assembly assembly)
        {
            try
            {
                if (assembly.IsDynamic) return null;
                string location = assembly.Location;
                return string.IsNullOrEmpty(location) ? null : Path.GetFullPath(location);
            }
            catch (Exception)
            {
                return null;
            }
        }

        private static void MarkUncertain(InspectionCap cap)
        {
            cap.Observed = null;
            if (cap.Truncated != true) cap.Truncated = null;
        }
    }
}
