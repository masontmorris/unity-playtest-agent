// SPDX-License-Identifier: Apache-2.0
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
    /// The fields stage is a repeated freshness sample: it returns the discovery GameObjects and
    /// fields, with the same ids and coverage, plus the sampling instant. These tests create objects
    /// in the open Editor scene and check that the sample joins discovery exactly, that it carries
    /// no source, IL or binding rows, that a capped sample records its coverage and that the stage
    /// refuses the wrong mode.
    /// </summary>
    public sealed class UnityFieldsInspectionTests
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
        public void Collect_JoinsDiscoveryIdsAndReturnsOnlySampleData()
        {
            GameObject parent = Track(new GameObject(Unique("parent")));
            GameObject child = Track(new GameObject(Unique("child")));
            child.transform.SetParent(parent.transform, false);
            child.SetActive(false);
            SceneDiscoveryProbe probe = parent.AddComponent<SceneDiscoveryProbe>();
            probe.Number = 42;

            SceneDiscoveryResult discovery = SceneDiscoveryCollector.Collect(10000, 10000, 10000);
            InspectionDocument sample = SceneFieldsCollector.Collect(10000, 10000, 10000);

            var discoveryObjectIds = new HashSet<string>(
                DiscoveryRows(discovery).Select(row => (string)row["id"]));
            var sampleObjectIds = new HashSet<string>(
                Rows(sample, "game_objects").Select(row => (string)row["id"]));
            Assert.That(sampleObjectIds.SetEquals(discoveryObjectIds), Is.True,
                "the fields stage must return exactly the discovery object ids");

            var discoveryFieldIds = new HashSet<string>(
                DiscoveryRows(discovery, "fields").Select(row => (string)row["id"]));
            var sampleFieldIds = new HashSet<string>(
                Rows(sample, "fields").Select(row => (string)row["id"]));
            Assert.That(sampleFieldIds.SetEquals(discoveryFieldIds), Is.True,
                "the fields stage must return exactly the discovery field ids");

            Dictionary<string, object> parentRow = DiscoveryNamed(discovery, parent.name);
            Dictionary<string, object> number = Rows(sample, "fields").Single(row =>
                (string)row["name"] == "Number" &&
                (string)row["object_id"] == (string)parentRow["id"]);
            Assert.That(number["value"], Is.EqualTo(42L));

            Assert.That(sample.Data.Keys, Is.EquivalentTo(new[]
            {
                "time_unit", "frame", "real_time_s", "game_time_s", "sample_wall_s", "keyboard_state", "input_state", "game_objects", "fields",
            }), "the fields stage carries only the sample rows, its instant and the input state");
            Assert.That(sample.Caps.Keys, Is.EquivalentTo(new[] { "game_objects", "fields" }));
            Assert.That(sample.Data["time_unit"], Is.EqualTo("seconds"));
            Assert.That(sample.Data["frame"], Is.TypeOf<int>());
            Assert.That((int)sample.Data["frame"], Is.GreaterThanOrEqualTo(0));
            Assert.That(sample.Data["real_time_s"], Is.TypeOf<double>());
            Assert.That(sample.Data["game_time_s"], Is.TypeOf<double>());

            AssertCap(sample.Caps["game_objects"], discovery.Caps["game_objects"]);
            AssertCap(sample.Caps["fields"], discovery.Caps["fields"]);
        }

        [Test]
        public void Collect_ReportsCoverageWhenFieldCapBinds()
        {
            Track(new GameObject(Unique("probe"))).AddComponent<SceneDiscoveryProbe>();

            InspectionDocument sample = SceneFieldsCollector.Collect(10000, 1, 10000);

            InspectionCap fields = sample.Caps["fields"];
            Assert.That(fields.Limit, Is.EqualTo(1));
            Assert.That(fields.Returned, Is.EqualTo(1));
            Assert.That(fields.Observed, Is.GreaterThan(1));
            Assert.That(fields.Truncated, Is.True);
            Assert.That(Rows(sample, "fields"), Has.Count.EqualTo(1));
            Assert.That(sample.Caps.ContainsKey("game_objects"), Is.True);
        }

        [Test]
        public void Collect_RejectsNonPositiveLimits()
        {
            Assert.Throws<ArgumentOutOfRangeException>(() => SceneFieldsCollector.Collect(0, 1, 1));
            Assert.Throws<ArgumentOutOfRangeException>(() => SceneFieldsCollector.Collect(1, 0, 1));
            Assert.Throws<ArgumentOutOfRangeException>(() => SceneFieldsCollector.Collect(1, 1, 0));
            Assert.Throws<ArgumentOutOfRangeException>(() => SceneFieldsCollector.Collect(-1, 1, 1));
        }

        [UnityTest]
        public IEnumerator FieldsStageRequiresPlayMode()
        {
            using (var source = new EditorInspectionSource())
            {
                Task<BridgeInspectionSnapshot> pending = Task.Run(() => source.Inspect("fields"));
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
            return DiscoveryRows(result, "game_objects");
        }

        private static List<Dictionary<string, object>> DiscoveryRows(SceneDiscoveryResult result, string key)
        {
            return ((List<object>)result.Data[key]).Cast<Dictionary<string, object>>().ToList();
        }

        private static List<Dictionary<string, object>> Rows(InspectionDocument document, string key)
        {
            return ((List<object>)document.Data[key]).Cast<Dictionary<string, object>>().ToList();
        }

        private static Dictionary<string, object> DiscoveryNamed(SceneDiscoveryResult result, string name)
        {
            return DiscoveryRows(result).Single(row => (string)row["name"] == name);
        }

        private static void AssertCap(InspectionCap sample, SceneCap discovery)
        {
            Assert.That(sample.Limit, Is.EqualTo(discovery.Limit));
            Assert.That(sample.Observed, Is.EqualTo(discovery.Observed));
            Assert.That(sample.Returned, Is.EqualTo(discovery.Returned));
            Assert.That(sample.Truncated, Is.EqualTo(discovery.Truncated));
        }
    }
}
