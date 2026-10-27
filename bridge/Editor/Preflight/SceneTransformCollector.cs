using System;
using System.Collections.Generic;
using System.Globalization;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>
    /// Read-only sampling of every loaded GameObject's world position at one instant.
    ///
    /// It does not enter Play mode, load a scene, write to the project or read names, components,
    /// tags or any component property. Stage checks belong to the caller.
    ///
    /// Object ids come from <see cref="SceneDiscoveryCollector"/> and use the same scene path,
    /// loaded-scene index and ordered sibling indices, so a row joins a discovery snapshot exactly.
    /// A GameObject that can no longer be read records an error and keeps its position null; a
    /// position is never replaced by zero or a guess.
    /// </summary>
    public static class SceneTransformCollector
    {
        /// <summary>Upper bound on GameObjects sampled. The cap is recorded when it binds.</summary>
        public const int ObjectLimit = 10000;

        public static InspectionDocument Collect(int maxObjects)
        {
            if (maxObjects <= 0) throw new ArgumentOutOfRangeException(nameof(maxObjects));

            var document = new InspectionDocument();
            new Collector(document, maxObjects).Run();
            return document;
        }

        private sealed class Collector
        {
            private readonly InspectionDocument _document;
            private readonly int _maxObjects;
            private readonly List<object> _objects = new List<object>();

            private int _observed;
            private bool _failure;
            private bool _truncated;
            private bool _stopped;

            public Collector(InspectionDocument document, int maxObjects)
            {
                _document = document;
                _maxObjects = maxObjects;
            }

            public void Run()
            {
                _document.Data["selection"] = "all_loaded_scenes_depth_first";
                _document.Data["id_policy"] = "scene_path_loaded_scene_index_and_ordered_sibling_indices";
                _document.Data["position_space"] = "world";
                _document.Data["position_unit"] = "unity_world_unit";
                _document.Data["position_order"] = new[] { "x", "y", "z" };
                _document.Data["time_unit"] = "seconds";
                _document.Data["game_objects"] = _objects;

                SampleInstant();

                try
                {
                    CollectScenes();
                }
                catch (Exception ex)
                {
                    _failure = true;
                    _document.Errors.Add("scenes: " + Describe(ex));
                }

                Finish();
            }

            private void SampleInstant()
            {
                _document.Data["frame"] = null;
                _document.Data["real_time_s"] = null;
                _document.Data["game_time_s"] = null;

                try
                {
                    _document.Data["frame"] = Time.frameCount;
                }
                catch (Exception ex)
                {
                    _document.Errors.Add("frame: " + Describe(ex));
                }

                try
                {
                    _document.Data["real_time_s"] = Time.realtimeSinceStartupAsDouble;
                }
                catch (Exception ex)
                {
                    _document.Errors.Add("real_time_s: " + Describe(ex));
                }

                try
                {
                    _document.Data["game_time_s"] = Time.timeAsDouble;
                }
                catch (Exception ex)
                {
                    _document.Errors.Add("game_time_s: " + Describe(ex));
                }
            }

            private void CollectScenes()
            {
                int count;
                try
                {
                    count = SceneManager.sceneCount;
                }
                catch (Exception ex)
                {
                    _failure = true;
                    _document.Errors.Add("scenes: " + Describe(ex));
                    return;
                }

                for (int i = 0; i < count; i++)
                {
                    if (_stopped) break;

                    Scene scene;
                    bool valid;
                    bool loaded;
                    string scenePath;
                    try
                    {
                        scene = SceneManager.GetSceneAt(i);
                        valid = scene.IsValid();
                        loaded = scene.isLoaded;
                        scenePath = scene.path ?? string.Empty;
                    }
                    catch (Exception ex)
                    {
                        _failure = true;
                        _document.Errors.Add("scene[" + i.ToString(CultureInfo.InvariantCulture) + "]: " +
                                             Describe(ex));
                        continue;
                    }

                    if (!valid || !loaded) continue;

                    GameObject[] roots;
                    try
                    {
                        roots = scene.GetRootGameObjects();
                    }
                    catch (Exception ex)
                    {
                        _failure = true;
                        _document.Errors.Add("scene[" + i.ToString(CultureInfo.InvariantCulture) + "]: roots: " +
                                             Describe(ex));
                        continue;
                    }

                    string sceneId = SceneDiscoveryCollector.SceneId(scene, i);
                    for (int r = 0; r < roots.Length; r++)
                    {
                        if (_stopped) break;
                        Walk(roots[r], sceneId, scenePath, null, new[] { r });
                    }
                }
            }

            private void Walk(GameObject gameObject, string sceneId, string scenePath, string parentId, int[] indices)
            {
                if (_stopped) return;
                if (gameObject == null)
                {
                    _failure = true;
                    _document.Errors.Add("object: missing GameObject at " + SceneDiscoveryCollector.JoinIndices(indices));
                    return;
                }

                string objectId = SceneDiscoveryCollector.ObjectId(sceneId, indices);

                // Check one extra object to determine whether the collection is truncated.
                _observed++;
                if (_objects.Count >= _maxObjects)
                {
                    _truncated = true;
                    _stopped = true;
                    return;
                }

                var entry = new Dictionary<string, object>
                {
                    ["id"] = objectId,
                    ["scene_id"] = sceneId,
                    ["scene_path"] = scenePath,
                    ["parent_id"] = parentId,
                    ["active_self"] = TryBool(() => gameObject.activeSelf, objectId, "active_self"),
                    ["active_in_hierarchy"] = TryBool(() => gameObject.activeInHierarchy, objectId, "active_in_hierarchy"),
                    ["position_state"] = "unavailable",
                    ["position"] = null,
                };
                _objects.Add(entry);

                Transform transform;
                try
                {
                    transform = gameObject.transform;
                }
                catch (Exception ex)
                {
                    _failure = true;
                    _document.Errors.Add("object " + objectId + ": transform: " + Describe(ex));
                    return;
                }

                try
                {
                    Vector3 position = transform.position;
                    if (IsFinite(position.x) && IsFinite(position.y) && IsFinite(position.z))
                    {
                        entry["position"] = new[] { position.x, position.y, position.z };
                        entry["position_state"] = "present";
                    }
                    else
                    {
                        _document.Errors.Add("object " + objectId + ": position is not finite");
                    }
                }
                catch (Exception ex)
                {
                    _document.Errors.Add("object " + objectId + ": position: " + Describe(ex));
                }

                int childCount;
                try
                {
                    childCount = transform.childCount;
                }
                catch (Exception ex)
                {
                    _failure = true;
                    _document.Errors.Add("object " + objectId + ": children: " + Describe(ex));
                    return;
                }

                for (int i = 0; i < childCount; i++)
                {
                    if (_stopped) break;

                    Transform child;
                    try
                    {
                        child = transform.GetChild(i);
                    }
                    catch (Exception ex)
                    {
                        _failure = true;
                        _document.Errors.Add("object " + objectId + ": child " +
                                             i.ToString(CultureInfo.InvariantCulture) + ": " + Describe(ex));
                        continue;
                    }

                    if (child == null)
                    {
                        _failure = true;
                        _document.Errors.Add("object " + objectId + ": child " +
                                             i.ToString(CultureInfo.InvariantCulture) + " is null");
                        continue;
                    }

                    GameObject childObject;
                    try
                    {
                        childObject = child.gameObject;
                    }
                    catch (Exception ex)
                    {
                        _failure = true;
                        _document.Errors.Add("object " + objectId + ": child " +
                                             i.ToString(CultureInfo.InvariantCulture) + " gameObject: " +
                                             Describe(ex));
                        continue;
                    }

                    var childIndices = new int[indices.Length + 1];
                    Array.Copy(indices, childIndices, indices.Length);
                    childIndices[indices.Length] = i;
                    Walk(childObject, sceneId, scenePath, objectId, childIndices);
                }
            }

            private bool? TryBool(Func<bool> read, string objectId, string field)
            {
                try
                {
                    return read();
                }
                catch (Exception ex)
                {
                    _document.Errors.Add("object " + objectId + ": " + field + ": " + Describe(ex));
                    return null;
                }
            }

            private void Finish()
            {
                // A structural failure leaves coverage unknown; an unreadable position stays null
                // on its own row without hiding how many objects were actually visited.
                _document.Caps["game_objects"] = new InspectionCap
                {
                    Limit = _maxObjects,
                    Observed = _failure || _truncated ? (int?)null : _observed,
                    Returned = _objects.Count,
                    Truncated = _failure ? (bool?)null : _truncated,
                };
            }

            private static string Describe(Exception exception)
            {
                if (exception == null) return "unknown";
                return exception.GetType().Name + ": " + exception.Message;
            }

            private static bool IsFinite(float value)
            {
                return !float.IsNaN(value) && !float.IsInfinity(value);
            }
        }
    }
}
