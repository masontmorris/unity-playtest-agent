// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.Globalization;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Edge cases for the act path: unverifiable freshness, dependency verification, lease
    /// retention across rejections, backend failures and sequential actions. Every action name,
    /// device and field path in this fixture is placeholder data; the bridge only carries it.
    /// </summary>
    [TestFixture]
    public sealed class BridgeActEdgeTests
    {
        private const string ActionOne = "action_one";
        private const string ActionTwo = "action_two";

        private BridgeServer _server;
        private FakeStatusSource _status;
        private ActFakeObservationSource _source;
        private FakeInputBackend _input;
        private BridgeActionCatalog _catalog;

        [SetUp]
        public void SetUp()
        {
            _status = new FakeStatusSource();
            _source = new ActFakeObservationSource();
            _input = new FakeInputBackend();
            _catalog = BridgeActionCatalog.FromDefinitions(new[]
            {
                new BridgeActionDefinition
                {
                    Name = ActionOne,
                    Backend = "input_system",
                    Device = "Device",
                    Control = "control-a",
                    DefaultLeaseS = 0.05,
                },
                new BridgeActionDefinition
                {
                    Name = ActionTwo,
                    Backend = "input_system",
                    Device = "Device",
                    Control = "control-b",
                    DefaultLeaseS = 0.05,
                },
            });
            _server = NewServer(_input);
            _server.Start();
        }

        [TearDown]
        public void TearDown()
        {
            _server?.Dispose();
        }

        [Test]
        public void Act_BasedOnSeqNeverSent_IsRejectedStaleWithoutInput()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);

                client.Send(Act("a-never-sent", observation.Seq + 1000));
                ExecutionRecordMessage rejected = ReadExecution(client);

                Assert.That(rejected.ActionId, Is.EqualTo("a-never-sent"));
                Assert.That(rejected.Status, Is.EqualTo(BridgeExecStatus.Rejected));
                Assert.That(rejected.RejectReason, Is.EqualTo(BridgeRejectReason.StaleAge));
                Assert.That(_input.Applied, Is.Empty);
                Assert.That(_input.Releases, Is.Zero);
            }
        }

        [Test]
        public void Act_MissingDependency_IsRejectedStaleFieldsWithoutInput()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);

                client.Send(Act(
                    "a-missing-dep",
                    observation.Seq,
                    dependsOn: new List<string> { "public.missing" }));
                ExecutionRecordMessage rejected = ReadExecution(client);

                Assert.That(rejected.Status, Is.EqualTo(BridgeExecStatus.Rejected));
                Assert.That(
                    rejected.RejectReason,
                    Is.EqualTo(BridgeRejectReason.StaleFields),
                    "a dependency that cannot be resolved in both observations cannot be verified");
                Assert.That(_input.Applied, Is.Empty);
                Assert.That(_input.Releases, Is.Zero);
            }
        }

        [Test]
        public void Act_UnchangedDependency_IsAcceptedAndCompletes()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage basedOn = ObserveOnce(client);
                ObservationMessage latest = ObserveOnce(client);
                Assert.That(latest.Seq, Is.GreaterThan(basedOn.Seq));

                client.Send(Act(
                    "a-unchanged-dep",
                    basedOn.Seq,
                    dependsOn: new List<string> { "public.value" }));
                ExecutionRecordMessage running = ReadExecution(client);
                ExecutionRecordMessage completed = ReadExecution(client);

                Assert.That(running.ActionId, Is.EqualTo("a-unchanged-dep"));
                Assert.That(running.Status, Is.EqualTo(BridgeExecStatus.Running));
                Assert.That(completed.Status, Is.EqualTo(BridgeExecStatus.Completed));
                Assert.That(completed.ValidatedAgainstSeq, Is.EqualTo(basedOn.Seq));
                Assert.That(completed.ReleaseVerified, Is.True);
                Assert.That(_input.Applied, Is.EqualTo(new[] { ActionOne }));
            }
        }

        [TestCase("max_age_s")]
        [TestCase("lease_s")]
        [TestCase("depends_on")]
        public void Act_Malformed_ReturnsInvalidErrorAndTheSessionSurvives(string malformedField)
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);

                switch (malformedField)
                {
                    case "max_age_s":
                        client.Send(Act("a-malformed", observation.Seq, maxAgeS: 0));
                        break;
                    case "lease_s":
                        client.Send(Act("a-malformed", observation.Seq, leaseS: -1));
                        break;
                    default:
                        client.SendRaw(ActJson("a-malformed", observation.Seq, "[\"public.value\", 7]"));
                        break;
                }

                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
                Assert.That(error.Message, Is.Not.Empty);
                Assert.That(_input.Applied, Is.Empty);

                // The contract violation is answered, not fatal: the same connection keeps working.
                _source.Current = ActFakeObservationSource.Snapshot(true, 1);
                ObservationMessage after = ObserveOnce(client);

                Assert.That(after.Seq, Is.GreaterThan(observation.Seq));
                Assert.That(_server.IsRunning, Is.True);
            }
        }

        [Test]
        public void Act_WithoutInputBackend_IsAnInternalErrorAndTheSessionSurvives()
        {
            using (var server = NewServer(null))
            {
                server.Start();
                using (var client = new TestClient(server))
                {
                    Handshake(client);
                    ObservationMessage observation = ObserveOnce(client);

                    ActRequestMessage act = Act("a-no-backend", observation.Seq);
                    client.Send(act);
                    BridgeErrorMessage error = client.ReadError();

                    Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Internal));
                    Assert.That(error.InReplyTo, Is.EqualTo(act.Seq));
                    Assert.That(server.IsRunning, Is.True);

                    ObservationMessage after = ObserveOnce(client);
                    Assert.That(after.Seq, Is.GreaterThan(observation.Seq));
                }
            }
        }

        [Test]
        public void Act_ApplyFailureAfterAdmissionIsFailedExecutionWithNoHeldInput()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);
                _input.FailApply = true;

                client.Send(Act("a-apply-fails", observation.Seq));
                ExecutionRecordMessage failed = ReadExecution(client);

                Assert.That(failed.Status, Is.EqualTo(BridgeExecStatus.Failed));
                Assert.That(failed.RejectReason, Is.Null);
                Assert.That(_input.Applied, Is.Empty, "a failed apply must not leave input held");

                _input.FailApply = false;
                _source.Current = ActFakeObservationSource.Snapshot(true, 1);
                ObservationMessage next = ObserveOnce(client);
                client.Send(Act("a-apply-ok", next.Seq));
                ExecutionRecordMessage running = ReadExecution(client);
                ExecutionRecordMessage completed = ReadExecution(client);

                Assert.That(running.ActionId, Is.EqualTo("a-apply-ok"));
                Assert.That(running.Status, Is.EqualTo(BridgeExecStatus.Running));
                Assert.That(completed.Status, Is.EqualTo(BridgeExecStatus.Completed));
                Assert.That(_input.Applied, Is.EqualTo(new[] { ActionOne }));
            }
        }

        // Release call succeeds but cannot be verified, and the release call itself fails.
        [TestCase(false, false)]
        [TestCase(true, false)]
        public void Act_UnverifiedRelease_FailsTheExecution(bool failRelease, bool releaseVerified)
        {
            _input.FailRelease = failRelease;
            _input.ReleaseVerified = releaseVerified;

            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);

                client.Send(Act("a-release", observation.Seq, leaseS: 0.05));
                ExecutionRecordMessage running = ReadExecution(client);
                ExecutionRecordMessage terminal = ReadExecution(client);

                Assert.That(running.ActionId, Is.EqualTo("a-release"));
                Assert.That(running.Status, Is.EqualTo(BridgeExecStatus.Running));
                Assert.That(terminal.ActionId, Is.EqualTo("a-release"));
                Assert.That(
                    terminal.Status,
                    Is.EqualTo(BridgeExecStatus.Failed),
                    "an unverified release is a failed execution, not a completed one");
                Assert.That(terminal.ReleaseVerified, Is.EqualTo(false));
                Assert.That(terminal.AppliedDurationS, Is.Not.Null);
                Assert.That(_input.Releases, Is.EqualTo(1));
            }
        }

        [Test]
        public void Act_NotPlaying_DoesNotReleaseTheHeldLease()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage playing = ObserveOnce(client);
                client.Send(Act("a-held", playing.Seq, leaseS: 5.0));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Running));
                int releasesWhileHeld = _input.Releases;

                _source.Current = ActFakeObservationSource.Snapshot(false, 1);
                ObservationMessage notPlaying = ObserveOnce(client);
                client.Send(Act("a-not-playing", notPlaying.Seq));
                ExecutionRecordMessage rejected = ReadExecution(client);

                Assert.That(rejected.Status, Is.EqualTo(BridgeExecStatus.Rejected));
                Assert.That(rejected.RejectReason, Is.EqualTo(BridgeRejectReason.NotPlaying));
                Assert.That(
                    _input.Releases,
                    Is.EqualTo(releasesWhileHeld),
                    "a rejected act must not release the current lease");
                Assert.That(_input.Applied, Is.EqualTo(new[] { ActionOne }));

                // The held lease is still the active one, so the next act cancels it explicitly.
                _source.Current = ActFakeObservationSource.Snapshot(true, 1);
                ObservationMessage playingAgain = ObserveOnce(client);
                client.Send(Act("a-next", playingAgain.Seq));
                ExecutionRecordMessage cancelled = ReadExecution(client);

                Assert.That(cancelled.ActionId, Is.EqualTo("a-held"));
                Assert.That(cancelled.Status, Is.EqualTo(BridgeExecStatus.Cancelled));
            }
        }

        [Test]
        public void Act_UnknownAction_DoesNotDisturbTheHeldLease()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage playing = ObserveOnce(client);
                client.Send(Act("a-held", playing.Seq, leaseS: 5.0));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Running));
                int releasesWhileHeld = _input.Releases;

                client.Send(Act("a-unknown", playing.Seq, action: "not_in_catalog"));
                ExecutionRecordMessage rejected = ReadExecution(client);

                Assert.That(rejected.Status, Is.EqualTo(BridgeExecStatus.Rejected));
                Assert.That(rejected.RejectReason, Is.EqualTo(BridgeRejectReason.UnknownAction));
                Assert.That(
                    _input.Releases,
                    Is.EqualTo(releasesWhileHeld),
                    "an unknown action must not release the current lease");
                Assert.That(_input.Applied, Is.EqualTo(new[] { ActionOne }));

                _source.Current = ActFakeObservationSource.Snapshot(true, 2);
                ObservationMessage next = ObserveOnce(client);
                client.Send(Act("a-next", next.Seq));
                ExecutionRecordMessage cancelled = ReadExecution(client);

                Assert.That(cancelled.ActionId, Is.EqualTo("a-held"));
                Assert.That(cancelled.Status, Is.EqualTo(BridgeExecStatus.Cancelled));
            }
        }

        [Test]
        public void Act_SequentialActions_CompleteIndependently()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage first = ObserveOnce(client);
                client.Send(Act("a-1", first.Seq, action: ActionOne));
                ExecutionRecordMessage firstRunning = ReadExecution(client);
                ExecutionRecordMessage firstCompleted = ReadExecution(client);

                _source.Current = ActFakeObservationSource.Snapshot(true, 2);
                ObservationMessage second = ObserveOnce(client);
                client.Send(Act("a-2", second.Seq, action: ActionTwo));
                ExecutionRecordMessage secondRunning = ReadExecution(client);
                ExecutionRecordMessage secondCompleted = ReadExecution(client);

                Assert.That(firstRunning.ActionId, Is.EqualTo("a-1"));
                Assert.That(firstCompleted.ActionId, Is.EqualTo("a-1"));
                Assert.That(firstCompleted.Status, Is.EqualTo(BridgeExecStatus.Completed));
                Assert.That(firstCompleted.ValidatedAgainstSeq, Is.EqualTo(first.Seq));
                Assert.That(firstCompleted.ReleaseVerified, Is.True);

                Assert.That(secondRunning.ActionId, Is.EqualTo("a-2"));
                Assert.That(secondCompleted.ActionId, Is.EqualTo("a-2"));
                Assert.That(secondCompleted.Status, Is.EqualTo(BridgeExecStatus.Completed));
                Assert.That(secondCompleted.ValidatedAgainstSeq, Is.EqualTo(second.Seq));
                Assert.That(secondCompleted.ReleaseVerified, Is.True);

                Assert.That(_input.Applied, Is.EqualTo(new[] { ActionOne, ActionTwo }));
            }
        }

        private BridgeServer NewServer(IBridgeInputBackend input)
        {
            return new BridgeServer(
                new BridgeServerOptions { Port = 0, HandshakeTimeoutMs = 3000, ReadTimeoutMs = 0 },
                _status,
                _source,
                _catalog,
                input,
                BridgeTestSupport.Token);
        }

        private static void Handshake(TestClient client)
        {
            client.Send(BridgeTestSupport.Hello(session: "s-act"));
            client.ReadStatus();
        }

        private static ObservationMessage ObserveOnce(TestClient client)
        {
            client.Send(new ObserveRequestMessage
            {
                Session = "s-act",
                Seq = 1,
                WallUtc = DateTime.UtcNow,
                Mode = "once",
                WithFrame = false,
            });
            string line = client.ReadLine();
            Assert.That(
                BridgeProtocol.TryReadObservation(line, out ObservationMessage observation, out ProtocolError error),
                Is.True,
                error?.Message);
            return observation;
        }

        private static ExecutionRecordMessage ReadExecution(TestClient client)
        {
            string line = client.ReadLine();
            Assert.That(
                BridgeProtocol.TryReadExecutionRecord(line, out ExecutionRecordMessage record, out ProtocolError error),
                Is.True,
                error?.Message);
            return record;
        }

        private static ActRequestMessage Act(
            string actionId,
            long basedOnSeq,
            double maxAgeS = 5.0,
            double? leaseS = 0.05,
            string action = ActionOne,
            List<string> dependsOn = null)
        {
            return new ActRequestMessage
            {
                Session = "s-act",
                Seq = 2,
                WallUtc = DateTime.UtcNow,
                ActionId = actionId,
                ChoiceId = "c1",
                Action = action,
                Params = new Dictionary<string, object>(),
                BasedOnSeq = basedOnSeq,
                MaxAgeS = maxAgeS,
                DependsOn = dependsOn ?? new List<string>(),
                LeaseS = leaseS,
            };
        }

        /// <summary>
        /// A raw act line, because a wrong-typed <c>depends_on</c> entry cannot be expressed by
        /// the typed request object. Every other field matches a valid act.
        /// </summary>
        private static string ActJson(string actionId, long basedOnSeq, string dependsOnJson)
        {
            return "{"
                + "\"protocol\":\"" + ProtocolVersion.Current + "\","
                + "\"session\":\"s-act\","
                + "\"seq\":2,"
                + "\"wall_utc\":\"" + DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture) + "\","
                + "\"type\":\"act\","
                + "\"action_id\":\"" + actionId + "\","
                + "\"choice_id\":\"c1\","
                + "\"action\":\"" + ActionOne + "\","
                + "\"params\":{},"
                + "\"based_on_seq\":" + basedOnSeq.ToString(CultureInfo.InvariantCulture) + ","
                + "\"max_age_s\":5.0,"
                + "\"depends_on\":" + dependsOnJson + ","
                + "\"lease_s\":0.05"
                + "}";
        }
    }
}
