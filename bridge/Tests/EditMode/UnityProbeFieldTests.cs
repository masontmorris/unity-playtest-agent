// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections;
using System.Collections.Generic;
using NUnit.Framework;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;
using Playtest.Bridge.Unity;
using UnityEngine;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The probe: the 3x3 grid, the blocker casts, the anchor order and the rule that an unmeasured
    /// reading is null. Every ray comes from the fake seam, so the tests need no physics scene; only
    /// the anchor fixtures are real GameObjects.
    /// </summary>
    [TestFixture]
    public sealed class UnityProbeFieldTests
    {
        private const string AnchorName = "ProbeTestAnchor";

        private const float Tolerance = 1e-5f;

        private readonly List<GameObject> _created = new List<GameObject>();

        private FakeRaycasts _rays;

        private string _originalPath;

        [SetUp]
        public void SetUp()
        {
            _originalPath = Environment.GetEnvironmentVariable(UnityProbeField.AnchorVariable);
            Environment.SetEnvironmentVariable(UnityProbeField.AnchorVariable, null);
            _rays = new FakeRaycasts();
        }

        [TearDown]
        public void TearDown()
        {
            for (int i = 0; i < _created.Count; i++)
            {
                if (_created[i] != null)
                {
                    UnityEngine.Object.DestroyImmediate(_created[i]);
                }
            }

            _created.Clear();
            Environment.SetEnvironmentVariable(UnityProbeField.AnchorVariable, _originalPath);
        }

        [Test]
        public void Probe_ReportsTheShapeVersionAndTheAnchorInItsOwnFrame()
        {
            Transform anchor = CreateAnchor(new Vector3(1f, 2.5f, -3f));

            Dictionary<string, object> probe = new UnityProbeField(_rays).Sample(anchor, UnityProbeField.KindPath);

            Assert.That(probe["version"], Is.EqualTo(1));
            var facts = probe["anchor"] as IDictionary;
            Assert.That(facts, Is.Not.Null);
            Assert.That(facts["kind"], Is.EqualTo("path"));
            Assert.That(facts["name"], Is.EqualTo(AnchorName));
            var position = facts["position"] as IList;
            Assert.That(position, Has.Count.EqualTo(3));
            Assert.That((float)position[0], Is.EqualTo(1f).Within(Tolerance));
            Assert.That((float)position[1], Is.EqualTo(2.5f).Within(Tolerance));
            Assert.That((float)position[2], Is.EqualTo(-3f).Within(Tolerance));
        }

        [Test]
        public void Samples_AreAThreeByThreeGridInForwardThenLateralOrder()
        {
            Transform anchor = CreateAnchor(Vector3.zero);

            Dictionary<string, object> probe = new UnityProbeField(_rays).Sample(anchor, UnityProbeField.KindPath);

            var samples = probe["samples"] as IList;
            Assert.That(samples, Has.Count.EqualTo(9));

            float[] forward = { 0.6f, 1.2f, 1.8f };
            float[] lateral = { -0.35f, 0f, 0.35f };
            int index = 0;
            for (int forwardIndex = 0; forwardIndex < forward.Length; forwardIndex++)
            {
                for (int lateralIndex = 0; lateralIndex < lateral.Length; lateralIndex++)
                {
                    var entry = samples[index] as IDictionary;
                    Assert.That(entry, Is.Not.Null);
                    Assert.That((float)entry["forward"], Is.EqualTo(forward[forwardIndex]).Within(Tolerance));
                    Assert.That((float)entry["lateral"], Is.EqualTo(lateral[lateralIndex]).Within(Tolerance));
                    index++;
                }
            }
        }

        [Test]
        public void Samples_ReportDeltaYAboveTheGroundUnderTheAnchor()
        {
            Transform anchor = CreateAnchor(new Vector3(0f, 2.5f, 0f));
            _rays.RaycastHandler = (origin, direction, maxDistance) =>
            {
                if (maxDistance <= UnityProbeField.BlockerMaxDistance)
                {
                    return default(ProbeRayHit);
                }

                bool isUnder = Mathf.Abs(origin.x) < 1e-4f && Mathf.Abs(origin.z) < 1e-4f;
                float surface = isUnder ? 2.5f : 2.75f;
                return new ProbeRayHit
                {
                    Hit = true,
                    Point = new Vector3(origin.x, surface, origin.z),
                    Normal = Vector3.up,
                    Distance = origin.y - surface,
                };
            };

            Dictionary<string, object> probe = new UnityProbeField(_rays).Sample(anchor, UnityProbeField.KindPath);

            var under = probe["under"] as IDictionary;
            Assert.That(under, Is.Not.Null);
            Assert.That(under["hit"], Is.EqualTo(true));
            Assert.That(under["inside"], Is.EqualTo(false));
            Assert.That((double)under["delta_y"], Is.EqualTo(0.0).Within(1e-6),
                "the anchor stands on the floor it is measured against");

            var samples = probe["samples"] as IList;
            for (int i = 0; i < samples.Count; i++)
            {
                var entry = samples[i] as IDictionary;
                Assert.That(entry["hit"], Is.EqualTo(true));
                Assert.That(entry["inside"], Is.EqualTo(false));
                Assert.That((double)entry["delta_y"], Is.EqualTo(0.25).Within(1e-6),
                    "one riser above the floor under the anchor reads its own height");
                Assert.That(entry["point"] as IList, Has.Count.EqualTo(3));
                Assert.That(entry["normal"] as IList, Has.Count.EqualTo(3));
            }
        }

        [Test]
        public void Samples_WithoutGroundUnderTheAnchor_ReportAnUnknownHeightNotZero()
        {
            Transform anchor = CreateAnchor(new Vector3(0f, 2.5f, 0f));
            _rays.RaycastHandler = (origin, direction, maxDistance) =>
            {
                bool isUnder = Mathf.Abs(origin.x) < 1e-4f && Mathf.Abs(origin.z) < 1e-4f;
                if (maxDistance <= UnityProbeField.BlockerMaxDistance || isUnder)
                {
                    return default(ProbeRayHit);
                }

                return new ProbeRayHit
                {
                    Hit = true,
                    Point = new Vector3(origin.x, 2.75f, origin.z),
                    Normal = Vector3.up,
                    Distance = origin.y - 2.75f,
                };
            };

            Dictionary<string, object> probe = new UnityProbeField(_rays).Sample(anchor, UnityProbeField.KindPath);

            Assert.That((probe["under"] as IDictionary)["hit"], Is.EqualTo(false));
            var samples = probe["samples"] as IList;
            for (int i = 0; i < samples.Count; i++)
            {
                var entry = samples[i] as IDictionary;
                Assert.That(entry["hit"], Is.EqualTo(true), "the surface was measured; only its reference is unknown");
                Assert.That(entry["delta_y"], Is.Null, "no ground under the anchor is unknown, not zero");
                Assert.That(entry["point"] as IList, Has.Count.EqualTo(3));
            }
        }

        [Test]
        public void Samples_MissWithoutOverlap_HasNullReadingsNotZero()
        {
            Transform anchor = CreateAnchor(Vector3.zero);

            Dictionary<string, object> probe = new UnityProbeField(_rays).Sample(anchor, UnityProbeField.KindPath);

            var samples = probe["samples"] as IList;
            for (int i = 0; i < samples.Count; i++)
            {
                var entry = samples[i] as IDictionary;
                Assert.That(entry.Contains("point"), Is.True, "every field must be present");
                Assert.That(entry.Contains("delta_y"), Is.True);
                Assert.That(entry.Contains("normal"), Is.True);
                Assert.That(entry["hit"], Is.EqualTo(false));
                Assert.That(entry["point"], Is.Null, "a miss is null, not a point of zero");
                Assert.That(entry["delta_y"], Is.Null, "a miss is null, not a height of zero");
                Assert.That(entry["normal"], Is.Null);
                Assert.That(entry["inside"], Is.EqualTo(false));
            }
        }

        [Test]
        public void Samples_MissInsideGeometry_IsAHitWithNullReadings()
        {
            Transform anchor = CreateAnchor(Vector3.zero);
            _rays.OverlapHandler = (point, radius) => true;

            Dictionary<string, object> probe = new UnityProbeField(_rays).Sample(anchor, UnityProbeField.KindPath);

            var samples = probe["samples"] as IList;
            for (int i = 0; i < samples.Count; i++)
            {
                var entry = samples[i] as IDictionary;
                Assert.That(entry["hit"], Is.EqualTo(true));
                Assert.That(entry["inside"], Is.EqualTo(true));
                Assert.That(entry["point"], Is.Null);
                Assert.That(entry["delta_y"], Is.Null);
                Assert.That(entry["normal"], Is.Null);
            }
        }

        [Test]
        public void Samples_SelfHit_IsAMissAndTheOverlapDecides()
        {
            Transform anchor = CreateAnchor(Vector3.zero);
            _rays.RaycastHandler = (origin, direction, maxDistance) => new ProbeRayHit
            {
                Hit = true,
                Point = origin,
                Normal = Vector3.up,
                Distance = 0f,
                Self = true,
            };

            Dictionary<string, object> probe = new UnityProbeField(_rays).Sample(anchor, UnityProbeField.KindPath);

            var entry = (probe["samples"] as IList)[0] as IDictionary;
            Assert.That(entry["hit"], Is.EqualTo(false), "the anchor's own collider is not a surface");
            Assert.That(entry["point"], Is.Null);
            Assert.That(entry["delta_y"], Is.Null);
            Assert.That(entry["normal"], Is.Null);

            _rays.OverlapHandler = (point, radius) => true;
            Dictionary<string, object> inside = new UnityProbeField(_rays).Sample(anchor, UnityProbeField.KindPath);
            var insideEntry = (inside["samples"] as IList)[0] as IDictionary;
            Assert.That(insideEntry["hit"], Is.EqualTo(true));
            Assert.That(insideEntry["inside"], Is.EqualTo(true));
            Assert.That(insideEntry["delta_y"], Is.Null);
        }

        [Test]
        public void Blockers_AreTwoForwardCastsAtFixedHeights()
        {
            Transform anchor = CreateAnchor(Vector3.zero);
            _rays.RaycastHandler = (origin, direction, maxDistance) =>
            {
                if (maxDistance > UnityProbeField.BlockerMaxDistance)
                {
                    return default(ProbeRayHit);
                }

                return new ProbeRayHit
                {
                    Hit = true,
                    Point = origin + direction * 0.38f,
                    Normal = Vector3.forward,
                    Distance = 0.38f,
                };
            };

            Dictionary<string, object> probe = new UnityProbeField(_rays).Sample(anchor, UnityProbeField.KindPath);

            var blockers = probe["blockers"] as IList;
            Assert.That(blockers, Has.Count.EqualTo(2));
            var first = blockers[0] as IDictionary;
            var second = blockers[1] as IDictionary;
            Assert.That(first, Is.Not.Null);
            Assert.That(second, Is.Not.Null);
            Assert.That((float)first["height"], Is.EqualTo(0.15f).Within(Tolerance));
            Assert.That((float)second["height"], Is.EqualTo(0.9f).Within(Tolerance));
            for (int i = 0; i < blockers.Count; i++)
            {
                var entry = blockers[i] as IDictionary;
                Assert.That((float)entry["max_distance"], Is.EqualTo(1.8f).Within(Tolerance));
                Assert.That(entry["hit"], Is.EqualTo(true));
                Assert.That((double)entry["distance"], Is.EqualTo(0.38).Within(1e-6));
                Assert.That(entry["point"] as IList, Has.Count.EqualTo(3));
                Assert.That(entry["normal"] as IList, Has.Count.EqualTo(3));
            }
        }

        [Test]
        public void Blockers_MissHasNullDistancePointAndNormal()
        {
            Transform anchor = CreateAnchor(Vector3.zero);

            Dictionary<string, object> probe = new UnityProbeField(_rays).Sample(anchor, UnityProbeField.KindPath);

            var blockers = probe["blockers"] as IList;
            for (int i = 0; i < blockers.Count; i++)
            {
                var entry = blockers[i] as IDictionary;
                Assert.That(entry.Contains("distance"), Is.True, "every field must be present");
                Assert.That(entry["hit"], Is.EqualTo(false));
                Assert.That(entry["distance"], Is.Null, "an unmeasured distance is null, not zero");
                Assert.That(entry["point"], Is.Null);
                Assert.That(entry["normal"], Is.Null);
            }
        }

        [Test]
        public void Horizontal_ZeroFlatDirectionFallsBackToTheGivenAxis()
        {
            Vector3 up = UnityProbeField.Horizontal(new Vector3(0f, 1f, 0f), Vector3.forward);
            Vector3 down = UnityProbeField.Horizontal(new Vector3(0f, -1f, 0f), Vector3.right);
            Vector3 angled = UnityProbeField.Horizontal(new Vector3(3f, 7f, 4f), Vector3.forward);

            Assert.That(up.x, Is.EqualTo(0f).Within(Tolerance));
            Assert.That(up.z, Is.EqualTo(1f).Within(Tolerance));
            Assert.That(down.x, Is.EqualTo(1f).Within(Tolerance));
            Assert.That(down.z, Is.EqualTo(0f).Within(Tolerance));
            Assert.That(angled.x, Is.EqualTo(0.6f).Within(Tolerance));
            Assert.That(angled.y, Is.EqualTo(0f).Within(Tolerance));
            Assert.That(angled.z, Is.EqualTo(0.8f).Within(Tolerance));
        }

        [Test]
        public void AnchorResolution_PrefersTheEnvironmentPathOverSceneComponents()
        {
            Transform anchor = CreateAnchor(Vector3.zero);
            CreateComponentHost<Rigidbody>("ProbeTestDecoyBody");
            CreateComponentHost<CharacterController>("ProbeTestDecoyController");
            Environment.SetEnvironmentVariable(UnityProbeField.AnchorVariable, AnchorName);

            var field = new UnityProbeField(_rays);
            Assert.That(field.TryResolve(out Transform resolved, out string reason), Is.True, reason);

            Assert.That(resolved, Is.SameAs(anchor));
            Assert.That(field.AnchorKind, Is.EqualTo(UnityProbeField.KindPath));
        }

        [Test]
        public void ChooseAnchor_OrderAndReasonAreDecidedWithoutAScene()
        {
            Transform path = CreateAnchor(Vector3.zero);
            Transform controller = CreateComponentHost<CharacterController>("ProbeTestController");
            Transform body = CreateComponentHost<Rigidbody>("ProbeTestBody");

            Assert.That(UnityProbeField.ChooseAnchor(AnchorName, path, controller, body,
                out Transform chosen, out string kind, out string reason), Is.True, reason);
            Assert.That(chosen, Is.SameAs(path));
            Assert.That(kind, Is.EqualTo(UnityProbeField.KindPath));

            Assert.That(UnityProbeField.ChooseAnchor(null, null, controller, body,
                out chosen, out kind, out reason), Is.True, reason);
            Assert.That(chosen, Is.SameAs(controller));
            Assert.That(kind, Is.EqualTo(UnityProbeField.KindCharacterController));

            Assert.That(UnityProbeField.ChooseAnchor(null, null, null, body,
                out chosen, out kind, out reason), Is.True, reason);
            Assert.That(chosen, Is.SameAs(body));
            Assert.That(kind, Is.EqualTo(UnityProbeField.KindRigidbody));

            Assert.That(UnityProbeField.ChooseAnchor("ProbeTestMissing", null, controller, body,
                out chosen, out kind, out reason), Is.False, "a named path never falls back to a component");
            Assert.That(chosen, Is.Null);
            Assert.That(reason, Does.Contain("names no GameObject").And.Contain("ProbeTestMissing"));

            Assert.That(UnityProbeField.ChooseAnchor(null, null, null, null,
                out chosen, out kind, out reason), Is.False);
            Assert.That(chosen, Is.Null);
            Assert.That(kind, Is.Null);
            Assert.That(reason, Is.EqualTo(UnityProbeField.NoAnchorReason));
        }

        [Test]
        public void AnchorResolution_ReResolvesAfterTheCachedAnchorIsDestroyed()
        {
            GameObject first = CreateGameObject("ProbeTestAnchorOne");
            Environment.SetEnvironmentVariable(UnityProbeField.AnchorVariable, first.name);

            var field = new UnityProbeField(_rays);
            Assert.That(field.TryResolve(out Transform resolved, out string reason), Is.True, reason);
            Assert.That(resolved, Is.SameAs(first.transform));

            UnityEngine.Object.DestroyImmediate(first);
            Transform second = CreateAnchor(Vector3.zero);
            Environment.SetEnvironmentVariable(UnityProbeField.AnchorVariable, AnchorName);

            Assert.That(field.TryResolve(out Transform again, out reason), Is.True, reason);
            Assert.That(again, Is.SameAs(second));
        }

        [Test]
        public void Source_RaycastFailure_OmitsTheProbeAndReportsTheReaderError()
        {
            CreateAnchor(Vector3.zero);
            Environment.SetEnvironmentVariable(UnityProbeField.AnchorVariable, AnchorName);
            _rays.RaycastHandler = (origin, direction, maxDistance) =>
                throw new InvalidOperationException("the seam failed");

            using (var source = new UnityObservationSource(probeRaycasts: _rays))
            {
                source.Start();
                BridgeObservationSnapshot snapshot = source.GetObservation();

                Assert.That(snapshot.Public.ContainsKey("probe"), Is.False, "a failed sample publishes no probe");
                ObservationReaderError error = snapshot.ReaderErrors.Find(entry => entry.Reader == "probe");
                Assert.That(error, Is.Not.Null, "a failed sample records a reader error");
                Assert.That(error.Error, Does.Contain("InvalidOperationException"));
            }
        }

        private Transform CreateAnchor(Vector3 position)
        {
            GameObject created = CreateGameObject(AnchorName);
            created.transform.position = position;
            return created.transform;
        }

        private GameObject CreateGameObject(string name)
        {
            var created = new GameObject(name);
            _created.Add(created);
            return created;
        }

        private Transform CreateComponentHost<T>(string name) where T : Component
        {
            return CreateGameObject(name).AddComponent<T>().transform;
        }

        /// <summary>A seam that answers from the test instead of from Unity's physics.</summary>
        private sealed class FakeRaycasts : IProbeRaycasts
        {
            public Func<Vector3, Vector3, float, ProbeRayHit> RaycastHandler;

            public Func<Vector3, float, bool> OverlapHandler;

            public bool Raycast(Vector3 origin, Vector3 direction, float maxDistance, out ProbeRayHit hit)
            {
                ProbeRayHit reading = RaycastHandler == null
                    ? default(ProbeRayHit)
                    : RaycastHandler(origin, direction, maxDistance);
                hit = reading;
                return reading.Hit;
            }

            public bool Overlap(Vector3 point, float radius)
            {
                return OverlapHandler != null && OverlapHandler(point, radius);
            }
        }
    }
}
