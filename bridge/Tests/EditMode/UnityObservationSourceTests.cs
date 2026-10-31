// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections;
using System.Threading.Tasks;
using NUnit.Framework;
using Playtest.Bridge.Transport;
using Playtest.Bridge.Unity;
using UnityEngine;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    [TestFixture]
    public sealed class UnityObservationSourceTests
    {
        private UnityObservationSource _source;

        private float _originalTimeScale;

        [SetUp]
        public void SetUp()
        {
            _originalTimeScale = Time.timeScale;
            Time.timeScale = 1f;
            _source = new UnityObservationSource();
        }

        [TearDown]
        public void TearDown()
        {
            if (_source != null)
            {
                _source.Dispose();
                _source = null;
            }

            Time.timeScale = _originalTimeScale;
        }

        [Test]
        public void GetObservation_ReturnsASnapshotBeforeStart()
        {
            BridgeObservationSnapshot snapshot = _source.GetObservation();

            Assert.That(snapshot, Is.Not.Null);
            Assert.That(snapshot.Scene, Is.Not.Null.And.Not.Empty);
            Assert.That(snapshot.ClockMode, Is.EqualTo("realtime"));
            Assert.That(snapshot.Ui, Is.Not.Null);
        }

        [Test]
        public void Start_IsIdempotentAndSamplesImmediately()
        {
            BridgeObservationSnapshot before = _source.GetObservation();

            _source.Start();
            BridgeObservationSnapshot first = _source.GetObservation();
            _source.Start();
            BridgeObservationSnapshot second = _source.GetObservation();

            Assert.That(first, Is.Not.SameAs(before), "Start must sample synchronously");
            Assert.That(second, Is.SameAs(first), "a second Start must not disturb the cache");
        }

        [Test]
        public void Stop_IsIdempotentAndKeepsTheLatestSnapshot()
        {
            _source.Start();
            BridgeObservationSnapshot sampled = _source.GetObservation();

            _source.Stop();
            _source.Stop();

            Assert.That(_source.GetObservation(), Is.SameAs(sampled));
        }

        [Test]
        public void Snapshot_HasTheGenericBaseline()
        {
            _source.Start();

            BridgeObservationSnapshot snapshot = _source.GetObservation();

            Assert.That(snapshot.Scene, Is.Not.Null.And.Not.Empty);
            Assert.That(snapshot.ClockMode, Is.EqualTo("realtime"));
            Assert.That(snapshot.Frame, Is.GreaterThan(0));
            Assert.That(snapshot.TimeScale, Is.EqualTo(1.0).Within(0.0001));
            Assert.That(snapshot.Playing, Is.EqualTo(Application.isPlaying));
            Assert.That(snapshot.Public, Is.Not.Null);
            Assert.That(snapshot.Ui, Is.Not.Null);
            Assert.That(snapshot.Ui.ContainsKey("canvases"), Is.True);
            Assert.That(snapshot.Ui["canvases"], Is.InstanceOf<IList>());
            Assert.That(snapshot.ReaderErrors, Is.Not.Null);
        }

        [UnityTest]
        public IEnumerator Sampling_RefreshesWhileTheEditorUpdates()
        {
            _source.Start();
            BridgeObservationSnapshot before = _source.GetObservation();
            long frameBefore = before.Frame;
            DateTime wallBefore = before.CapturedWallUtc;
            bool advanced = false;

            for (int i = 0; i < 20 && !advanced; i++)
            {
                yield return null;

                BridgeObservationSnapshot current = _source.GetObservation();
                Assert.That(current, Is.Not.Null);
                advanced = current.Frame > frameBefore || current.CapturedWallUtc > wallBefore;
            }

            Assert.That(advanced, Is.True, "the observation cache must stay fresh across frames");
        }

        [Test]
        public void GetObservation_IsSafeFromABackgroundThread()
        {
            _source.Start();
            BridgeObservationSnapshot cached = _source.GetObservation();

            BridgeObservationSnapshot fromTask = Task.Run(() => _source.GetObservation()).GetAwaiter().GetResult();

            Assert.That(fromTask, Is.Not.Null);
            Assert.That(fromTask.Scene, Is.EqualTo(cached.Scene));
            Assert.That(fromTask.Ui, Is.SameAs(cached.Ui));
        }

        [Test]
        public void Camera_FactsArePublishedUnderPublicOnly()
        {
            Camera camera = Camera.main;
            GameObject created = null;
            if (camera == null)
            {
                created = new GameObject("ObservationTestCamera");
                created.tag = "MainCamera";
                camera = created.AddComponent<Camera>();
                camera.fieldOfView = 60f;
            }

            try
            {
                _source.Start();
                BridgeObservationSnapshot snapshot = _source.GetObservation();

                Assert.That(snapshot.Public.ContainsKey("camera"), Is.True, "a camera must be published under public");
                Assert.That(snapshot.Ui.ContainsKey("camera"), Is.False, "ui must only carry canvases");

                var facts = snapshot.Public["camera"] as IDictionary;
                Assert.That(facts, Is.Not.Null);
                Assert.That(facts["name"], Is.Not.Null.And.Not.Empty);
                Assert.That(facts["position"], Is.InstanceOf<IList>());
                Assert.That((IList)facts["position"], Has.Count.EqualTo(3));
                Assert.That(facts["rotation"], Is.InstanceOf<IList>());
                Assert.That((IList)facts["rotation"], Has.Count.EqualTo(3));
                Assert.That(facts["field_of_view"], Is.InstanceOf<float>());
                Assert.That(facts["orthographic"], Is.InstanceOf<bool>());
            }
            finally
            {
                if (created != null)
                {
                    UnityEngine.Object.DestroyImmediate(created);
                }
            }
        }

        [Test]
        public void Probe_WithAnAnchor_IsPublishedWithNineSamplesAndTwoBlockers()
        {
            var anchorObject = new GameObject("ObservationTestAnchor");
            string originalPath = Environment.GetEnvironmentVariable(UnityProbeField.AnchorVariable);
            Environment.SetEnvironmentVariable(UnityProbeField.AnchorVariable, anchorObject.name);
            try
            {
                using (var source = new UnityObservationSource(probeRaycasts: new SilentRaycasts()))
                {
                    source.Start();
                    BridgeObservationSnapshot snapshot = source.GetObservation();

                    Assert.That(snapshot.Public.ContainsKey("probe"), Is.True, Describe(snapshot));
                    var probe = snapshot.Public["probe"] as IDictionary;
                    Assert.That(probe, Is.Not.Null);
                    Assert.That(probe["version"], Is.EqualTo(1));
                    Assert.That(probe["under"], Is.Not.Null);
                    Assert.That(probe["samples"], Is.InstanceOf<IList>());
                    Assert.That((IList)probe["samples"], Has.Count.EqualTo(9));
                    Assert.That(probe["blockers"], Is.InstanceOf<IList>());
                    Assert.That((IList)probe["blockers"], Has.Count.EqualTo(2));
                    Assert.That(snapshot.ReaderErrors.Exists(entry => entry.Reader == "probe"), Is.False,
                        "a sampled probe has no reader error");
                }
            }
            finally
            {
                Environment.SetEnvironmentVariable(UnityProbeField.AnchorVariable, originalPath);
                UnityEngine.Object.DestroyImmediate(anchorObject);
            }
        }

        [Test]
        public void Probe_WhenTheAnchorCannotBeResolved_IsOmittedWithAReaderError()
        {
            string originalPath = Environment.GetEnvironmentVariable(UnityProbeField.AnchorVariable);
            Environment.SetEnvironmentVariable(UnityProbeField.AnchorVariable, "ProbeTestNoSuchObject");
            try
            {
                using (var source = new UnityObservationSource(probeRaycasts: new SilentRaycasts()))
                {
                    source.Start();
                    BridgeObservationSnapshot snapshot = source.GetObservation();

                    Assert.That(snapshot.Public.ContainsKey("probe"), Is.False, "an anchorless probe is omitted");
                    var error = snapshot.ReaderErrors.Find(entry => entry.Reader == "probe");
                    Assert.That(error, Is.Not.Null, Describe(snapshot));
                    Assert.That(error.Error, Does.Contain("names no GameObject"));
                }
            }
            finally
            {
                Environment.SetEnvironmentVariable(UnityProbeField.AnchorVariable, originalPath);
            }
        }

        private static string Describe(BridgeObservationSnapshot snapshot)
        {
            return string.Join("; ", snapshot.ReaderErrors.ConvertAll(entry => entry.Reader + ": " + entry.Error));
        }

        /// <summary>A seam with no colliders, so an observation carries the probe shape only.</summary>
        private sealed class SilentRaycasts : IProbeRaycasts
        {
            public bool Raycast(Vector3 origin, Vector3 direction, float maxDistance, out ProbeRayHit hit)
            {
                hit = default(ProbeRayHit);
                return false;
            }

            public bool Overlap(Vector3 point, float radius)
            {
                return false;
            }
        }
    }
}
