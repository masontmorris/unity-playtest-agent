// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections;
using System.Collections.Generic;
using NUnit.Framework;
using Playtest.Bridge.Unity;
using UnityEngine;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The probe in a plane. These tests use real 2D physics rather than a fake seam: a body with a
    /// <see cref="Rigidbody2D"/>, floors made of <see cref="BoxCollider2D"/>, an edge and a wall. They
    /// check the shape the probe publishes in 2D — the same keys and units as in 3D — and the three
    /// readings a run-up needs: floor under, floor ahead, and floor behind.
    /// </summary>
    [TestFixture]
    public sealed class UnityProbeField2DTests
    {
        private const string BodyName = "ProbeTestPlanarBody";

        private const float Tolerance = 1e-3f;

        private readonly List<GameObject> _created = new List<GameObject>();

        private string _originalAnchorPath;

        private string _originalEntityPath;

        [SetUp]
        public void SetUp()
        {
            _originalAnchorPath = Environment.GetEnvironmentVariable(UnityProbeField.AnchorVariable);
            _originalEntityPath = Environment.GetEnvironmentVariable(UnityControlledEntity.VariableName);
            Environment.SetEnvironmentVariable(UnityProbeField.AnchorVariable, null);
            Environment.SetEnvironmentVariable(UnityControlledEntity.VariableName, null);
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
            Environment.SetEnvironmentVariable(UnityProbeField.AnchorVariable, _originalAnchorPath);
            Environment.SetEnvironmentVariable(UnityControlledEntity.VariableName, _originalEntityPath);
        }

        [Test]
        public void Planar_ProbeNamesItsPlaneAndTheDirectionItCallsAhead()
        {
            Transform anchor = CreateBody(new Vector3(0f, 0f, 0f));
            CreateSlab("ProbeTestPlanarFloor", new Vector2(0f, -0.5f), new Vector2(8f, 1f));

            Dictionary<string, object> probe = Sample(anchor);

            var basis = probe["basis"] as IDictionary;
            Assert.That(basis, Is.Not.Null, "the frame the samples were taken in is published");
            Assert.That(basis["plane"], Is.EqualTo("2d"));
            Assert.That(basis["lateral"], Is.EqualTo("collapsed"), "a plane has no lateral axis to cast");
            Assert.That(basis["forward_source"], Is.EqualTo("facing_scale"));
            var forward = basis["forward"] as IList;
            Assert.That((float)forward[0], Is.EqualTo(1f).Within(Tolerance));
            Assert.That((float)forward[1], Is.EqualTo(0f).Within(Tolerance));

            anchor.localScale = new Vector3(-1f, 1f, 1f);
            Physics2D.SyncTransforms();
            var mirrored = Sample(anchor)["basis"] as IDictionary;
            var mirroredForward = mirrored["forward"] as IList;
            Assert.That((float)mirroredForward[0], Is.EqualTo(-1f).Within(Tolerance),
                "a body mirrored on x faces the other way");
        }

        [Test]
        public void Planar_ProbeKeepsTheThreeByThreeKeysInThePlane()
        {
            Transform anchor = CreateBody(Vector3.zero);
            CreateSlab("ProbeTestPlanarFloor", new Vector2(0f, -0.5f), new Vector2(8f, 1f));

            Dictionary<string, object> probe = Sample(anchor);

            var under = probe["under"] as IDictionary;
            Assert.That(under, Is.Not.Null);
            Assert.That(under["hit"], Is.EqualTo(true));
            Assert.That(under["inside"], Is.EqualTo(false));
            Assert.That((double)under["delta_y"], Is.EqualTo(0.0).Within(1e-3),
                "a body standing on the floor is measured against it");

            var samples = probe["samples"] as IList;
            Assert.That(samples, Has.Count.EqualTo(3), "one in-plane ray per offset ahead");
            float[] ahead = { 0.6f, 1.2f, 1.8f };
            for (int i = 0; i < samples.Count; i++)
            {
                var entry = samples[i] as IDictionary;
                Assert.That((float)entry["forward"], Is.EqualTo(ahead[i]).Within(Tolerance));
                Assert.That((float)entry["lateral"], Is.EqualTo(0f).Within(Tolerance));
                Assert.That(entry["hit"], Is.EqualTo(true));
                Assert.That((double)entry["delta_y"], Is.EqualTo(0.0).Within(1e-3));
                Assert.That(entry["point"] as IList, Has.Count.EqualTo(3));
                Assert.That(entry["normal"] as IList, Has.Count.EqualTo(3));
            }
        }

        [Test]
        public void Planar_ProbeReportsWhereTheFloorAheadEnds()
        {
            Transform anchor = CreateBody(Vector3.zero);
            // The floor runs from -4 to +1, so the sample at 1.2 has nothing under it.
            CreateSlab("ProbeTestPlanarFloor", new Vector2(-1.5f, -0.5f), new Vector2(5f, 1f));

            Dictionary<string, object> probe = Sample(anchor);

            var samples = probe["samples"] as IList;
            var last = samples[2] as IDictionary;
            var first = samples[0] as IDictionary;
            Assert.That(first["hit"], Is.EqualTo(true), "0.6 ahead is still floor");
            Assert.That(last["hit"], Is.EqualTo(false), "1.8 ahead is past the edge");
            Assert.That(last["point"], Is.Null, "a miss is null, not a point of zero");
            Assert.That(last["delta_y"], Is.Null, "a miss is null, not a height of zero");
            Assert.That(last["normal"], Is.Null);
        }

        [Test]
        public void Planar_ProbeReportsWhereTheFloorBehindEnds()
        {
            Transform anchor = CreateBody(Vector3.zero);
            // The floor runs from -0.5 to +4, so the first ray behind has nothing under it.
            CreateSlab("ProbeTestPlanarFloor", new Vector2(1.75f, -0.5f), new Vector2(4.5f, 1f));

            Dictionary<string, object> probe = Sample(anchor);

            var backward = probe["backward"] as IList;
            Assert.That(backward, Has.Count.EqualTo(3), "one in-plane ray per offset behind");
            float[] behind = { -0.6f, -1.2f, -1.8f };
            for (int i = 0; i < backward.Count; i++)
            {
                var entry = backward[i] as IDictionary;
                Assert.That((float)entry["forward"], Is.EqualTo(behind[i]).Within(Tolerance),
                    "a distance behind is published as a negative offset");
                Assert.That(entry["hit"], Is.EqualTo(false), "the floor ends 0.5 behind the anchor");
                Assert.That(entry["delta_y"], Is.Null);
            }

            var ahead = probe["samples"] as IList;
            for (int i = 0; i < ahead.Count; i++)
            {
                Assert.That((ahead[i] as IDictionary)["hit"], Is.EqualTo(true),
                    "the floor ahead was not affected by the edge behind");
            }
        }

        [Test]
        public void Planar_ProbeReportsAStandingSurfaceAheadAsItsHeight()
        {
            Transform anchor = CreateBody(Vector3.zero);
            CreateSlab("ProbeTestPlanarFloor", new Vector2(0f, -0.5f), new Vector2(10f, 1f));
            // A one-unit riser ahead: the sample over it lands on its top, one riser above the floor.
            CreateSlab("ProbeTestPlanarRiser", new Vector2(1.2f, 0.5f), new Vector2(2f, 1f));

            Dictionary<string, object> probe = Sample(anchor);

            var samples = probe["samples"] as IList;
            var over = samples[0] as IDictionary;
            Assert.That(over["hit"], Is.EqualTo(true));
            Assert.That((double)over["delta_y"], Is.EqualTo(1.0).Within(1e-3),
                "a surface one riser up reads that riser's height");
        }

        [Test]
        public void Planar_ProbeReportsAWallAheadAtTheSameDistancesAsInThreeDimensions()
        {
            Transform anchor = CreateBody(Vector3.zero);
            CreateSlab("ProbeTestPlanarFloor", new Vector2(0f, -0.5f), new Vector2(10f, 1f));
            CreateSlab("ProbeTestPlanarWall", new Vector2(0.55f, 1.5f), new Vector2(0.1f, 3f));

            Dictionary<string, object> probe = Sample(anchor);

            var blockers = probe["blockers"] as IList;
            Assert.That(blockers, Has.Count.EqualTo(2));
            var low = blockers[0] as IDictionary;
            var high = blockers[1] as IDictionary;
            Assert.That((float)low["height"], Is.EqualTo(0.15f).Within(Tolerance));
            Assert.That((float)high["height"], Is.EqualTo(0.9f).Within(Tolerance));
            for (int i = 0; i < blockers.Count; i++)
            {
                var entry = blockers[i] as IDictionary;
                Assert.That((float)entry["max_distance"], Is.EqualTo(1.8f).Within(Tolerance));
                Assert.That(entry["hit"], Is.EqualTo(true), "the wall stands in front of both casts");
                Assert.That((double)entry["distance"], Is.EqualTo(0.5).Within(5e-3));
                var normal = entry["normal"] as IList;
                Assert.That((float)normal[0], Is.EqualTo(-1f).Within(Tolerance));
            }
        }

        [Test]
        public void Planar_AnchorIsDetectedFromTheConfiguredEntityWhenTheAnchorIsAPlainTransform()
        {
            GameObject entity = CreateGameObject(BodyName);
            entity.transform.position = Vector3.zero;
            entity.AddComponent<Rigidbody2D>();
            GameObject child = CreateGameObject("ProbeTestPlanarChild");
            child.transform.SetParent(entity.transform, false);
            Environment.SetEnvironmentVariable(UnityControlledEntity.VariableName, BodyName);

            Assert.That(UnityProbeField.IsPlanar(child.transform), Is.True,
                "the body the session moves decides the plane, not the object the path names");
            Assert.That(UnityProbeField.IsPlanar(CreateGameObject("ProbeTestPlanarPlain").transform), Is.False);
        }

        [Test]
        public void Planar_ForwardFallsBackToTheRightAxisWhenTheTransformIsDegenerate()
        {
            Transform anchor = CreateBody(Vector3.zero);
            // Turned to look along the plane's normal: its own right axis has no part in the plane.
            anchor.rotation = Quaternion.Euler(0f, 90f, 0f);

            Vector3 forward = UnityProbeField.Forward(anchor, true);

            Assert.That(forward.x, Is.EqualTo(1f).Within(Tolerance));
            Assert.That(forward.y, Is.EqualTo(0f).Within(Tolerance));
            Assert.That(forward.z, Is.EqualTo(0f).Within(Tolerance));
        }

        private static Dictionary<string, object> Sample(Transform anchor)
        {
            Physics2D.SyncTransforms();
            return new UnityProbeField().Sample(anchor, UnityProbeField.KindRigidbody2D);
        }

        private Transform CreateBody(Vector3 position)
        {
            GameObject body = CreateGameObject(BodyName);
            body.transform.position = position;
            body.AddComponent<Rigidbody2D>();
            return body.transform;
        }

        private void CreateSlab(string name, Vector2 centre, Vector2 size)
        {
            GameObject slab = CreateGameObject(name);
            slab.transform.position = new Vector3(centre.x, centre.y, 0f);
            var box = slab.AddComponent<BoxCollider2D>();
            box.size = size;
            Physics2D.SyncTransforms();
        }

        private GameObject CreateGameObject(string name)
        {
            var created = new GameObject(name);
            _created.Add(created);
            return created;
        }
    }
}
