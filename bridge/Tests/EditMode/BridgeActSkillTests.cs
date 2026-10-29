using System;
using System.Collections.Generic;
using System.Linq;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Chords and skills over the real transport, against the real executor and a simulated device:
    /// the wire shows per-input receipts, a bad act is rejected without disturbing the running one,
    /// and a stop names and verifies what it released.
    /// </summary>
    [TestFixture]
    public sealed class BridgeActSkillTests
    {
        private InputRig _rig;
        private BridgeServer _server;
        private ActFakeObservationSource _source;

        [SetUp]
        public void SetUp()
        {
            _rig = new InputRig(operationTimeoutMs: 2000, realClock: true);
            _source = new ActFakeObservationSource();
            BridgeActionCatalog catalog = BridgeActionCatalog.FromDefinitions(new[]
            {
                InputRig.Action("strafe", 0.1, InputRig.Key("w"), InputRig.Key("a")),
                InputRig.Action("hold", 30, InputRig.Key("w")),
                InputRig.Skill(
                    "combo",
                    2.0,
                    new BridgeActionStep(new[] { InputRig.Key("w") }, 0.1, 0.05),
                    new BridgeActionStep(new[] { new BridgeInputBinding("Mouse", "delta", 0.5, 0) }, 0.05, 0)),
            });
            _server = new BridgeServer(
                new BridgeServerOptions { Port = 0, HandshakeTimeoutMs = 3000, ReadTimeoutMs = 0, ReleaseVerifyTimeoutMs = 1000 },
                new FakeStatusSource(),
                _source,
                catalog,
                _rig.Executor,
                BridgeTestSupport.Token);
            _server.Start();
        }

        [TearDown]
        public void TearDown()
        {
            _server?.Dispose();
            _rig.Dispose();
        }

        [Test]
        public void Chord_ReportsRunningThenCompletedWithPerInputReceiptsOnTheWire()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);

                client.Send(Act("a1", observation.Seq, "strafe", null));
                ExecutionRecordMessage running = ReadExecution(client);
                ExecutionRecordMessage completed = ReadExecution(client);

                Assert.That(running.Status, Is.EqualTo(BridgeExecStatus.Running));
                Assert.That(running.AppliedInputs.Select(i => i.Control), Is.EqualTo(new[] { "w", "a" }));
                Assert.That(running.AppliedInputs.All(i => i.InputStartWallS.HasValue && !i.InputEndWallS.HasValue), Is.True);
                Assert.That(completed.Status, Is.EqualTo(BridgeExecStatus.Completed));
                Assert.That(completed.ReleaseVerified, Is.True);
                Assert.That(completed.AppliedInputs.Count, Is.EqualTo(2));
                foreach (AppliedInputReceipt receipt in completed.AppliedInputs)
                {
                    Assert.That(receipt.Device, Is.EqualTo("Keyboard"));
                    Assert.That(receipt.ReleaseVerified, Is.True);
                    Assert.That(receipt.AppliedDurationS, Is.GreaterThanOrEqualTo(0.08), "measured from the device, close to the 0.1 s hold");
                    Assert.That(receipt.InputEndWallS, Is.GreaterThan(receipt.InputStartWallS.Value));
                }

                Assert.That(_rig.Surface.Log, Is.EqualTo(new[] { "queue Keyboard w+a", "neutral Keyboard" }));
            }
        }

        [Test]
        public void Skill_RunsItsStepsInOrderAndSpansTheDeclaredHoldsAndWaits()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);

                client.Send(Act("s1", observation.Seq, "combo", null));
                ExecutionRecordMessage running = ReadExecution(client);
                ExecutionRecordMessage completed = ReadExecution(client);

                Assert.That(running.Status, Is.EqualTo(BridgeExecStatus.Running));
                Assert.That(completed.Status, Is.EqualTo(BridgeExecStatus.Completed));
                Assert.That(completed.ReleaseVerified, Is.True);
                Assert.That(
                    completed.AppliedInputs.Select(i => i.StepIndex + ":" + i.Device + "." + i.Control),
                    Is.EqualTo(new[] { "0:Keyboard.w", "1:Mouse.delta" }));
                Assert.That(completed.AppliedInputs[1].AppliedDeltaX, Is.EqualTo(32).Within(1e-9));
                Assert.That(completed.AppliedInputs[1].AppliedDurationS, Is.Null, "an impulse has no duration");
                Assert.That(completed.AppliedDurationS, Is.GreaterThanOrEqualTo(0.17), "0.1 hold + 0.05 wait + 0.05 hold, measured");
                Assert.That(
                    _rig.Surface.Log,
                    Is.EqualTo(new[] { "queue Keyboard w", "neutral Keyboard", "queue Mouse delta", "neutral Mouse" }));
            }
        }

        [Test]
        public void ActParams_AreRejectedAsInvalidParamsAndTheRunningHoldSurvives()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);
                client.Send(Act("held", observation.Seq, "hold", 30));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Running));
                List<string> log = _rig.Surface.Log.ToList();

                ActRequestMessage withParams = Act("tweak", observation.Seq, "strafe", null);
                withParams.Params = new Dictionary<string, object> { { "speed", 2 } };
                client.Send(withParams);
                ExecutionRecordMessage rejected = ReadExecution(client);

                Assert.That(rejected.ActionId, Is.EqualTo("tweak"));
                Assert.That(rejected.Status, Is.EqualTo(BridgeExecStatus.Rejected));
                Assert.That(rejected.RejectReason, Is.EqualTo(BridgeRejectReason.InvalidParams));
                Assert.That(_rig.Surface.Log, Is.EqualTo(log), "the rejected act touched nothing");
                Assert.That(_rig.Surface.Keys, Is.EqualTo(new[] { "w" }), "the running hold is undisturbed");
            }
        }

        [Test]
        public void SkillLeaseThatIsNotMaxS_IsRejectedWithoutTouchingTheDevice()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);

                client.Send(Act("s1", observation.Seq, "combo", 1.0));
                ExecutionRecordMessage rejected = ReadExecution(client);

                Assert.That(rejected.Status, Is.EqualTo(BridgeExecStatus.Rejected));
                Assert.That(rejected.RejectReason, Is.EqualTo(BridgeRejectReason.InvalidParams));
                Assert.That(_rig.Surface.Log, Is.Empty);
            }
        }

        [Test]
        public void Stop_ReleasesAHeldInputNamesItAndVerifiesItFromTheDevice()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);
                client.Send(Act("held", observation.Seq, "hold", 30));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Running));

                client.Send(new StopRequestMessage { Session = "s-skill", Seq = 5, WallUtc = DateTime.UtcNow, Reason = "user" });
                ExecutionRecordMessage cancelled = ReadExecution(client);
                Assert.That(
                    BridgeProtocol.TryReadStopResult(client.ReadLine(), out StopResultMessage stopped, out ProtocolError error),
                    Is.True,
                    error?.Message);

                Assert.That(cancelled.Status, Is.EqualTo(BridgeExecStatus.Cancelled));
                Assert.That(cancelled.ReleaseVerified, Is.True);
                Assert.That(cancelled.AppliedInputs.Single().ReleaseVerified, Is.True);
                Assert.That(stopped.Released, Is.EqualTo(new[] { "key:w" }));
                Assert.That(stopped.ReleaseVerified, Is.True);
                Assert.That(_rig.Surface.Keys, Is.Empty);
                Assert.That(client.WaitForClose(), Is.True);
            }
        }

        [Test]
        public void Stop_WithNothingRunningReleasesNothingEvenAfterAnEarlierAction()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);
                client.Send(Act("a1", observation.Seq, "strafe", null));
                ReadExecution(client);
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Completed));

                client.Send(new StopRequestMessage { Session = "s-skill", Seq = 5, WallUtc = DateTime.UtcNow, Reason = "finish" });
                Assert.That(
                    BridgeProtocol.TryReadStopResult(client.ReadLine(), out StopResultMessage stopped, out ProtocolError error),
                    Is.True,
                    error?.Message);

                Assert.That(stopped.Released, Is.Empty, "the stop released nothing; the earlier chord ended on its own");
                Assert.That(stopped.ReleaseVerified, Is.True);
            }
        }

        [Test]
        public void Hello_WithTheOldProtocol_IsRejectedBecauseExecutionRecordsGainedFields()
        {
            Assert.That(ProtocolVersion.Current, Is.EqualTo("upa/1"));
            Assert.That(ProtocolVersion.IsSupported("upa/0"), Is.False);
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello(protocol: "upa/0", session: "s-old"));

                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Protocol));
                Assert.That(error.Message, Does.Contain("upa/1"));
            }
        }

        private static void Handshake(TestClient client)
        {
            client.Send(BridgeTestSupport.Hello(session: "s-skill"));
            client.ReadStatus();
        }

        private static ObservationMessage ObserveOnce(TestClient client)
        {
            client.Send(new ObserveRequestMessage
            {
                Session = "s-skill",
                Seq = 1,
                WallUtc = DateTime.UtcNow,
                Mode = "once",
                WithFrame = false,
            });
            Assert.That(
                BridgeProtocol.TryReadObservation(client.ReadLine(), out ObservationMessage observation, out ProtocolError error),
                Is.True,
                error?.Message);
            return observation;
        }

        private static ExecutionRecordMessage ReadExecution(TestClient client)
        {
            Assert.That(
                BridgeProtocol.TryReadExecutionRecord(client.ReadLine(), out ExecutionRecordMessage record, out ProtocolError error),
                Is.True,
                error?.Message);
            return record;
        }

        private static ActRequestMessage Act(string actionId, long basedOnSeq, string action, double? leaseS)
        {
            return new ActRequestMessage
            {
                Session = "s-skill",
                Seq = 2,
                WallUtc = DateTime.UtcNow,
                ActionId = actionId,
                ChoiceId = "c1",
                Action = action,
                Params = new Dictionary<string, object>(),
                BasedOnSeq = basedOnSeq,
                MaxAgeS = 5.0,
                DependsOn = new List<string>(),
                LeaseS = leaseS,
            };
        }
    }
}
