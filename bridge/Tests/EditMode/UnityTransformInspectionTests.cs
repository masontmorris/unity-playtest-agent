using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using NUnit.Framework;
using Playtest.Bridge.Editor.Preflight;
using Playtest.Bridge.Transport;
using UnityEngine;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The transforms stage samples live scene positions without entering Play mode itself. These
    /// tests create objects in the open Editor scene and check that ids join discovery exactly,
    /// that inactive objects are included, that a capped sample records its coverage and that the
    /// stage refuses the wrong mode.
    /// </summary>
    public sealed class UnityTransformInspectionTests
    {
        private readonly List<GameObject> _objects = new List<GameObject>();

        [TearDown]
        public void TearDown()
        {
            foreach (GameObject item in _objects)
            {
                if (item != null) UnityEngine.Object.DestroyImmediate(item);
            }

            _objects.Clear();
        }

        [Test]
        public void Collect_JoinsDiscoveryIdsAndReportsWorldPositions()
        {
            GameObject parent = Track(new GameObject(Unique("parent")));
            parent.transform.position = new Vector3(10f, 2f, -3f);

            GameObject child = Track(new GameObject(Unique("child")));
            child.transform.SetParent(parent.transform, false);
            child.transform.localPosition = new Vector3(1f, 2f, 3f);
            child.SetActive(false);

            SceneDiscoveryResult discovery = SceneDiscoveryCollector.Collect(10000, 10000, 10000);
            InspectionDocument transforms = SceneTransformCollector.Collect(10000);

            Dictionary<string, object> discoveryParent = DiscoveryNamed(discovery, parent.name);
            Dictionary<string, object> discoveryChild = DiscoveryNamed(discovery, child.name);
            Dictionary<string, object> parentRow = TransformWithId(transforms, (string)discoveryParent["id"]);
            Dictionary<string, object> childRow = TransformWithId(transforms, (string)discoveryChild["id"]);

            Assert.That(childRow["parent_id"], Is.EqualTo(discoveryParent["id"]));
            Assert.That(childRow["scene_path"], Is.EqualTo(discoveryChild["scene_path"]));
            Assert.That(parentRow["parent_id"], Is.Null);
            Assert.That(parentRow["active_self"], Is.EqualTo(true));
            Assert.That(childRow["active_self"], Is.EqualTo(false));
            Assert.That(childRow["active_in_hierarchy"], Is.EqualTo(false));
            AssertPosition(parentRow, new Vector3(10f, 2f, -3f));
            AssertPosition(childRow, new Vector3(11f, 4f, 0f));

            parent.transform.rotation = Quaternion.Euler(0f, 90f, 0f);
            Dictionary<string, object> rotated = TransformWithId(SceneTransformCollector.Collect(10000), (string)discoveryParent["id"]);
            Assert.That(rotated["basis_state"], Is.EqualTo("present"));
            Assert.That((float[])rotated["world_forward"], Is.EqualTo(new[] { 1f, 0f, 0f }).Within(0.0001f));
            Assert.That((float[])rotated["world_right"], Is.EqualTo(new[] { 0f, 0f, -1f }).Within(0.0001f));

            var discoveryIds = new HashSet<string>(DiscoveryRows(discovery).Select(row => (string)row["id"]));
            var transformIds = new HashSet<string>(TransformRows(transforms).Select(row => (string)row["id"]));
            Assert.That(transformIds.SetEquals(discoveryIds), Is.True,
                "the transforms stage must return exactly the discovery object ids");
        }

        [Test]
        public void Collect_CapsTheSampleAndRecordsCoverage()
        {
            for (int i = 0; i < 3; i++) Track(new GameObject(Unique("object")));

            InspectionDocument document = SceneTransformCollector.Collect(1);
            InspectionCap cap = document.Caps["game_objects"];

            Assert.That(cap.Limit, Is.EqualTo(1));
            Assert.That(cap.Returned, Is.EqualTo(1));
            Assert.That(cap.Observed, Is.Null, "the total is unknown when traversal stops at the cap");
            Assert.That(cap.Truncated, Is.True);
            Assert.That(TransformRows(document), Has.Count.EqualTo(1));

            Assert.That(document.Data["selection"], Is.EqualTo("all_loaded_scenes_depth_first"));
            Assert.That(document.Data["position_space"], Is.EqualTo("world"));
            Assert.That(document.Data["position_unit"], Is.EqualTo("unity_world_unit"));
            Assert.That(document.Data["frame"], Is.TypeOf<int>());
            Assert.That((int)document.Data["frame"], Is.GreaterThanOrEqualTo(0));
            Assert.That(document.Data["real_time_s"], Is.TypeOf<double>());
            Assert.That(document.Data["game_time_s"], Is.TypeOf<double>());
        }

        [Test]
        public void Collect_RejectsNonPositiveLimits()
        {
            Assert.Throws<ArgumentOutOfRangeException>(() => SceneTransformCollector.Collect(0));
            Assert.Throws<ArgumentOutOfRangeException>(() => SceneTransformCollector.Collect(-1));
        }

        [UnityTest]
        public IEnumerator TransformsStageRequiresPlayMode()
        {
            using (var source = new EditorInspectionSource())
            {
                Task<BridgeInspectionSnapshot> pending = Task.Run(() => source.Inspect("transforms"));
                DateTime deadline = DateTime.UtcNow.AddSeconds(120);
                while (!pending.IsCompleted && DateTime.UtcNow < deadline) yield return null;
                Assert.That(pending.IsCompleted, Is.True, "the mode rejection did not complete");

                var error = Assert.Throws<BridgeInspectionUnavailableException>(
                    () => pending.GetAwaiter().GetResult());
                Assert.That(error.Message, Does.Contain("requires Play mode"));
            }
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

        private static List<Dictionary<string, object>> DiscoveryRows(SceneDiscoveryResult result)
        {
            return ((List<object>)result.Data["game_objects"]).Cast<Dictionary<string, object>>().ToList();
        }

        private static List<Dictionary<string, object>> TransformRows(InspectionDocument document)
        {
            return ((List<object>)document.Data["game_objects"]).Cast<Dictionary<string, object>>().ToList();
        }

        private static Dictionary<string, object> DiscoveryNamed(SceneDiscoveryResult result, string name)
        {
            return DiscoveryRows(result).Single(row => (string)row["name"] == name);
        }

        private static Dictionary<string, object> TransformWithId(InspectionDocument document, string id)
        {
            return TransformRows(document).Single(row => (string)row["id"] == id);
        }

        private static void AssertPosition(Dictionary<string, object> row, Vector3 expected)
        {
            Assert.That(row["position_state"], Is.EqualTo("present"));
            var position = (float[])row["position"];
            Assert.That(position, Has.Length.EqualTo(3));
            Assert.That(position[0], Is.EqualTo(expected.x).Within(0.0001f));
            Assert.That(position[1], Is.EqualTo(expected.y).Within(0.0001f));
            Assert.That(position[2], Is.EqualTo(expected.z).Within(0.0001f));
        }
    }
}
