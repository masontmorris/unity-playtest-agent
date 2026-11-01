// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections;
using System.Collections.Generic;
using System.Threading.Tasks;
using NUnit.Framework;
using Playtest.Bridge.Transport;
using Playtest.Bridge.Unity;
using UnityEngine;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The placement operation and the configured-entity resolver, driven directly on the main
    /// thread so the settle wait is deterministic: no frame has to run for a step to be taken.
    /// The source's own marshalling is exercised once, when the Editor is not playing.
    /// </summary>
    [TestFixture]
    public sealed class UnityPlacementSourceTests
    {
        private readonly List<GameObject> _created = new List<GameObject>();

        private float _originalTimeScale;

        [SetUp]
        public void SetUp()
        {
            _originalTimeScale = Time.timeScale;
            Time.timeScale = 1f;
        }

        [TearDown]
        public void TearDown()
        {
            foreach (GameObject created in _created)
            {
                if (created != null)
                {
                    UnityEngine.Object.DestroyImmediate(created);
                }
            }

            _created.Clear();
            Time.timeScale = _originalTimeScale;
        }

        [TestCase("transform")]
        [TestCase("rigidbody")]
        [TestCase("rigidbody2d")]
        public void Operation_SetsThePoseAndMeasuresAfterTheSettle(string kind)
        {
            GameObject entity = Entity(kind);
            entity.transform.position = new Vector3(1f, 1f, 1f);
            var target = new Vector3(4f, -1.5f, 2f);
            BridgePlacementOperation operation = Operation(entity, target, settleS: 0.5);

            Assert.That(operation.Step(10.0, 100.0), Is.Null, "the pose is set on the first step and the wait begins");
            Assert.That(operation.Entity, Is.SameAs(entity));
            Assert.That(entity.transform.position, Is.EqualTo(target));

            Assert.That(operation.Step(10.25, 100.1), Is.Null, "half a settle is not a settle");

            BridgePlacementResult result = operation.Step(10.5, 100.2);

            Assert.That(result, Is.Not.Null);
            Assert.That(result.Placed, Is.True);
            Assert.That(result.Reason, Is.Null);
            Assert.That(result.PositionAfterSettle, Is.EqualTo(new[] { 4.0, -1.5, 2.0 }));
            Assert.That(result.MovedDuringSettle, Is.EqualTo(0.0).Within(1e-4));
            Assert.That(operation.IsComplete, Is.True);
            Assert.That(operation.Step(99.0, 999.0), Is.SameAs(result), "a completed placement is not re-run");
            Assert.That(entity.transform.position, Is.EqualTo(target), "a settled pose is not touched again");
        }

        [Test]
        public void Operation_Rigidbody_ZerosTheLinearVelocity()
        {
            GameObject entity = Entity("rigidbody");
            var body = entity.GetComponent<Rigidbody>();
            body.linearVelocity = new Vector3(3f, 2f, 1f);
            var target = new Vector3(-2f, 7f, 0.5f);

            Operation(entity, target, settleS: 0.1).Step(1.0, 1.0);

            Assert.That((body.position - target).magnitude, Is.LessThan(1e-4), "the body's own position is set");
            Assert.That(body.linearVelocity.magnitude, Is.LessThan(1e-4), "a placed body must not keep moving");
            Assert.That(entity.transform.position, Is.EqualTo(target), "the transform follows its body");
        }

        [Test]
        public void Operation_Rigidbody2d_SetsThePlanarPoseAndZerosTheLinearVelocity()
        {
            GameObject entity = Entity("rigidbody2d");
            var body = entity.GetComponent<Rigidbody2D>();
            body.linearVelocity = new Vector2(2f, 1f);
            var target = new Vector3(-3f, 6f, 0f);

            Operation(entity, target, settleS: 0.1).Step(1.0, 1.0);

            Assert.That((body.position - new Vector2(-3f, 6f)).magnitude, Is.LessThan(1e-4));
            Assert.That(body.linearVelocity.magnitude, Is.LessThan(1e-4), "a placed body must not keep moving");
            Assert.That(entity.transform.position, Is.EqualTo(target));
        }

        [Test]
        public void Operation_MovementDuringTheSettle_IsMeasured()
        {
            GameObject entity = Entity("transform");
            var target = new Vector3(0f, 0f, 0f);
            BridgePlacementOperation operation = Operation(entity, target, settleS: 1.0);
            operation.Step(0.0, 0.0);

            entity.transform.position = new Vector3(0f, 2f, 0f);

            BridgePlacementResult result = operation.Step(1.0, 0.05);

            Assert.That(result.Placed, Is.True, "movement after the pose is measured, not hidden");
            Assert.That(result.MovedDuringSettle, Is.EqualTo(2.0).Within(1e-4));
            Assert.That(result.PositionAfterSettle, Is.EqualTo(new[] { 0.0, 2.0, 0.0 }));
        }

        [Test]
        public void Operation_WhenTheProjectIsNotPlaying_IsUnplacedAndTouchesNothing()
        {
            GameObject entity = Entity("transform");
            var start = new Vector3(5f, 5f, 5f);
            entity.transform.position = start;
            BridgePlacementOperation operation = new BridgePlacementOperation(
                entity,
                new Vector3(1f, 2f, 3f),
                settleS: 0.5,
                wallBoundS: 20.0,
                playing: false,
                paused: false);

            BridgePlacementResult result = operation.Step(1.0, 1.0);

            Assert.That(result.Placed, Is.False);
            Assert.That(result.Reason, Does.Contain("not playing"));
            Assert.That(result.PositionAfterSettle, Is.Null);
            Assert.That(result.MovedDuringSettle, Is.Null);
            Assert.That(entity.transform.position, Is.EqualTo(start), "a refusal never moves the entity");
        }

        [Test]
        public void Operation_WhenTheClockIsPaused_IsUnplacedBeforeTouchingThePose()
        {
            GameObject entity = Entity("transform");
            var start = new Vector3(5f, 5f, 5f);
            entity.transform.position = start;
            BridgePlacementOperation operation = new BridgePlacementOperation(
                entity,
                new Vector3(1f, 2f, 3f),
                settleS: 0.5,
                wallBoundS: 20.0,
                playing: true,
                paused: true);

            BridgePlacementResult result = operation.Step(1.0, 1.0);

            Assert.That(result.Placed, Is.False);
            Assert.That(result.Reason, Does.Contain("paused"));
            Assert.That(entity.transform.position, Is.EqualTo(start), "a paused clock is refused before the pose is set");
        }

        [Test]
        public void Operation_WhenTheEntityIsGone_IsUnplaced()
        {
            GameObject entity = Entity("transform");
            BridgePlacementOperation operation = Operation(entity, new Vector3(1f, 2f, 3f), settleS: 0.1);
            operation.Step(0.0, 0.0);

            UnityEngine.Object.DestroyImmediate(entity);

            BridgePlacementResult result = operation.Step(1.0, 1.0);

            Assert.That(result.Placed, Is.False);
            Assert.That(result.Reason, Does.Contain("destroyed"));
            Assert.That(result.PositionAfterSettle, Is.Null);
        }

        [Test]
        public void Operation_WhenTheClockStalls_EndsWithoutAMeasurement()
        {
            GameObject entity = Entity("transform");
            double wallBound = BridgePlacementOperation.WallBoundSeconds(1.0, 1.0);
            BridgePlacementOperation operation = Operation(entity, new Vector3(1f, 2f, 3f), settleS: 1.0, wallBoundS: wallBound);
            operation.Step(0.0, 0.0);

            BridgePlacementResult stall = operation.Step(0.2, wallBound + 1.0);

            Assert.That(stall.Placed, Is.False);
            Assert.That(stall.Reason, Does.Contain("wall bound"));
            Assert.That(stall.PositionAfterSettle, Is.Null, "a settle that never happened is not measured");
        }

        [TestCase(1.0, 0.5, 12.0)]
        [TestCase(1.0, 1.0, 11.0)]
        [TestCase(1.0, 0.0, 30.0)]
        public void WallBoundSeconds_ScalesWithTheClockAndFloorsAtTheSlowestScale(
            double settleS,
            double timeScale,
            double expected)
        {
            Assert.That(BridgePlacementOperation.WallBoundSeconds(settleS, timeScale), Is.EqualTo(expected).Within(1e-6));
        }

        [Test]
        public void WallBoundSeconds_IsAlwaysLongerThanTheSettleItself()
        {
            Assert.That(BridgePlacementOperation.WallBoundSeconds(5.0, 1.0), Is.GreaterThan(5.0));
            Assert.That(BridgePlacementOperation.WallBoundSeconds(0.1, 4.0), Is.GreaterThan(0.1));
        }

        [Test]
        public void ControlledEntity_Unconfigured_IsAPlainReason()
        {
            string previous = Environment.GetEnvironmentVariable(UnityControlledEntity.VariableName);
            Environment.SetEnvironmentVariable(UnityControlledEntity.VariableName, null);
            try
            {
                Assert.That(UnityControlledEntity.TryResolve(out GameObject entity, out string reason), Is.False);
                Assert.That(entity, Is.Null, "an unconfigured session never resolves a substitute object");
                Assert.That(reason, Does.Contain(UnityControlledEntity.VariableName));
            }
            finally
            {
                Environment.SetEnvironmentVariable(UnityControlledEntity.VariableName, previous);
            }
        }

        [Test]
        public void ControlledEntity_UnknownPath_IsAPlainReason()
        {
            string previous = Environment.GetEnvironmentVariable(UnityControlledEntity.VariableName);
            Environment.SetEnvironmentVariable(UnityControlledEntity.VariableName, "PlaceTestNoSuchObject");
            try
            {
                Assert.That(UnityControlledEntity.TryResolve(out GameObject entity, out string reason), Is.False);
                Assert.That(entity, Is.Null);
                Assert.That(reason, Does.Contain("PlaceTestNoSuchObject"));
            }
            finally
            {
                Environment.SetEnvironmentVariable(UnityControlledEntity.VariableName, previous);
            }
        }

        [UnityTest]
        public IEnumerator Place_WhenTheEditorIsNotPlaying_IsAnUnplacedResult()
        {
            Assert.That(Application.isPlaying, Is.False, "EditMode tests never run inside a play session");
            using (var source = new UnityPlacementSource())
            {
                var request = new BridgePlacementRequest
                {
                    Position = new List<double> { 1.0, 2.0, 3.0 },
                    SettleS = 0.1,
                };
                Task<BridgePlacementResult> answered = Task.Run(() => source.Place(request));
                DateTime deadline = DateTime.UtcNow.AddSeconds(10);
                while (!answered.IsCompleted && DateTime.UtcNow < deadline)
                {
                    yield return null;
                }

                Assert.That(answered.IsCompleted, Is.True, "the source must answer through the editor update loop");
                BridgePlacementResult result = answered.GetAwaiter().GetResult();
                Assert.That(result.Placed, Is.False);
                Assert.That(result.Reason, Does.Contain("not playing"));
                Assert.That(result.PositionAfterSettle, Is.Null);
            }
        }

        [Test]
        public void Place_AfterDispose_IsUnavailable()
        {
            var source = new UnityPlacementSource();
            source.Dispose();

            var request = new BridgePlacementRequest
            {
                Position = new List<double> { 1.0, 2.0, 3.0 },
                SettleS = 0.1,
            };

            Assert.Throws<BridgePlacementUnavailableException>(() => source.Place(request));
        }

        private GameObject Entity(string kind)
        {
            var entity = new GameObject("UPA_place_test_unique");
            _created.Add(entity);
            if (kind == "rigidbody")
            {
                entity.AddComponent<Rigidbody>();
            }
            else if (kind == "rigidbody2d")
            {
                entity.AddComponent<Rigidbody2D>();
            }

            return entity;
        }

        private static BridgePlacementOperation Operation(GameObject entity, Vector3 target, double settleS)
        {
            return Operation(entity, target, settleS, BridgePlacementOperation.WallBoundSeconds(settleS, 1.0));
        }

        private static BridgePlacementOperation Operation(
            GameObject entity,
            Vector3 target,
            double settleS,
            double wallBoundS)
        {
            return new BridgePlacementOperation(entity, target, settleS, wallBoundS, playing: true, paused: false);
        }
    }
}
