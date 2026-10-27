using System;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using System.Runtime.CompilerServices;
using UnityEditor;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.SceneManagement;
using UnityEngine.Serialization;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>One live-scene collection's bounds and observations.</summary>
    public sealed class SceneCap
    {
        public int Limit;
        public int? Observed;
        public int Returned;
        public bool? Truncated;
    }

    /// <summary>
    /// The inspect response for live-scene discovery. Data contains only JSON-friendly primitives and
    /// collections; Caps records each collection's coverage separately from its rows.
    /// </summary>
    public sealed class SceneDiscoveryResult
    {
        public readonly Dictionary<string, object> Data = new Dictionary<string, object>();
        public readonly Dictionary<string, SceneCap> Caps = new Dictionary<string, SceneCap>();
        public readonly List<string> Errors = new List<string>();
    }

    /// <summary>
    /// Read-only discovery of the scenes that are loaded in the Editor right now. It does not enter
    /// Play mode, load a scene, or write to the project; stage checks belong to the caller.
    ///
    /// Identity is derived from the scene path, ordered sibling indices, component index and the
    /// reflected declaring assembly/type/field name. Object names and tags are display data only.
    /// Field values are read directly from fields, never from properties, and arbitrary objects are
    /// never converted with ToString. Input System action structures are the only nested objects
    /// traversed, and they are traversed through their public data APIs.
    /// </summary>
    public static class SceneDiscoveryCollector
    {
        /// <summary>Upper bound on the number of loaded scenes inspected.</summary>
        public const int SceneLimit = 4096;

        /// <summary>Upper bound on GameObjects inspected. The cap is recorded when it binds.</summary>
        public const int ObjectLimit = 100000;

        /// <summary>Strings longer than this are returned as an explicit prefix plus their length.</summary>
        public const int StringValueLimit = 4096;

        public static SceneDiscoveryResult Collect(int maxComponents, int maxFields, int maxBindings)
        {
            if (maxComponents <= 0) throw new ArgumentOutOfRangeException(nameof(maxComponents));
            if (maxFields <= 0) throw new ArgumentOutOfRangeException(nameof(maxFields));
            if (maxBindings <= 0) throw new ArgumentOutOfRangeException(nameof(maxBindings));

            var result = new SceneDiscoveryResult();
            new Collector(result, maxComponents, maxFields, maxBindings).Run();
            return result;
        }

        /// <summary>
        /// Stable scene id from the path and the loaded-scene index. Shared with every stage that
        /// must join a discovery row, so the derivation exists once.
        /// </summary>
        internal static string SceneId(Scene scene, int index)
        {
            string path = scene.path;
            string suffix = "@" + index.ToString(CultureInfo.InvariantCulture);
            if (!string.IsNullOrEmpty(path)) return "scene:" + path + suffix;
            string name = scene.name;
            return "scene:untitled:" + (name ?? string.Empty) + suffix;
        }

        /// <summary>Hierarchy coordinates joined root-first, for example <c>0.2.1</c>.</summary>
        internal static string JoinIndices(int[] indices)
        {
            if (indices == null || indices.Length == 0) return string.Empty;
            var parts = new string[indices.Length];
            for (int i = 0; i < indices.Length; i++)
            {
                parts[i] = indices[i].ToString(CultureInfo.InvariantCulture);
            }

            return string.Join(".", parts);
        }

        /// <summary>Object id shared by discovery and the transforms stage.</summary>
        internal static string ObjectId(string sceneId, int[] indices)
        {
            return sceneId + "::" + JoinIndices(indices);
        }

        private sealed class Collector
        {
            private readonly SceneDiscoveryResult _result;
            private readonly int _maxComponents;
            private readonly int _maxFields;
            private readonly int _maxBindings;

            private readonly List<object> _scenes = new List<object>();
            private readonly List<object> _objects = new List<object>();
            private readonly List<object> _components = new List<object>();
            private readonly List<object> _fields = new List<object>();
            private readonly List<object> _bindings = new List<object>();
            private readonly List<CollectedComponent> _collectedComponents = new List<CollectedComponent>();

            private readonly Dictionary<UnityEngine.Object, string> _objectIds =
                new Dictionary<UnityEngine.Object, string>(ReferenceComparer.Instance);
            private readonly Dictionary<UnityEngine.Object, string> _objectSceneIds =
                new Dictionary<UnityEngine.Object, string>(ReferenceComparer.Instance);
            private readonly Dictionary<UnityEngine.Object, string> _componentIds =
                new Dictionary<UnityEngine.Object, string>(ReferenceComparer.Instance);
            private readonly Dictionary<UnityEngine.Object, string> _componentObjectIds =
                new Dictionary<UnityEngine.Object, string>(ReferenceComparer.Instance);
            private readonly Dictionary<UnityEngine.Object, string> _componentSceneIds =
                new Dictionary<UnityEngine.Object, string>(ReferenceComparer.Instance);
            private readonly Dictionary<UnityEngine.Object, AssetInfo> _assetInfo =
                new Dictionary<UnityEngine.Object, AssetInfo>(ReferenceComparer.Instance);

            private int _sceneTotal;
            private int _objectTotal;
            private int _componentTotal;
            private int _fieldTotal;
            private int _bindingTotal;

            private bool _sceneFailure;
            private bool _objectFailure;
            private bool _componentFailure;
            private bool _fieldFailure;
            private bool _bindingFailure;

            private bool _scenesTruncated;
            private bool _objectsTruncated;
            private bool _objectsStopped;

            public Collector(SceneDiscoveryResult result, int maxComponents, int maxFields, int maxBindings)
            {
                _result = result;
                _maxComponents = maxComponents;
                _maxFields = maxFields;
                _maxBindings = maxBindings;
            }

            public void Run()
            {
                _result.Data["selection"] = "all_loaded_valid_scenes_depth_first";
                _result.Data["id_policy"] = "scene_path_loaded_scene_index_and_ordered_sibling_indices";
                _result.Data["field_policy"] =
                    "instance_fields_declared_by_managed_behaviours_and_their_managed_bases";
                _result.Data["binding_policy"] = "live_component_fields_only";
                _result.Data["string_value_character_limit"] = StringValueLimit;
                _result.Data["scenes"] = _scenes;
                _result.Data["game_objects"] = _objects;
                _result.Data["components"] = _components;
                _result.Data["fields"] = _fields;
                _result.Data["bindings"] = _bindings;

                try
                {
                    CollectScenes();
                }
                catch (Exception ex)
                {
                    _sceneFailure = true;
                    _objectFailure = true;
                    _componentFailure = true;
                    _fieldFailure = true;
                    _bindingFailure = true;
                    _result.Errors.Add("scenes: " + Describe(ex));
                }

                try
                {
                    CollectFields();
                }
                catch (Exception ex)
                {
                    _fieldFailure = true;
                    _bindingFailure = true;
                    _result.Errors.Add("fields: " + Describe(ex));
                }

                Finish();
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
                    _sceneFailure = true;
                    _objectFailure = true;
                    _componentFailure = true;
                    _fieldFailure = true;
                    _bindingFailure = true;
                    _result.Errors.Add("scenes: " + Describe(ex));
                    return;
                }

                _sceneTotal = count;
                int limit = Math.Min(count, SceneLimit);
                _scenesTruncated = count > limit;

                for (int i = 0; i < limit; i++)
                {
                    if (_objectsStopped) break;

                    Scene scene;
                    string sceneId;
                    string scenePath;
                    string sceneName;
                    bool valid;
                    bool loaded;
                    try
                    {
                        scene = SceneManager.GetSceneAt(i);
                        valid = scene.IsValid();
                        loaded = scene.isLoaded;
                        scenePath = scene.path ?? string.Empty;
                        sceneName = scene.name ?? string.Empty;
                        sceneId = valid
                            ? SceneDiscoveryCollector.SceneId(scene, i)
                            : "scene:invalid:" + i.ToString(CultureInfo.InvariantCulture);
                    }
                    catch (Exception ex)
                    {
                        _sceneFailure = true;
                        _result.Errors.Add("scene[" + i.ToString(CultureInfo.InvariantCulture) + "]: " + Describe(ex));
                        continue;
                    }

                    var descriptor = new Dictionary<string, object>
                    {
                        ["id"] = sceneId,
                        ["path"] = scenePath,
                        ["name"] = sceneName,
                        ["is_valid"] = valid,
                        ["is_loaded"] = loaded,
                    };

                    if (!valid || !loaded)
                    {
                        descriptor["collected"] = false;
                        descriptor["skip_reason"] = valid ? "not_loaded" : "not_valid";
                        _scenes.Add(descriptor);
                        continue;
                    }

                    GameObject[] roots;
                    try
                    {
                        roots = scene.GetRootGameObjects();
                    }
                    catch (Exception ex)
                    {
                        _objectFailure = true;
                        descriptor["collected"] = false;
                        descriptor["skip_reason"] = "roots_unavailable";
                        descriptor["error"] = Describe(ex);
                        _scenes.Add(descriptor);
                        _result.Errors.Add("scene " + sceneId + ": roots: " + Describe(ex));
                        continue;
                    }

                    descriptor["collected"] = true;
                    descriptor["root_count"] = roots.Length;
                    var rootIds = new List<object>();
                    for (int r = 0; r < roots.Length; r++)
                    {
                        if (_objectsStopped) break;
                        string rootId = Walk(roots[r], sceneId, scenePath, null, new[] { r }, string.Empty, 0);
                        if (rootId != null) rootIds.Add(rootId);
                    }

                    descriptor["root_ids"] = rootIds;
                    _scenes.Add(descriptor);
                }
            }

            private string Walk(GameObject gameObject, string sceneId, string scenePath, string parentId, int[] indices,
                string parentPath, int depth)
            {
                if (_objectsStopped) return null;
                if (gameObject == null)
                {
                    _objectFailure = true;
                    _result.Errors.Add("object: missing GameObject at " + SceneDiscoveryCollector.JoinIndices(indices));
                    return null;
                }

                _objectTotal++;
                if (_objectTotal > ObjectLimit)
                {
                    _objectsTruncated = true;
                    _objectsStopped = true;
                    return null;
                }

                string objectId = SceneDiscoveryCollector.ObjectId(sceneId, indices);
                _objectIds[gameObject] = objectId;
                _objectSceneIds[gameObject] = sceneId;

                string name = null;
                try
                {
                    name = gameObject.name;
                }
                catch (Exception ex)
                {
                    _objectFailure = true;
                    _result.Errors.Add("object " + objectId + ": name: " + Describe(ex));
                }

                string path = string.IsNullOrEmpty(parentPath) ? (name ?? string.Empty) : parentPath + "/" + (name ?? string.Empty);
                var entry = new Dictionary<string, object>
                {
                    ["id"] = objectId,
                    ["scene_id"] = sceneId,
                    ["scene_path"] = scenePath,
                    ["parent_id"] = parentId,
                    ["name"] = name,
                    ["path"] = path,
                    ["sibling_index"] = indices[indices.Length - 1],
                    ["depth"] = depth,
                    ["active_self"] = TryBool(() => gameObject.activeSelf),
                    ["active_in_hierarchy"] = TryBool(() => gameObject.activeInHierarchy),
                    ["is_static"] = TryBool(() => gameObject.isStatic),
                };

                CollectComponents(gameObject, entry, objectId, sceneId);

                _objects.Add(entry);

                Transform transform;
                try
                {
                    transform = gameObject.transform;
                }
                catch (Exception ex)
                {
                    _objectFailure = true;
                    _result.Errors.Add("object " + objectId + ": transform: " + Describe(ex));
                    return objectId;
                }

                int childCount;
                try
                {
                    childCount = transform.childCount;
                }
                catch (Exception ex)
                {
                    _objectFailure = true;
                    _result.Errors.Add("object " + objectId + ": children: " + Describe(ex));
                    return objectId;
                }

                for (int i = 0; i < childCount; i++)
                {
                    if (_objectsStopped) break;
                    Transform child;
                    try
                    {
                        child = transform.GetChild(i);
                    }
                    catch (Exception ex)
                    {
                        _objectFailure = true;
                        _result.Errors.Add("object " + objectId + ": child " + i.ToString(CultureInfo.InvariantCulture) +
                                           ": " + Describe(ex));
                        continue;
                    }

                    if (child == null)
                    {
                        _objectFailure = true;
                        _result.Errors.Add("object " + objectId + ": child " + i.ToString(CultureInfo.InvariantCulture) +
                                           " is null");
                        continue;
                    }

                    var childIndices = new int[indices.Length + 1];
                    Array.Copy(indices, childIndices, indices.Length);
                    childIndices[indices.Length] = i;

                    GameObject childObject;
                    try
                    {
                        childObject = child.gameObject;
                    }
                    catch (Exception ex)
                    {
                        _objectFailure = true;
                        _result.Errors.Add("object " + objectId + ": child " + i.ToString(CultureInfo.InvariantCulture) +
                                           " gameObject: " + Describe(ex));
                        continue;
                    }

                    Walk(childObject, sceneId, scenePath, objectId, childIndices, path, depth + 1);
                }

                return objectId;
            }

            private void CollectComponents(GameObject gameObject, Dictionary<string, object> objectEntry, string objectId,
                string sceneId)
            {
                Component[] components;
                try
                {
                    components = gameObject.GetComponents<Component>();
                }
                catch (Exception ex)
                {
                    _componentFailure = true;
                    objectEntry["component_count"] = null;
                    objectEntry["components_returned"] = 0;
                    objectEntry["components_complete"] = null;
                    objectEntry["missing_component_count"] = null;
                    _result.Errors.Add("object " + objectId + ": components: " + Describe(ex));
                    return;
                }

                _componentTotal += components.Length;
                int missing = 0;
                int returned = 0;

                for (int i = 0; i < components.Length; i++)
                {
                    Component component = components[i];
                    string componentId = objectId + "::component:" + i.ToString(CultureInfo.InvariantCulture);
                    if (component == null)
                    {
                        missing++;
                        if (_components.Count < _maxComponents)
                        {
                            _components.Add(MissingComponent(componentId, objectId, sceneId, i));
                            returned++;
                        }

                        continue;
                    }

                    _componentIds[component] = componentId;
                    _componentObjectIds[component] = objectId;
                    _componentSceneIds[component] = sceneId;

                    if (_components.Count >= _maxComponents) continue;

                    Dictionary<string, object> entry;
                    try
                    {
                        entry = ComponentEntry(component, componentId, objectId, sceneId, i, out bool managedBehaviour);
                        if (managedBehaviour) _collectedComponents.Add(new CollectedComponent(component, entry, componentId));
                    }
                    catch (Exception ex)
                    {
                        _componentFailure = true;
                        _fieldFailure = true;
                        entry = new Dictionary<string, object>
                        {
                            ["id"] = componentId,
                            ["object_id"] = objectId,
                            ["scene_id"] = sceneId,
                            ["index"] = i,
                            ["missing"] = false,
                            ["type"] = null,
                            ["assembly"] = null,
                            ["type_id"] = null,
                            ["managed_behaviour"] = null,
                            ["enabled"] = null,
                            ["field_count"] = null,
                            ["fields_returned"] = 0,
                            ["fields_complete"] = null,
                            ["error"] = Describe(ex),
                        };
                        _result.Errors.Add("component " + componentId + ": " + Describe(ex));
                    }

                    _components.Add(entry);
                    returned++;
                }

                objectEntry["component_count"] = components.Length;
                objectEntry["components_returned"] = returned;
                objectEntry["components_complete"] = returned == components.Length;
                objectEntry["missing_component_count"] = missing;
            }

            private Dictionary<string, object> MissingComponent(string componentId, string objectId, string sceneId,
                int index)
            {
                return new Dictionary<string, object>
                {
                    ["id"] = componentId,
                    ["object_id"] = objectId,
                    ["scene_id"] = sceneId,
                    ["index"] = index,
                    ["missing"] = true,
                    ["type"] = null,
                    ["assembly"] = null,
                    ["type_id"] = null,
                    ["managed_behaviour"] = null,
                    ["enabled"] = null,
                    ["field_count"] = null,
                    ["fields_returned"] = 0,
                    ["fields_complete"] = null,
                };
            }

            private Dictionary<string, object> ComponentEntry(Component component, string componentId, string objectId,
                string sceneId, int index, out bool managedBehaviour)
            {
                Type type = component.GetType();
                string assembly = AssemblyName(type);
                string typeName = TypeName(type);
                managedBehaviour = IsManagedBehaviour(type);

                bool? enabled = null;
                try
                {
                    if (component is Behaviour behaviour) enabled = behaviour.enabled;
                }
                catch (Exception ex)
                {
                    _result.Errors.Add("component " + componentId + ": enabled: " + Describe(ex));
                }

                return new Dictionary<string, object>
                {
                    ["id"] = componentId,
                    ["object_id"] = objectId,
                    ["scene_id"] = sceneId,
                    ["index"] = index,
                    ["missing"] = false,
                    ["type"] = typeName,
                    ["assembly"] = assembly,
                    ["type_id"] = assembly == null || typeName == null ? null : assembly + ":" + typeName,
                    ["managed_behaviour"] = managedBehaviour,
                    ["enabled"] = enabled,
                    ["field_count"] = null,
                    ["fields_returned"] = 0,
                    ["fields_complete"] = null,
                };
            }

            private void CollectFields()
            {
                foreach (CollectedComponent collected in _collectedComponents)
                {
                    Dictionary<string, object> entry = collected.Entry;
                    Component component = collected.Component;
                    if (component == null)
                    {
                        _fieldFailure = true;
                        _result.Errors.Add("component " + collected.Id + ": no longer present");
                        entry["field_count"] = null;
                        entry["fields_complete"] = null;
                        continue;
                    }

                    List<FieldInfo> fields;
                    try
                    {
                        fields = ManagedBehaviourFields(component.GetType());
                    }
                    catch (Exception ex)
                    {
                        _fieldFailure = true;
                        entry["field_count"] = null;
                        entry["fields_complete"] = null;
                        _result.Errors.Add("component " + collected.Id + ": fields: " + Describe(ex));
                        continue;
                    }

                    entry["field_count"] = fields.Count;
                    int returned = 0;
                    foreach (FieldInfo field in fields)
                    {
                        _fieldTotal++;
                        if (_fields.Count >= _maxFields) continue;
                        _fields.Add(DescribeField(component, field, collected));
                        returned++;
                    }

                    entry["fields_returned"] = returned;
                    entry["fields_complete"] = returned == fields.Count;
                }
            }

            private List<FieldInfo> ManagedBehaviourFields(Type type)
            {
                var hierarchy = new List<Type>();
                for (Type current = type; current != null && IsManagedBehaviour(current); current = current.BaseType)
                {
                    hierarchy.Add(current);
                }

                hierarchy.Reverse();

                var fields = new List<FieldInfo>();
                foreach (Type current in hierarchy)
                {
                    FieldInfo[] declared = current.GetFields(
                        BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
                    Array.Sort(declared, FieldOrder.Instance);
                    fields.AddRange(declared);
                }

                return fields;
            }

            private Dictionary<string, object> DescribeField(Component component, FieldInfo field,
                CollectedComponent owner)
            {
                string fieldId = FieldId(owner.Id, field);
                string fieldTypeName = TypeName(field.FieldType);
                string fieldAssembly = AssemblyName(field.FieldType);
                bool nonSerialized = field.IsNotSerialized ||
                                     field.IsDefined(typeof(NonSerializedAttribute), false);
                bool serializeField = field.IsDefined(typeof(SerializeField), false);
                bool? serialized = SerializedState(component, field, fieldId);

                var entry = new Dictionary<string, object>
                {
                    ["id"] = fieldId,
                    ["component_id"] = owner.Id,
                    ["object_id"] = owner.Entry["object_id"],
                    ["scene_id"] = owner.Entry["scene_id"],
                    ["name"] = field.Name,
                    ["declaring_assembly"] = AssemblyName(field.DeclaringType),
                    ["declaring_type"] = TypeName(field.DeclaringType),
                    ["declaration_id"] = AssemblyName(field.DeclaringType) == null || TypeName(field.DeclaringType) == null
                        ? null : IlDiscoveryCollector.FieldId(field),
                    ["field_type"] = fieldTypeName,
                    ["field_type_assembly"] = fieldAssembly,
                    ["field_type_kind"] = FieldTypeKind(field.FieldType),
                    ["visibility"] = Visibility(field),
                    ["is_static"] = field.IsStatic,
                    ["is_readonly"] = field.IsInitOnly,
                    ["is_literal"] = field.IsLiteral,
                    ["compiler_generated"] = field.IsDefined(typeof(CompilerGeneratedAttribute), false),
                    ["serialize_field"] = serializeField,
                    ["non_serialized"] = nonSerialized,
                    ["serialize_reference"] = field.IsDefined(typeof(SerializeReference), false),
                    ["hide_in_inspector"] = field.IsDefined(typeof(HideInInspector), false),
                    ["serialized"] = serialized,
                    ["formerly_serialized_as"] = FormerlySerializedNames(field),
                    ["value_state"] = "error",
                    ["value_kind"] = "error",
                    ["value"] = null,
                    ["is_null"] = null,
                    ["destroyed"] = null,
                    ["value_length"] = null,
                    ["value_truncated"] = null,
                    ["enum_name"] = null,
                    ["read_error"] = null,
                    ["binding_count"] = null,
                    ["bindings_returned"] = 0,
                    ["bindings_complete"] = null,
                };

                string declaredInputKind = InputKind(field.FieldType);
                if (declaredInputKind != null) entry["input_kind"] = declaredInputKind;

                object value;
                try
                {
                    value = field.GetValue(component);
                }
                catch (Exception ex)
                {
                    string error = Describe(ex);
                    entry["read_error"] = error;
                    _result.Errors.Add("field " + fieldId + ": read: " + error);
                    if (declaredInputKind != null || field.FieldType == typeof(object)) _bindingFailure = true;
                    return entry;
                }

                Dictionary<string, object> inputIdentity = null;
                try
                {
                    DescribeValue(entry, value, declaredInputKind, out inputIdentity);
                }
                catch (Exception ex)
                {
                    string error = Describe(ex);
                    entry["value_state"] = "error";
                    entry["value_kind"] = "error";
                    entry["value"] = null;
                    entry["is_null"] = null;
                    entry["destroyed"] = null;
                    entry["read_error"] = error;
                    _result.Errors.Add("field " + fieldId + ": value: " + error);
                    if (declaredInputKind != null || field.FieldType == typeof(object)) _bindingFailure = true;
                    return entry;
                }

                if (inputIdentity == null) return entry;

                var source = new FieldSource
                {
                    FieldId = fieldId,
                    FieldName = field.Name,
                    DeclaringType = TypeName(field.DeclaringType),
                    DeclaringAssembly = AssemblyName(field.DeclaringType),
                    FieldType = fieldTypeName,
                    ComponentId = owner.Id,
                    ObjectId = (string)owner.Entry["object_id"],
                    SceneId = (string)owner.Entry["scene_id"],
                    InputKind = entry.TryGetValue("input_kind", out object inputKind) ? inputKind as string : null,
                };

                int bindingsReturned = 0;
                bool failed = false;
                try
                {
                    int count = ExtractBindings(value, source, ref bindingsReturned);
                    entry["binding_count"] = count;
                    entry["bindings_returned"] = bindingsReturned;
                    entry["bindings_complete"] = bindingsReturned == count;
                    inputIdentity["binding_count"] = count;
                }
                catch (Exception ex)
                {
                    failed = true;
                    _bindingFailure = true;
                    entry["binding_count"] = null;
                    entry["bindings_returned"] = bindingsReturned;
                    entry["bindings_complete"] = null;
                    inputIdentity["binding_count"] = null;
                    _result.Errors.Add("field " + fieldId + ": bindings: " + Describe(ex));
                }

                if (failed) entry["bindings_complete"] = null;
                return entry;
            }

            private void DescribeValue(Dictionary<string, object> entry, object value, string declaredInputKind,
                out Dictionary<string, object> inputIdentity)
            {
                inputIdentity = null;

                if (value == null)
                {
                    entry["value_state"] = "null";
                    entry["value_kind"] = "null";
                    entry["is_null"] = true;
                    entry["destroyed"] = false;
                    return;
                }

                if (value is InputAction action)
                {
                    NoteInputKind(entry, "InputAction");
                    entry["value_state"] = "present";
                    entry["value_kind"] = "input_action";
                    entry["is_null"] = false;
                    entry["destroyed"] = false;
                    inputIdentity = DescribeAction(action);
                    entry["value"] = inputIdentity;
                    return;
                }

                if (value is InputActionReference reference)
                {
                    if (reference == null)
                    {
                        DescribeDestroyed(entry, "input_action_reference");
                        return;
                    }

                    NoteInputKind(entry, "InputActionReference");
                    entry["value_state"] = "present";
                    entry["value_kind"] = "input_action_reference";
                    entry["is_null"] = false;
                    entry["destroyed"] = false;
                    inputIdentity = DescribeReference(reference);
                    entry["value"] = inputIdentity;
                    return;
                }

                if (value is InputActionMap map)
                {
                    NoteInputKind(entry, "InputActionMap");
                    entry["value_state"] = "present";
                    entry["value_kind"] = "input_action_map";
                    entry["is_null"] = false;
                    entry["destroyed"] = false;
                    inputIdentity = DescribeMap(map);
                    entry["value"] = inputIdentity;
                    return;
                }

                if (value is InputActionAsset asset)
                {
                    if (asset == null)
                    {
                        DescribeDestroyed(entry, "input_action_asset");
                        return;
                    }

                    NoteInputKind(entry, "InputActionAsset");
                    entry["value_state"] = "present";
                    entry["value_kind"] = "input_action_asset";
                    entry["is_null"] = false;
                    entry["destroyed"] = false;
                    inputIdentity = DescribeAsset(asset);
                    entry["value"] = inputIdentity;
                    return;
                }

                if (value is InputActionProperty property)
                {
                    NoteInputKind(entry, "InputActionProperty");
                    entry["value_state"] = "present";
                    entry["value_kind"] = "input_action_property";
                    entry["is_null"] = false;
                    entry["destroyed"] = false;
                    inputIdentity = DescribeProperty(property);
                    entry["value"] = inputIdentity;
                    return;
                }

                if (value is UnityEngine.Object unityObject)
                {
                    if (unityObject == null)
                    {
                        DescribeDestroyed(entry, "object_reference");
                        return;
                    }

                    entry["value_state"] = "present";
                    entry["value_kind"] = "object_reference";
                    entry["is_null"] = false;
                    entry["destroyed"] = false;
                    entry["value"] = ReferenceDescriptor(unityObject);
                    return;
                }

                Type runtimeType = value.GetType();
                if (runtimeType == typeof(string))
                {
                    string text = (string)value;
                    int length = text.Length;
                    string returned = length > StringValueLimit ? text.Substring(0, StringValueLimit) : text;
                    entry["value_state"] = "present";
                    entry["value_kind"] = "string";
                    entry["value"] = returned;
                    entry["is_null"] = false;
                    entry["destroyed"] = false;
                    entry["value_length"] = length;
                    entry["value_truncated"] = length > StringValueLimit;
                    return;
                }

                if (runtimeType == typeof(bool))
                {
                    Present(entry, "boolean", (bool)value);
                    return;
                }

                if (runtimeType == typeof(char))
                {
                    Present(entry, "character", ((char)value).ToString());
                    return;
                }

                if (runtimeType == typeof(sbyte) || runtimeType == typeof(byte) ||
                    runtimeType == typeof(short) || runtimeType == typeof(ushort) ||
                    runtimeType == typeof(int) || runtimeType == typeof(uint) ||
                    runtimeType == typeof(long))
                {
                    Present(entry, "integer", Convert.ToInt64(value, CultureInfo.InvariantCulture));
                    return;
                }

                if (runtimeType == typeof(ulong))
                {
                    Present(entry, "integer", (ulong)value);
                    return;
                }

                if (runtimeType == typeof(float))
                {
                    float number = (float)value;
                    if (float.IsNaN(number) || float.IsInfinity(number))
                    {
                        Present(entry, "special_number", SpecialFloat(number));
                        return;
                    }

                    Present(entry, "number", number);
                    return;
                }

                if (runtimeType == typeof(double))
                {
                    double number = (double)value;
                    if (double.IsNaN(number) || double.IsInfinity(number))
                    {
                        Present(entry, "special_number", SpecialDouble(number));
                        return;
                    }

                    Present(entry, "number", number);
                    return;
                }

                if (runtimeType == typeof(decimal))
                {
                    Present(entry, "number", (decimal)value);
                    return;
                }

                if (runtimeType.IsEnum)
                {
                    object numeric = Convert.ChangeType(value, Enum.GetUnderlyingType(runtimeType),
                        CultureInfo.InvariantCulture);
                    entry["value_state"] = "present";
                    entry["value_kind"] = "enum";
                    entry["value"] = numeric;
                    entry["is_null"] = false;
                    entry["destroyed"] = false;
                    entry["enum_name"] = Enum.GetName(runtimeType, value);
                    return;
                }

                entry["value_state"] = "unsupported";
                entry["value_kind"] = "unsupported";
                entry["value"] = null;
                entry["is_null"] = null;
                entry["destroyed"] = null;
                entry["unsupported_type"] = TypeName(runtimeType);
            }

            private static void NoteInputKind(Dictionary<string, object> entry, string kind)
            {
                if (!entry.ContainsKey("input_kind")) entry["input_kind"] = kind;
            }

            private static void Present(Dictionary<string, object> entry, string kind, object value)
            {
                entry["value_state"] = "present";
                entry["value_kind"] = kind;
                entry["value"] = value;
                entry["is_null"] = false;
                entry["destroyed"] = false;
            }

            private static void DescribeDestroyed(Dictionary<string, object> entry, string kind)
            {
                entry["value_state"] = "destroyed";
                entry["value_kind"] = kind;
                entry["value"] = null;
                entry["is_null"] = true;
                entry["destroyed"] = true;
            }

            private Dictionary<string, object> ReferenceDescriptor(UnityEngine.Object unityObject)
            {
                var descriptor = new Dictionary<string, object>
                {
                    ["type"] = TypeName(unityObject.GetType()),
                    ["name"] = TryString(() => unityObject.name),
                };

                if (_objectIds.TryGetValue(unityObject, out string objectId))
                {
                    descriptor["kind"] = "scene_object";
                    descriptor["id"] = objectId;
                    descriptor["object_id"] = objectId;
                    descriptor["scene_id"] = _objectSceneIds.TryGetValue(unityObject, out string sceneId) ? sceneId : null;
                    descriptor["identity_basis"] = "scene_hierarchy";
                    return descriptor;
                }

                if (_componentIds.TryGetValue(unityObject, out string componentId))
                {
                    descriptor["kind"] = "component";
                    descriptor["id"] = componentId;
                    descriptor["component_id"] = componentId;
                    descriptor["object_id"] = _componentObjectIds.TryGetValue(unityObject, out string ownerId) ? ownerId : null;
                    descriptor["scene_id"] = _componentSceneIds.TryGetValue(unityObject, out string sceneId) ? sceneId : null;
                    descriptor["identity_basis"] = "scene_hierarchy";
                    return descriptor;
                }

                AssetInfo info = GetAssetInfo(unityObject);
                if (info != null && (!string.IsNullOrEmpty(info.Path) || !string.IsNullOrEmpty(info.Guid)))
                {
                    descriptor["kind"] = unityObject is Component ? "asset_component" : "asset_object";
                    descriptor["asset_path"] = string.IsNullOrEmpty(info.Path) ? null : info.Path;
                    descriptor["asset_guid"] = string.IsNullOrEmpty(info.Guid) ? null : info.Guid;
                    descriptor["local_file_id"] = info.HasLocalFileId ? (object)info.LocalFileId : null;
                    descriptor["identity_basis"] = info.HasLocalFileId ? "asset_local_file_id" : "asset_path";
                    descriptor["id"] = info.HasLocalFileId
                        ? "asset:" + (info.Path ?? info.Guid) + "#" + info.LocalFileId.ToString(CultureInfo.InvariantCulture)
                        : "asset:" + (info.Path ?? info.Guid);
                    return descriptor;
                }

                descriptor["kind"] = "unresolved";
                descriptor["id"] = null;
                descriptor["identity_basis"] = "none";
                descriptor["reason"] = "not_in_loaded_scene_or_asset";
                return descriptor;
            }

            private Dictionary<string, object> DescribeAction(InputAction action)
            {
                var identity = new Dictionary<string, object> { ["kind"] = "InputAction" };
                AddActionIdentity(identity, action);
                AddActionMapIdentity(identity, action, action.actionMap);
                return identity;
            }

            private Dictionary<string, object> DescribeReference(InputActionReference reference)
            {
                var identity = new Dictionary<string, object> { ["kind"] = "InputActionReference" };
                AssetInfo referenceAsset = GetAssetInfo(reference);
                if (referenceAsset != null)
                {
                    identity["reference_asset_name"] = string.IsNullOrEmpty(referenceAsset.Name) ? null : referenceAsset.Name;
                    identity["reference_asset_path"] = string.IsNullOrEmpty(referenceAsset.Path) ? null : referenceAsset.Path;
                    identity["reference_asset_guid"] = string.IsNullOrEmpty(referenceAsset.Guid) ? null : referenceAsset.Guid;
                }

                InputActionAsset asset = null;
                InputAction action = null;
                try
                {
                    asset = reference.asset;
                }
                catch (Exception ex)
                {
                    _result.Errors.Add("input reference " + reference.name + ": asset: " + Describe(ex));
                }

                try
                {
                    action = reference.action;
                }
                catch (Exception ex)
                {
                    _result.Errors.Add("input reference " + reference.name + ": action: " + Describe(ex));
                }

                if (asset != null) AddAssetIdentity(identity, asset);
                if (action != null)
                {
                    AddActionIdentity(identity, action);
                    AddActionMapIdentity(identity, action, action.actionMap);
                }

                return identity;
            }

            private Dictionary<string, object> DescribeMap(InputActionMap map)
            {
                var identity = new Dictionary<string, object>
                {
                    ["kind"] = "InputActionMap",
                    ["map_id"] = map.id.ToString(),
                    ["map_name"] = map.name,
                };

                InputActionAsset asset = null;
                try
                {
                    asset = map.asset;
                }
                catch (Exception ex)
                {
                    _result.Errors.Add("input map " + map.name + ": asset: " + Describe(ex));
                }

                if (asset != null) AddAssetIdentity(identity, asset);
                identity["action_count"] = TryInt(() => map.actions.Count);
                identity["binding_count"] = TryInt(() => map.bindings.Count);
                return identity;
            }

            private Dictionary<string, object> DescribeAsset(InputActionAsset asset)
            {
                var identity = new Dictionary<string, object> { ["kind"] = "InputActionAsset" };
                AddAssetIdentity(identity, asset);
                int? mapCount = TryInt(() => asset.actionMaps.Count);
                identity["map_count"] = mapCount;
                if (mapCount.HasValue)
                {
                    int actionCount = 0;
                    int bindingCount = 0;
                    bool failed = false;
                    try
                    {
                        foreach (InputActionMap map in asset.actionMaps)
                        {
                            actionCount += map.actions.Count;
                            bindingCount += map.bindings.Count;
                        }
                    }
                    catch (Exception ex)
                    {
                        failed = true;
                        _result.Errors.Add("input asset " + asset.name + ": counts: " + Describe(ex));
                    }

                    identity["action_count"] = failed ? (int?)null : actionCount;
                    identity["binding_count"] = failed ? (int?)null : bindingCount;
                }
                else
                {
                    identity["action_count"] = null;
                    identity["binding_count"] = null;
                }

                return identity;
            }

            private Dictionary<string, object> DescribeProperty(InputActionProperty property)
            {
                var identity = new Dictionary<string, object> { ["kind"] = "InputActionProperty" };
                InputActionReference reference = null;
                InputAction action = null;
                try
                {
                    reference = property.reference;
                }
                catch (Exception ex)
                {
                    _result.Errors.Add("input property: reference: " + Describe(ex));
                }

                try
                {
                    action = property.action;
                }
                catch (Exception ex)
                {
                    _result.Errors.Add("input property: action: " + Describe(ex));
                }

                identity["has_reference"] = reference != null;
                identity["has_action"] = action != null;
                if (reference != null)
                {
                    AssetInfo referenceAsset = GetAssetInfo(reference);
                    if (referenceAsset != null)
                    {
                        identity["reference_asset_name"] =
                            string.IsNullOrEmpty(referenceAsset.Name) ? null : referenceAsset.Name;
                        identity["reference_asset_path"] =
                            string.IsNullOrEmpty(referenceAsset.Path) ? null : referenceAsset.Path;
                        identity["reference_asset_guid"] =
                            string.IsNullOrEmpty(referenceAsset.Guid) ? null : referenceAsset.Guid;
                    }
                }

                if (action != null)
                {
                    AddActionIdentity(identity, action);
                    AddActionMapIdentity(identity, action, action.actionMap);
                }
                else
                {
                    identity["binding_count"] = 0;
                }

                return identity;
            }

            private void AddActionIdentity(Dictionary<string, object> identity, InputAction action)
            {
                identity["action_id"] = action.id.ToString();
                identity["action_name"] = action.name;
                identity["action_type"] = Enum.GetName(typeof(InputActionType), action.type) ??
                                          Convert.ToInt32(action.type, CultureInfo.InvariantCulture)
                                              .ToString(CultureInfo.InvariantCulture);
                identity["expected_control_type"] = action.expectedControlType;
                identity["action_interactions"] = action.interactions;
                identity["action_processors"] = action.processors;
            }

            private void AddActionMapIdentity(Dictionary<string, object> identity, InputAction action,
                InputActionMap map)
            {
                if (map == null) return;
                identity["map_id"] = map.id.ToString();
                identity["map_name"] = map.name;
                InputActionAsset asset = null;
                try
                {
                    asset = map.asset;
                }
                catch (Exception ex)
                {
                    _result.Errors.Add("input map " + map.name + ": asset: " + Describe(ex));
                }

                if (asset != null) AddAssetIdentity(identity, asset);
            }

            private void AddAssetIdentity(Dictionary<string, object> identity, InputActionAsset asset)
            {
                AssetInfo info = GetAssetInfo(asset);
                if (info == null) return;
                identity["asset_name"] = string.IsNullOrEmpty(info.Name) ? null : info.Name;
                identity["asset_path"] = string.IsNullOrEmpty(info.Path) ? null : info.Path;
                identity["asset_guid"] = string.IsNullOrEmpty(info.Guid) ? null : info.Guid;
            }

            private int ExtractBindings(object value, FieldSource source, ref int returned)
            {
                int count = 0;
                string inputKind = source.InputKind;
                if (inputKind == null)
                {
                    if (value is InputAction) inputKind = "InputAction";
                    else if (value is InputActionReference) inputKind = "InputActionReference";
                    else if (value is InputActionMap) inputKind = "InputActionMap";
                    else if (value is InputActionAsset) inputKind = "InputActionAsset";
                    else if (value is InputActionProperty) inputKind = "InputActionProperty";
                }

                switch (inputKind)
                {
                    case "InputAction":
                        if (value is InputAction action)
                        {
                            count += AddActionBindings(action, source, ref returned);
                        }

                        break;
                    case "InputActionReference":
                        if (value is InputActionReference reference)
                        {
                            InputAction referenced = reference.action;
                            if (referenced != null) count += AddActionBindings(referenced, source, ref returned);
                        }

                        break;
                    case "InputActionMap":
                        if (value is InputActionMap map)
                        {
                            count += AddMapBindings(map, source, ref returned);
                        }

                        break;
                    case "InputActionAsset":
                        if (value is InputActionAsset asset)
                        {
                            foreach (InputActionMap assetMap in asset.actionMaps)
                            {
                                count += AddMapBindings(assetMap, source, ref returned);
                            }
                        }

                        break;
                    case "InputActionProperty":
                        if (value is InputActionProperty property)
                        {
                            InputAction propertyAction = property.action;
                            if (propertyAction != null) count += AddActionBindings(propertyAction, source, ref returned);
                        }

                        break;
                }

                return count;
            }

            private int AddActionBindings(InputAction action, FieldSource source, ref int returned)
            {
                InputActionMap map = null;
                try
                {
                    map = action.actionMap;
                }
                catch (Exception ex)
                {
                    _result.Errors.Add("field " + source.FieldId + ": action map: " + Describe(ex));
                }

                int index = 0;
                int count = 0;
                foreach (InputBinding binding in action.bindings)
                {
                    AddBinding(binding, index, "action", action, map, source, ref returned);
                    index++;
                    count++;
                }

                return count;
            }

            private int AddMapBindings(InputActionMap map, FieldSource source, ref int returned)
            {
                InputActionAsset asset = null;
                try
                {
                    asset = map.asset;
                }
                catch (Exception ex)
                {
                    _result.Errors.Add("field " + source.FieldId + ": map asset: " + Describe(ex));
                }

                int index = 0;
                int count = 0;
                foreach (InputBinding binding in map.bindings)
                {
                    InputAction action = null;
                    if (!string.IsNullOrEmpty(binding.action))
                    {
                        try
                        {
                            action = map.FindAction(binding.action, false);
                        }
                        catch (Exception ex)
                        {
                            _result.Errors.Add("field " + source.FieldId + ": binding action: " + Describe(ex));
                        }
                    }

                    AddBinding(binding, index, "map", action, map, source, ref returned);
                    index++;
                    count++;
                }

                return count;
            }

            private void AddBinding(InputBinding binding, int index, string scope, InputAction action,
                InputActionMap map, FieldSource source, ref int returned)
            {
                _bindingTotal++;
                if (_bindings.Count >= _maxBindings) return;

                Guid actionId = action == null ? Guid.Empty : action.id;
                Guid bindingId = binding.id;
                string key = bindingId != Guid.Empty
                    ? bindingId.ToString()
                    : actionId.ToString() + ":" + index.ToString(CultureInfo.InvariantCulture);
                string assetPath = null;
                string assetName = null;
                string assetGuid = null;
                InputActionAsset asset = null;
                try
                {
                    asset = map == null ? null : map.asset;
                }
                catch (Exception ex)
                {
                    _result.Errors.Add("field " + source.FieldId + ": binding asset: " + Describe(ex));
                }

                if (asset != null)
                {
                    AssetInfo info = GetAssetInfo(asset);
                    if (info != null)
                    {
                        assetPath = string.IsNullOrEmpty(info.Path) ? null : info.Path;
                        assetName = string.IsNullOrEmpty(info.Name) ? null : info.Name;
                        assetGuid = string.IsNullOrEmpty(info.Guid) ? null : info.Guid;
                    }
                }

                var entry = new Dictionary<string, object>
                {
                    ["id"] = source.FieldId + "::binding:" + key,
                    ["source_field_id"] = source.FieldId,
                    ["source_field_name"] = source.FieldName,
                    ["source_declaring_type"] = source.DeclaringType,
                    ["source_declaring_assembly"] = source.DeclaringAssembly,
                    ["source_field_type"] = source.FieldType,
                    ["source_component_id"] = source.ComponentId,
                    ["source_object_id"] = source.ObjectId,
                    ["source_scene_id"] = source.SceneId,
                    ["source_input_kind"] = source.InputKind,
                    ["binding_scope"] = scope,
                    ["binding_index"] = index,
                    ["asset_name"] = assetName,
                    ["asset_path"] = assetPath,
                    ["asset_guid"] = assetGuid,
                    ["map_id"] = map == null ? null : map.id.ToString(),
                    ["map_name"] = map == null ? null : map.name,
                    ["action_id"] = action == null ? null : action.id.ToString(),
                    ["action_name"] = action == null ? null : action.name,
                    ["action_type"] = action == null
                        ? null
                        : (Enum.GetName(typeof(InputActionType), action.type) ??
                           Convert.ToInt32(action.type, CultureInfo.InvariantCulture)
                               .ToString(CultureInfo.InvariantCulture)),
                    ["expected_control_type"] = action == null ? null : action.expectedControlType,
                    ["action_interactions"] = action == null ? null : action.interactions,
                    ["action_processors"] = action == null ? null : action.processors,
                    ["binding_id"] = bindingId == Guid.Empty ? string.Empty : bindingId.ToString(),
                    ["binding_name"] = binding.name,
                    ["binding_path"] = binding.path,
                    ["binding_override_path"] = binding.overridePath,
                    ["binding_effective_path"] = binding.effectivePath,
                    ["binding_interactions"] = binding.interactions,
                    ["binding_processors"] = binding.processors,
                    ["binding_groups"] = binding.groups,
                    ["binding_action_name"] = binding.action,
                    ["binding_is_composite"] = binding.isComposite,
                    ["binding_is_part_of_composite"] = binding.isPartOfComposite,
                };

                _bindings.Add(entry);
                returned++;
            }

            private AssetInfo GetAssetInfo(UnityEngine.Object asset)
            {
                if (asset == null) return null;
                if (_assetInfo.TryGetValue(asset, out AssetInfo cached)) return cached;

                var info = new AssetInfo();
                try
                {
                    info.Name = asset.name;
                }
                catch (Exception ex)
                {
                    _result.Errors.Add("asset: name: " + Describe(ex));
                }

                try
                {
                    info.Path = AssetDatabase.GetAssetPath(asset);
                }
                catch (Exception ex)
                {
                    _result.Errors.Add("asset: path: " + Describe(ex));
                }

                try
                {
                    if (AssetDatabase.TryGetGUIDAndLocalFileIdentifier(asset, out string guid, out long localId))
                    {
                        info.Guid = guid;
                        info.HasLocalFileId = true;
                        info.LocalFileId = localId;
                    }
                    else if (!string.IsNullOrEmpty(info.Path))
                    {
                        info.Guid = AssetDatabase.AssetPathToGUID(info.Path);
                    }
                }
                catch (Exception ex)
                {
                    _result.Errors.Add("asset: guid: " + Describe(ex));
                }

                _assetInfo[asset] = info;
                return info;
            }

            private void Finish()
            {
                bool objectFailure = _sceneFailure || _objectFailure;
                bool upstreamFailure = objectFailure || _componentFailure;
                bool componentsComplete = !upstreamFailure && !_scenesTruncated && !_objectsTruncated;
                bool fieldsFailure = upstreamFailure || _fieldFailure;
                bool fieldsComplete = !fieldsFailure && componentsComplete && _componentTotal == _components.Count;
                bool bindingsFailure = fieldsFailure || _bindingFailure;
                bool bindingsComplete = !bindingsFailure && fieldsComplete && _fieldTotal == _fields.Count;

                AddCap("scenes", SceneLimit, _sceneTotal, _scenes.Count, !_sceneFailure,
                    _sceneTotal > _scenes.Count, _sceneFailure);
                AddCap("game_objects", ObjectLimit, _objectTotal, _objects.Count,
                    !objectFailure && !_scenesTruncated && !_objectsTruncated,
                    _scenesTruncated || _objectsTruncated || _objectTotal > _objects.Count,
                    objectFailure);
                AddCap("components", _maxComponents, _componentTotal, _components.Count,
                    !upstreamFailure && !_scenesTruncated && !_objectsTruncated,
                    _scenesTruncated || _objectsTruncated || _componentTotal > _components.Count,
                    upstreamFailure);
                AddCap("fields", _maxFields, _fieldTotal, _fields.Count,
                    fieldsComplete && _componentTotal == _components.Count,
                    _scenesTruncated || _objectsTruncated || _componentTotal > _components.Count ||
                    _fieldTotal > _fields.Count,
                    fieldsFailure);
                AddCap("bindings", _maxBindings, _bindingTotal, _bindings.Count,
                    bindingsComplete && _fieldTotal == _fields.Count,
                    _scenesTruncated || _objectsTruncated || _componentTotal > _components.Count ||
                    _fieldTotal > _fields.Count || _bindingTotal > _bindings.Count,
                    bindingsFailure);
            }

            private void AddCap(string name, int limit, int observed, int returned, bool known, bool truncated,
                bool failure)
            {
                _result.Caps[name] = new SceneCap
                {
                    Limit = limit,
                    Observed = known && !failure ? observed : (int?)null,
                    Returned = returned,
                    Truncated = failure ? (bool?)null : truncated,
                };
            }

            private static string FieldId(string componentId, FieldInfo field)
            {
                return componentId + "::field:" + (AssemblyName(field.DeclaringType) ?? "?") + "::" +
                       (TypeName(field.DeclaringType) ?? "?") + "::" + field.Name;
            }

            private static bool IsManagedBehaviour(Type type)
            {
                return type != null && type != typeof(MonoBehaviour) && typeof(MonoBehaviour).IsAssignableFrom(type);
            }

            private bool? SerializedState(Component component, FieldInfo field, string fieldId)
            {
                try
                {
                    int matching = 0;
                    for (Type current = component.GetType(); IsManagedBehaviour(current); current = current.BaseType)
                        if (current.GetField(field.Name, BindingFlags.Instance | BindingFlags.Public |
                            BindingFlags.NonPublic | BindingFlags.DeclaredOnly) != null) matching++;
                    if (matching > 1)
                    {
                        _result.Errors.Add("field " + fieldId + ": serialization is ambiguous across base types");
                        return null;
                    }
                    using (var serialized = new SerializedObject(component))
                        return serialized.FindProperty(field.Name) != null;
                }
                catch (Exception ex)
                {
                    _result.Errors.Add("field " + fieldId + ": serialized state: " + Describe(ex));
                    return null;
                }
            }

            private static string FieldTypeKind(Type type)
            {
                if (InputKind(type) != null) return "input_action";
                if (type != null && typeof(UnityEngine.Object).IsAssignableFrom(type)) return "unity_object";
                if (type != null && type.IsEnum) return "enum";
                if (type == typeof(string) || type == typeof(bool) || type == typeof(char) ||
                    type == typeof(sbyte) || type == typeof(byte) || type == typeof(short) ||
                    type == typeof(ushort) || type == typeof(int) || type == typeof(uint) ||
                    type == typeof(long) || type == typeof(ulong) || type == typeof(float) ||
                    type == typeof(double) || type == typeof(decimal)) return "primitive";
                return "unsupported";
            }

            private static string InputKind(Type type)
            {
                if (type == null) return null;
                if (type == typeof(InputAction)) return "InputAction";
                if (type == typeof(InputActionReference)) return "InputActionReference";
                if (type == typeof(InputActionMap)) return "InputActionMap";
                if (type == typeof(InputActionAsset)) return "InputActionAsset";
                if (type == typeof(InputActionProperty)) return "InputActionProperty";
                return null;
            }

            private static string Visibility(FieldInfo field)
            {
                if (field.IsPublic) return "public";
                if (field.IsPrivate) return "private";
                if (field.IsFamily) return "protected";
                if (field.IsAssembly) return "internal";
                if (field.IsFamilyOrAssembly) return "protected_internal";
                if (field.IsFamilyAndAssembly) return "private_protected";
                return "unknown";
            }

            private static List<object> FormerlySerializedNames(FieldInfo field)
            {
                var names = new List<object>();
                object[] attributes = field.GetCustomAttributes(typeof(FormerlySerializedAsAttribute), false);
                foreach (object attribute in attributes)
                {
                    if (attribute is FormerlySerializedAsAttribute formerly && !string.IsNullOrEmpty(formerly.oldName))
                    {
                        names.Add(formerly.oldName);
                    }
                }

                return names;
            }

            private static string TypeName(Type type)
            {
                if (type == null) return null;
                return type.FullName ?? type.Name;
            }

            private static string AssemblyName(Type type)
            {
                if (type == null) return null;
                try
                {
                    return type.Assembly.GetName().Name;
                }
                catch (Exception)
                {
                    return null;
                }
            }

            private static string SpecialFloat(float value)
            {
                if (float.IsNaN(value)) return "NaN";
                return value > 0 ? "Infinity" : "-Infinity";
            }

            private static string SpecialDouble(double value)
            {
                if (double.IsNaN(value)) return "NaN";
                return value > 0 ? "Infinity" : "-Infinity";
            }

            private static bool? TryBool(Func<bool> read)
            {
                try
                {
                    return read();
                }
                catch (Exception)
                {
                    return null;
                }
            }

            private static int? TryInt(Func<int> read)
            {
                try
                {
                    return read();
                }
                catch (Exception)
                {
                    return null;
                }
            }

            private static string TryString(Func<string> read)
            {
                try
                {
                    return read();
                }
                catch (Exception)
                {
                    return null;
                }
            }

            private static string Describe(Exception exception)
            {
                if (exception == null) return "unknown";
                return exception.GetType().Name + ": " + exception.Message;
            }
        }

        private sealed class CollectedComponent
        {
            public readonly Component Component;
            public readonly Dictionary<string, object> Entry;
            public readonly string Id;

            public CollectedComponent(Component component, Dictionary<string, object> entry, string id)
            {
                Component = component;
                Entry = entry;
                Id = id;
            }
        }

        private sealed class FieldSource
        {
            public string FieldId;
            public string FieldName;
            public string DeclaringType;
            public string DeclaringAssembly;
            public string FieldType;
            public string ComponentId;
            public string ObjectId;
            public string SceneId;
            public string InputKind;
        }

        private sealed class AssetInfo
        {
            public string Name;
            public string Path;
            public string Guid;
            public bool HasLocalFileId;
            public long LocalFileId;
        }

        private sealed class FieldOrder : IComparer<FieldInfo>
        {
            public static readonly FieldOrder Instance = new FieldOrder();

            public int Compare(FieldInfo left, FieldInfo right)
            {
                if (ReferenceEquals(left, right)) return 0;
                if (left == null) return -1;
                if (right == null) return 1;
                int leftToken = Token(left);
                int rightToken = Token(right);
                int compared = leftToken.CompareTo(rightToken);
                return compared != 0 ? compared : string.CompareOrdinal(left.Name, right.Name);
            }

            private static int Token(FieldInfo field)
            {
                try
                {
                    return field.MetadataToken;
                }
                catch (Exception)
                {
                    return int.MaxValue;
                }
            }
        }

        private sealed class ReferenceComparer : IEqualityComparer<UnityEngine.Object>
        {
            public static readonly ReferenceComparer Instance = new ReferenceComparer();

            public bool Equals(UnityEngine.Object left, UnityEngine.Object right)
            {
                return ReferenceEquals(left, right);
            }

            public int GetHashCode(UnityEngine.Object value)
            {
                return RuntimeHelpers.GetHashCode(value);
            }
        }
    }
}
