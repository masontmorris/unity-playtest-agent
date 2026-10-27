using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using UnityEditor;
using UnityEditor.Compilation;
using Assembly = System.Reflection.Assembly;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>Joins direct IL stores to fields of assemblies containing authored scripts.</summary>
    public static class IlDiscoveryCollector
    {
        public const int ScriptLimit = 20000;
        public const int AssemblyLimit = 512;
        public const int TypeLimit = 20000;
        public const int FieldLimit = 50000;
        public const int MethodLimit = 20000;
        public const int WriteLimit = 100000;
        public const int IssueLimit = 10000;

        private const BindingFlags Fields = BindingFlags.DeclaredOnly | BindingFlags.Instance
            | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;

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
            var loaded = AppDomain.CurrentDomain.GetAssemblies().GroupBy(a => a.GetName().Name)
                .ToDictionary(g => g.Key, g => g.First(), StringComparer.Ordinal);
            var types = new List<Type>();
            foreach (string name in selectedAssemblies)
            {
                if (!loaded.TryGetValue(name, out Assembly assembly))
                {
                    incomplete = true;
                    document.Errors.Add("il_assembly:" + name + ": not loaded");
                    continue;
                }
                try { types.AddRange(assembly.GetTypes()); }
                catch (ReflectionTypeLoadException ex)
                {
                    incomplete = true;
                    types.AddRange(ex.Types.Where(t => t != null));
                    document.Errors.Add("il_assembly:" + name + ": some types could not be loaded");
                }
                catch (Exception ex)
                {
                    incomplete = true;
                    document.Errors.Add("il_assembly:" + name + ": " + ex.GetType().Name);
                }
            }
            types = types.Distinct().OrderBy(TypeId, StringComparer.Ordinal).ToList();
            List<Type> selectedTypes = document.Take("il_types", types, TypeLimit);
            if (incomplete) MarkUncertain(document.Caps["il_types"]);
            incomplete |= selectedTypes.Count != types.Count;

            var fields = new SortedDictionary<string, FieldInfo>(StringComparer.Ordinal);
            foreach (Type type in selectedTypes)
            {
                try
                {
                    foreach (FieldInfo field in type.GetFields(Fields)) fields[FieldId(field)] = field;
                }
                catch (Exception ex)
                {
                    incomplete = true;
                    document.Errors.Add("il_type:" + TypeId(type) + ": " + ex.GetType().Name);
                }
            }
            List<string> selectedFields = document.Take("il_fields", fields.Keys.ToList(), FieldLimit);
            if (incomplete) MarkUncertain(document.Caps["il_fields"]);
            incomplete |= selectedFields.Count != fields.Count;

            IlWriteScanResult scan = IlWriteScanner.Scan(selectedTypes, MethodLimit);
            document.Caps["il_methods"] = new InspectionCap
            {
                Limit = MethodLimit,
                Observed = incomplete ? (int?)null : scan.TotalMethods,
                Returned = scan.ScannedMethods,
                Truncated = scan.LimitReached ? true : incomplete ? (bool?)null : false,
            };
            incomplete |= !scan.IsComplete;
            var writes = scan.Writes.ToList();
            var selectedWrites = document.Take("il_writes", writes, WriteLimit);
            if (incomplete) MarkUncertain(document.Caps["il_writes"]);
            incomplete |= selectedWrites.Count != writes.Count;
            var issues = scan.Issues.ToList();
            var selectedIssues = document.Take("il_issues", issues, IssueLimit);
            if (incomplete) MarkUncertain(document.Caps["il_issues"]);
            incomplete |= selectedIssues.Count != issues.Count;

            var grouped = new Dictionary<string, List<object>>(StringComparer.Ordinal);
            foreach (IlWrite write in selectedWrites)
            {
                string id = FieldId(write.Field);
                if (!grouped.TryGetValue(id, out List<object> list)) grouped[id] = list = new List<object>();
                list.Add(new Dictionary<string, object>
                {
                    ["method"] = MethodId(write.Method), ["il_offset"] = write.IlOffset,
                    ["constructor"] = write.Method.IsConstructor,
                });
            }
            document.Data["il_write_sets"] = selectedFields.Select(id => (object)new Dictionary<string, object>
            {
                ["field_id"] = id,
                ["direct_writers"] = grouped.TryGetValue(id, out List<object> list) ? list : new List<object>(),
            }).ToList();
            var listed = new HashSet<string>(selectedFields, StringComparer.Ordinal);
            document.Data["il_unlisted_writes"] = grouped.Where(pair => !listed.Contains(pair.Key))
                .OrderBy(pair => pair.Key, StringComparer.Ordinal)
                .Select(pair => (object)new Dictionary<string, object>
                {
                    ["field_id"] = pair.Key, ["direct_writers"] = pair.Value,
                }).ToList();
            document.Data["il_issues"] = selectedIssues.Select(issue => (object)new Dictionary<string, object>
            {
                ["kind"] = issue.Kind.ToString(),
                ["method"] = issue.Method == null ? null : MethodId(issue.Method),
                ["field_id"] = issue.Field == null ? null : FieldId(issue.Field),
                ["il_offset"] = issue.IlOffset < 0 ? (int?)null : issue.IlOffset,
                ["reason"] = issue.Reason,
            }).ToList();
            document.Data["il_scope"] = "direct_stores_in_loaded_assemblies_containing_authored_scripts";
            document.Data["il_complete"] = !incomplete;
            return document;
        }

        private static void MarkUncertain(InspectionCap cap)
        {
            cap.Observed = null;
            if (cap.Truncated != true) cap.Truncated = null;
        }

        public static string TypeId(Type type) => type.Assembly.GetName().Name + ":" + (type.FullName ?? type.Name);
        public static string FieldId(FieldInfo field) => TypeId(field.DeclaringType) + ":" + field.Name;

        private static string MethodId(MethodBase method)
        {
            return TypeId(method.DeclaringType) + ":" + method.Name + "("
                + string.Join(",", method.GetParameters().Select(p => p.ParameterType.FullName ?? p.ParameterType.Name)) + ")";
        }
    }
}
