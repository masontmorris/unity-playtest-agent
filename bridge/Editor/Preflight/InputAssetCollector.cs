// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.Linq;
using UnityEditor;
using UnityEngine.InputSystem;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>Reads authored Input System assets even when no live component references them.</summary>
    public static class InputAssetCollector
    {
        public const int FileLimit = 4096;
        public const int ActionLimit = 20000;
        public const int BindingLimit = 40000;

        public static InspectionDocument Collect()
        {
            var document = new InspectionDocument();
            var actions = new List<object>();
            var bindings = new List<object>();
            int actionsSeen = 0;
            int bindingsSeen = 0;
            bool incomplete = false;
            try
            {
                List<string> paths = AssetDatabase.FindAssets("t:InputActionAsset")
                    .Select(AssetDatabase.GUIDToAssetPath)
                    .Where(path => !string.IsNullOrEmpty(path) && ProjectSurveyCollector.IsAuthoredAsset(path))
                    .Distinct(StringComparer.Ordinal).OrderBy(path => path, StringComparer.Ordinal).ToList();
                List<string> selected = document.Take("input_asset_files", paths, FileLimit);
                incomplete = selected.Count != paths.Count;
                foreach (string path in selected)
                {
                    try
                    {
                        InputActionAsset asset = AssetDatabase.LoadAssetAtPath<InputActionAsset>(path);
                        if (asset == null) throw new InvalidOperationException("asset unavailable");
                        foreach (InputActionMap map in asset.actionMaps)
                        {
                            foreach (InputAction action in map.actions)
                            {
                                actionsSeen++;
                                if (actions.Count < ActionLimit)
                                    actions.Add(new Dictionary<string, object>
                                    {
                                        ["asset_path"] = path, ["map_name"] = map.name, ["map_id"] = map.id.ToString(),
                                        ["name"] = action.name, ["id"] = action.id.ToString(),
                                        ["type"] = action.type.ToString(), ["expected_control_type"] = action.expectedControlType,
                                        ["interactions"] = action.interactions, ["processors"] = action.processors,
                                    });
                            }
                            foreach (InputBinding binding in map.bindings)
                            {
                                bindingsSeen++;
                                if (bindings.Count < BindingLimit)
                                    bindings.Add(new Dictionary<string, object>
                                    {
                                        ["asset_path"] = path, ["map_name"] = map.name, ["map_id"] = map.id.ToString(),
                                        ["id"] = binding.id.ToString(), ["name"] = binding.name,
                                        ["action"] = binding.action, ["path"] = binding.path,
                                        ["groups"] = binding.groups, ["interactions"] = binding.interactions,
                                        ["processors"] = binding.processors, ["is_composite"] = binding.isComposite,
                                        ["is_part_of_composite"] = binding.isPartOfComposite,
                                    });
                            }
                        }
                    }
                    catch (Exception ex)
                    {
                        incomplete = true;
                        document.Errors.Add("input_asset:" + path + ": " + ex.GetType().Name);
                    }
                }
            }
            catch (Exception ex)
            {
                incomplete = true;
                document.Unavailable("input_asset_files", FileLimit, ex.GetType().Name);
            }
            document.Data["input_asset_actions"] = actions;
            document.Data["input_asset_bindings"] = bindings;
            document.Caps["input_asset_actions"] = MakeCap(ActionLimit, actionsSeen, actions.Count, incomplete);
            document.Caps["input_asset_bindings"] = MakeCap(BindingLimit, bindingsSeen, bindings.Count, incomplete);
            return document;
        }

        private static InspectionCap MakeCap(int limit, int observed, int returned, bool incomplete)
        {
            return new InspectionCap
            {
                Limit = limit,
                Observed = incomplete ? (int?)null : observed,
                Returned = returned,
                Truncated = observed > returned ? true : incomplete ? (bool?)null : false,
            };
        }
    }
}
