using System;
using System.Collections.Generic;
using System.Linq;
using NUnit.Framework;
using Playtest.Bridge.Editor.Preflight;
using UnityEngine;
using UnityEngine.InputSystem;

namespace Playtest.Bridge.Tests
{
    [TestFixture]
    public sealed class SceneDiscoveryCollectorTests
    {
        private readonly List<GameObject> _objects = new List<GameObject>();
        private InputAction _inlineAction;

        [TearDown]
        public void TearDown()
        {
            if (_inlineAction != null)
            {
                _inlineAction.Dispose();
                _inlineAction = null;
            }

            foreach (GameObject item in _objects)
            {
                if (item != null) UnityEngine.Object.DestroyImmediate(item);
            }

            _objects.Clear();
        }

        [Test]
        public void Collect_DerivesDeterministicIdsAndCollectsFields()
        {
            string name = Unique("probe");
            SceneDiscoveryProbe probe = CreateProbe(name);
            probe.Number = 42;

            SceneDiscoveryResult first = SceneDiscoveryCollector.Collect(10000, 10000, 10000);
            SceneDiscoveryResult second = SceneDiscoveryCollector.Collect(10000, 10000, 10000);

            Dictionary<string, object> firstObject = ObjectNamed(first, name);
            Dictionary<string, object> secondObject = ObjectNamed(second, name);
            Assert.That(firstObject["id"], Is.EqualTo(secondObject["id"]));
            Assert.That((string)firstObject["id"], Does.StartWith("scene:"));

            Dictionary<string, object> firstComponent =
                ComponentOf(first, (string)firstObject["id"], typeof(SceneDiscoveryProbe));
            Dictionary<string, object> secondComponent =
                ComponentOf(second, (string)secondObject["id"], typeof(SceneDiscoveryProbe));
            Assert.That(firstComponent["id"], Is.EqualTo(secondComponent["id"]));

            Dictionary<string, object> number = FieldNamed(first, (string)firstComponent["id"], "Number");
            Assert.That(number["value"], Is.EqualTo(42L));
            Assert.That((string)number["declaring_type"], Is.EqualTo(typeof(SceneDiscoveryProbe).FullName));
            Assert.That(number["declaration_id"], Is.EqualTo(ReflectionIds.FieldId(typeof(SceneDiscoveryProbe).GetField("Number"))));
            Assert.That((string)number["id"], Does.Contain("::field:"));

            Dictionary<string, object> inherited = FieldNamed(first, (string)firstComponent["id"], "BaseValue");
            Assert.That(inherited["value"], Is.EqualTo(5L));
            Assert.That((string)inherited["declaring_type"], Is.EqualTo(typeof(SceneDiscoveryProbeBase).FullName));

            Dictionary<string, object> serialized =
                FieldNamed(first, (string)firstComponent["id"], "_serializedPrivate");
            Assert.That(serialized["visibility"], Is.EqualTo("private"));
            Assert.That(serialized["serialized"], Is.EqualTo(true));
            Assert.That(serialized["value"], Is.EqualTo(11L));

            Dictionary<string, object> nonSerialized =
                FieldNamed(first, (string)firstComponent["id"], "NonSerializedPublic");
            Assert.That(nonSerialized["serialized"], Is.EqualTo(false));
            Assert.That(nonSerialized["non_serialized"], Is.EqualTo(true));
            Assert.That(nonSerialized["value"], Is.EqualTo(13L));
        }

        [Test]
        public void Collect_IncludesInactiveChildrenAndTheirComponents()
        {
            GameObject parent = Track(new GameObject(Unique("parent")));
            GameObject child = Track(new GameObject(Unique("child")));
            child.transform.SetParent(parent.transform, false);
            child.SetActive(false);
            child.AddComponent<SceneDiscoveryProbe>();

            SceneDiscoveryResult result = SceneDiscoveryCollector.Collect(10000, 10000, 10000);

            Dictionary<string, object> parentObject = ObjectNamed(result, parent.name);
            Dictionary<string, object> childObject = ObjectNamed(result, child.name);
            Assert.That(childObject["parent_id"], Is.EqualTo(parentObject["id"]));
            Assert.That(childObject["sibling_index"], Is.EqualTo(0));
            Assert.That(childObject["depth"], Is.EqualTo(1));
            Assert.That(childObject["active_self"], Is.EqualTo(false));
            Assert.That(childObject["active_in_hierarchy"], Is.EqualTo(false));

            Dictionary<string, object> component =
                ComponentOf(result, (string)childObject["id"], typeof(SceneDiscoveryProbe));
            Assert.That(component["missing"], Is.EqualTo(false));
        }

        [Test]
        public void Collect_ReportsNullDestroyedAndUnsupportedValues()
        {
            SceneDiscoveryProbe probe = CreateProbe(Unique("probe"));
            probe.NullReference = null;
            GameObject destroyed = new GameObject(Unique("destroyed"));
            probe.DestroyedReference = destroyed;
            UnityEngine.Object.DestroyImmediate(destroyed);

            SceneDiscoveryResult result = SceneDiscoveryCollector.Collect(10000, 10000, 10000);
            Dictionary<string, object> component =
                ComponentOf(result, (string)ObjectNamed(result, probe.gameObject.name)["id"],
                    typeof(SceneDiscoveryProbe));

            Dictionary<string, object> nullField = FieldNamed(result, (string)component["id"], "NullReference");
            Assert.That(nullField["value_state"], Is.EqualTo("null"));
            Assert.That(nullField["value"], Is.Null);
            Assert.That(nullField["is_null"], Is.EqualTo(true));

            Dictionary<string, object> destroyedField =
                FieldNamed(result, (string)component["id"], "DestroyedReference");
            Assert.That(destroyedField["value_state"], Is.EqualTo("destroyed"));
            Assert.That(destroyedField["destroyed"], Is.EqualTo(true));

            Dictionary<string, object> unsupported =
                FieldNamed(result, (string)component["id"], "Unsupported");
            Assert.That(unsupported["value_state"], Is.EqualTo("unsupported"));
            Assert.That(unsupported["is_null"], Is.Null);
        }

        [Test]
        public void Collect_ReportsVectorsAndQuaternionsAsExactComponents()
        {
            string componentId = CollectVectorProbe(out SceneDiscoveryResult result);

            AssertComponents(FieldNamed(result, componentId, "Planar"), "vector2", new[] { 1.5f, -2.25f });
            AssertComponents(FieldNamed(result, componentId, "Velocity"), "vector3", new[] { 1f, 2f, 3f });
            AssertComponents(FieldNamed(result, componentId, "Tint"), "vector4", new[] { 0.25f, 0.5f, 0.75f, 1f });
            AssertComponents(FieldNamed(result, componentId, "Facing"), "quaternion", new[] { 0f, 0.5f, 0f, 0.5f });

            Dictionary<string, object> cell = FieldNamed(result, componentId, "Cell");
            Assert.That(cell["value_state"], Is.EqualTo("present"));
            Assert.That(cell["value_kind"], Is.EqualTo("vector2_int"));
            Assert.That((int[])cell["value"], Is.EqualTo(new[] { 3, -4 }));

            Dictionary<string, object> voxel = FieldNamed(result, componentId, "Voxel");
            Assert.That(voxel["value_kind"], Is.EqualTo("vector3_int"));
            Assert.That((int[])voxel["value"], Is.EqualTo(new[] { 5, 6, -7 }));
        }

        [Test]
        public void Collect_NamesNonFiniteVectorComponentsAndLeavesOtherStructsUnsupported()
        {
            string componentId = CollectVectorProbe(out SceneDiscoveryResult result);

            Dictionary<string, object> broken = FieldNamed(result, componentId, "Broken");
            Assert.That(broken["value_state"], Is.EqualTo("present"));
            Assert.That(broken["value_kind"], Is.EqualTo("special_vector"));
            Assert.That((object[])broken["value"], Is.EqualTo(new object[] { "NaN", 1f, "Infinity" }));

            Dictionary<string, object> paint = FieldNamed(result, componentId, "Paint");
            Assert.That(paint["value_state"], Is.EqualTo("unsupported"));
            Assert.That(paint["value"], Is.Null);
        }

        [Test]
        public void Collect_RejectsNonPositiveLimits()
        {
            Assert.Throws<ArgumentOutOfRangeException>(() => SceneDiscoveryCollector.Collect(0, 1, 1));
            Assert.Throws<ArgumentOutOfRangeException>(() => SceneDiscoveryCollector.Collect(1, 0, 1));
            Assert.Throws<ArgumentOutOfRangeException>(() => SceneDiscoveryCollector.Collect(1, 1, 0));
            Assert.Throws<ArgumentOutOfRangeException>(() => SceneDiscoveryCollector.Collect(-1, 1, 1));
        }

        [Test]
        public void Collect_ReportsCapsWhenLimitsBind()
        {
            SceneDiscoveryProbe probe = CreateProbe(Unique("probe"));
            probe.SetInlineAction(CreateInlineAction());

            SceneDiscoveryResult componentsResult = SceneDiscoveryCollector.Collect(1, 1000, 1000);
            SceneCap components = componentsResult.Caps["components"];
            Assert.That(components.Limit, Is.EqualTo(1));
            Assert.That(components.Returned, Is.EqualTo(1));
            Assert.That(components.Truncated, Is.EqualTo(true));
            Assert.That(componentsResult.Caps["fields"].Truncated, Is.EqualTo(true));
            Assert.That(componentsResult.Caps["bindings"].Truncated, Is.EqualTo(true));

            SceneDiscoveryResult fieldsResult = SceneDiscoveryCollector.Collect(1000, 1, 1000);
            SceneCap fields = fieldsResult.Caps["fields"];
            Assert.That(fields.Limit, Is.EqualTo(1));
            Assert.That(fields.Returned, Is.EqualTo(1));
            Assert.That(fields.Observed, Is.GreaterThan(1));
            Assert.That(fields.Truncated, Is.EqualTo(true));
            Assert.That(fieldsResult.Caps["bindings"].Truncated, Is.EqualTo(true));

            SceneDiscoveryResult bindingsResult = SceneDiscoveryCollector.Collect(1000, 1000, 1);
            SceneCap bindings = bindingsResult.Caps["bindings"];
            Assert.That(bindings.Limit, Is.EqualTo(1));
            Assert.That(bindings.Returned, Is.EqualTo(1));
            Assert.That(bindings.Observed, Is.GreaterThan(1));
            Assert.That(bindings.Truncated, Is.EqualTo(true));
        }

        [Test]
        public void Collect_ExtractsInlineInputActionBindings()
        {
            SceneDiscoveryProbe probe = CreateProbe(Unique("probe"));
            probe.SetInlineAction(CreateInlineAction());

            SceneDiscoveryResult result = SceneDiscoveryCollector.Collect(10000, 10000, 10000);
            Dictionary<string, object> component =
                ComponentOf(result, (string)ObjectNamed(result, probe.gameObject.name)["id"],
                    typeof(SceneDiscoveryProbe));
            Dictionary<string, object> field = FieldNamed(result, (string)component["id"], "_inlineAction");

            Assert.That(field["input_kind"], Is.EqualTo("InputAction"));
            Assert.That(field["value_state"], Is.EqualTo("present"));
            Assert.That(field["binding_count"], Is.EqualTo(4));

            List<Dictionary<string, object>> bindings = Rows(result, "bindings")
                .Where(row => (string)row["source_field_id"] == (string)field["id"]).ToList();
            Assert.That(bindings.Count, Is.EqualTo(4));

            Dictionary<string, object> direct =
                bindings.Single(row => (string)row["binding_path"] == "<Keyboard>/space");
            Assert.That(direct["binding_interactions"], Is.EqualTo("tap"));
            Assert.That(direct["binding_processors"], Is.EqualTo("stickDeadzone"));
            Assert.That(direct["binding_groups"], Is.EqualTo("Keyboard"));
            Assert.That(direct["binding_effective_path"], Is.EqualTo("<Keyboard>/space"));
            Assert.That(direct["action_name"], Is.EqualTo("discovery_action"));
            Assert.That(direct["source_field_name"], Is.EqualTo("_inlineAction"));

            Assert.That(bindings.Any(row => (bool)row["binding_is_composite"]), Is.True);
            Assert.That(bindings.Any(row => (bool)row["binding_is_part_of_composite"]), Is.True);
        }

        private SceneDiscoveryProbe CreateProbe(string name)
        {
            GameObject gameObject = Track(new GameObject(name));
            return gameObject.AddComponent<SceneDiscoveryProbe>();
        }

        private string CollectVectorProbe(out SceneDiscoveryResult result)
        {
            GameObject host = Track(new GameObject(Unique("vectors")));
            host.AddComponent<SceneVectorProbe>();
            result = SceneDiscoveryCollector.Collect(10000, 10000, 10000);
            return (string)ComponentOf(result, (string)ObjectNamed(result, host.name)["id"],
                typeof(SceneVectorProbe))["id"];
        }

        private static void AssertComponents(Dictionary<string, object> row, string kind, float[] expected)
        {
            Assert.That(row["value_state"], Is.EqualTo("present"));
            Assert.That(row["value_kind"], Is.EqualTo(kind));
            Assert.That(row["is_null"], Is.EqualTo(false));
            Assert.That((float[])row["value"], Is.EqualTo(expected));
        }

        private InputAction CreateInlineAction()
        {
            _inlineAction = new InputAction("discovery_action", InputActionType.Button);
            _inlineAction.AddBinding(new InputBinding
            {
                path = "<Keyboard>/space",
                interactions = "tap",
                processors = "stickDeadzone",
                groups = "Keyboard",
            });
            _inlineAction.AddCompositeBinding("2DVector")
                .With("Up", "<Keyboard>/w")
                .With("Down", "<Keyboard>/s");
            return _inlineAction;
        }

        private GameObject Track(GameObject gameObject)
        {
            _objects.Add(gameObject);
            return gameObject;
        }

        private static string Unique(string prefix)
        {
            return prefix + "-" + Guid.NewGuid().ToString("N");
        }

        private static List<Dictionary<string, object>> Rows(SceneDiscoveryResult result, string key)
        {
            return ((List<object>)result.Data[key]).Cast<Dictionary<string, object>>().ToList();
        }

        private static Dictionary<string, object> ObjectNamed(SceneDiscoveryResult result, string name)
        {
            return Rows(result, "game_objects").Single(row => (string)row["name"] == name);
        }

        private static Dictionary<string, object> ComponentOf(SceneDiscoveryResult result, string objectId,
            Type type)
        {
            return Rows(result, "components").Single(row =>
                (string)row["object_id"] == objectId && (string)row["type"] == type.FullName);
        }

        private static Dictionary<string, object> FieldNamed(SceneDiscoveryResult result, string componentId,
            string name)
        {
            return Rows(result, "fields").Single(row =>
                (string)row["component_id"] == componentId && (string)row["name"] == name);
        }
    }
}
