// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using UnityEditor;
using UnityEditor.PackageManager;
using UnityEngine;
using PackageInfo = UnityEditor.PackageManager.PackageInfo;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>Read-only Edit-mode survey of project structure and input configuration.</summary>
    public static class ProjectSurveyCollector
    {
        public const int PackageLimit = 1024;
        public const int SceneLimit = 4096;
        public const int ScriptLimit = 20000;
        public const int InputAssetLimit = 4096;
        public const int LegacyAxisLimit = 4096;

        public static InspectionDocument Collect()
        {
            if (EditorApplication.isPlayingOrWillChangePlaymode)
                throw new InvalidOperationException("The project survey requires Edit mode.");

            var result = new InspectionDocument();
            result.Data["unity_version"] = Application.unityVersion;
            result.Data["input_handling"] = ReadInputHandling(result.Errors);
            ReadPackages(result);
            ReadAssets(result, "scenes", "t:Scene", SceneLimit, SceneDescriptor);
            ReadAssets(result, "scripts", "t:MonoScript", ScriptLimit, path => new Dictionary<string, object> { ["path"] = path });
            ReadAssets(result, "input_assets", "t:InputActionAsset", InputAssetLimit, path => new Dictionary<string, object> { ["path"] = path });
            ReadLegacyAxes(result);
            return result;
        }

        private static void ReadPackages(InspectionDocument result)
        {
            try
            {
                var packages = PackageInfo.GetAllRegisteredPackages()
                    .OrderBy(p => p.name, StringComparer.Ordinal)
                    .Select(p => (object)new Dictionary<string, object>
                    {
                        ["name"] = p.name,
                        ["version"] = p.version,
                        ["source"] = p.source.ToString(),
                        ["asset_path"] = p.assetPath,
                    }).ToList();
                result.Data["packages"] = result.Take("packages", packages, PackageLimit);
            }
            catch (Exception ex)
            {
                result.Data["packages"] = null;
                result.Unavailable("packages", PackageLimit, ex.GetType().Name);
            }
        }

        // Includes embedded and local packages, where authored game scripts can live, but excludes
        // registry/built-in packages and this inspection package itself.
        public static bool IsAuthoredAsset(string path)
        {
            if (path.StartsWith("Assets/", StringComparison.Ordinal)) return true;
            if (!path.StartsWith("Packages/", StringComparison.Ordinal)) return false;
            PackageInfo package = PackageInfo.FindForAssetPath(path);
            return package != null && package.name != "com.playtest.bridge"
                && (package.source == PackageSource.Embedded || package.source == PackageSource.Local);
        }

        private static void ReadAssets(InspectionDocument result, string name, string filter, int limit,
            Func<string, object> describe)
        {
            try
            {
                var paths = AssetDatabase.FindAssets(filter)
                    .Select(AssetDatabase.GUIDToAssetPath)
                    .Where(p => !string.IsNullOrEmpty(p) && IsAuthoredAsset(p))
                    .Distinct(StringComparer.Ordinal).OrderBy(p => p, StringComparer.Ordinal).ToList();
                var selected = result.Take(name, paths, limit);
                result.Data[name] = selected.Select(describe).ToList();
            }
            catch (Exception ex)
            {
                result.Data[name] = null;
                result.Unavailable(name, limit, ex.GetType().Name);
            }
        }

        private static object SceneDescriptor(string path)
        {
            EditorBuildSettingsScene entry = EditorBuildSettings.scenes.FirstOrDefault(s => s.path == path);
            return new Dictionary<string, object>
            {
                ["path"] = path,
                ["in_build_settings"] = entry != null,
                ["enabled"] = entry == null ? (bool?)null : entry.enabled,
            };
        }

        private static string ReadInputHandling(List<string> errors)
        {
            try
            {
                string root = Path.GetDirectoryName(Application.dataPath);
                string settings = File.ReadAllText(Path.Combine(root, "ProjectSettings", "ProjectSettings.asset"));
                Match match = Regex.Match(settings, @"(?m)^\s*activeInputHandler:\s*(\d+)\s*$");
                if (match.Success)
                {
                    switch (match.Groups[1].Value)
                    {
                        case "0": return "legacy";
                        case "1": return "input_system";
                        case "2": return "both";
                    }
                }
                errors.Add("input_handling: setting missing or unrecognized");
            }
            catch (Exception ex)
            {
                errors.Add("input_handling: " + ex.GetType().Name);
            }
            return null;
        }

        private static void ReadLegacyAxes(InspectionDocument result)
        {
            try
            {
                UnityEngine.Object asset = AssetDatabase.LoadAllAssetsAtPath("ProjectSettings/InputManager.asset").FirstOrDefault();
                if (asset == null) throw new InvalidOperationException("InputManager not available");
                var serialized = new SerializedObject(asset);
                SerializedProperty axes = serialized.FindProperty("m_Axes");
                if (axes == null || !axes.isArray) throw new InvalidOperationException("axes not available");
                var found = new List<object>();
                for (int i = 0; i < axes.arraySize; i++)
                {
                    SerializedProperty axis = axes.GetArrayElementAtIndex(i);
                    var item = new Dictionary<string, object>();
                    foreach (string field in new[] { "m_Name", "descriptiveName", "descriptiveNegativeName", "negativeButton", "positiveButton", "altNegativeButton", "altPositiveButton", "gravity", "dead", "sensitivity", "snap", "invert", "type", "axis", "joyNum" })
                    {
                        SerializedProperty value = axis.FindPropertyRelative(field);
                        item[field] = PropertyValue(value);
                    }
                    found.Add(item);
                }
                result.Data["legacy_axes"] = result.Take("legacy_axes", found, LegacyAxisLimit);
            }
            catch (Exception ex)
            {
                result.Data["legacy_axes"] = null;
                result.Unavailable("legacy_axes", LegacyAxisLimit, ex.GetType().Name);
            }
        }

        private static object PropertyValue(SerializedProperty value)
        {
            if (value == null) return null;
            switch (value.propertyType)
            {
                case SerializedPropertyType.String: return value.stringValue;
                case SerializedPropertyType.Boolean: return value.boolValue;
                case SerializedPropertyType.Integer: return value.intValue;
                case SerializedPropertyType.Enum: return value.intValue;
                case SerializedPropertyType.Float: return value.floatValue;
                default: return null;
            }
        }
    }
}
