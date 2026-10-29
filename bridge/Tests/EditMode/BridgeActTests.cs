// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;

namespace Playtest.Bridge.Tests
{
    internal sealed class FakeInputBackend : IBridgeInputBackend
    {
        public bool Available = true;
        public bool FailValidate;
        public bool FailApply;
        public bool FailRelease;
        public bool ReleaseVerified = true;
        public bool VerifyRelease = true;
        public readonly List<string> Applied = new List<string>();
        public readonly List<string> Released = new List<string>();
        public int Releases;

        public string Name
        {
            get { return "input_system"; }
        }

        public IReadOnlyList<string> ReleasedInputs
        {
            get { return Released; }
        }

        public bool IsAvailable
        {
            get { return Available; }
        }

        public bool TryValidate(BridgeActionDefinition action, out string error)
        {
            error = null;
            if (!Available)
            {
                error = "the fake backend is unavailable";
                return false;
            }

            if (action == null)
            {
                error = "missing action definition";
                return false;
            }

            if (FailValidate)
            {
                error = "fake validate failure";
                return false;
            }

            return true;
        }

        public bool TryApplyInputs(
            BridgeInputRun run,
            int stepIndex,
            IReadOnlyList<BridgeInputBinding> inputs,
            out BridgeInputAck ack,
            out string error)
        {
            ack = null;
            if (!Available || FailApply)
            {
                error = "fake apply failure";
                return false;
            }

            Applied.Add(run.ActionName);
            Released.Clear();
            error = null;
            return true;
        }

        public bool TryReleaseInputs(BridgeInputRun run, out bool releaseVerified, out string error)
        {
            Releases++;
            Released.Clear();
            Released.AddRange(Applied);
            releaseVerified = ReleaseVerified;
            if (FailRelease)
            {
                error = "fake release failure";
                return false;
            }

            error = null;
            return true;
        }

        public bool TryVerifyRelease(out string error)
        {
            error = VerifyRelease ? null : "fake release verification failure";
            return VerifyRelease;
        }

        public void Dispose()
        {
        }
    }

    internal sealed class ActFakeObservationSource : IBridgeObservationSource
    {
        public BridgeObservationSnapshot Current { get; set; } = Snapshot(true, 1);

        public bool CaptureAvailable { get; set; }

        public BridgeObservationSnapshot GetObservation()
        {
            return Current;
        }

        public static BridgeObservationSnapshot Snapshot(bool playing, int value)
        {
            return new BridgeObservationSnapshot
            {
                Scene = "scene-test",
                Playing = playing,
                Paused = false,
                TimeScale = 1,
                Frame = 10,
                GameS = 1.5,
                Public = new Dictionary<string, object> { { "value", value } },
                Ui = new Dictionary<string, object> { { "canvases", new List<object>() } },
                ReaderErrors = new List<ObservationReaderError>(),
                CapturedWallUtc = DateTime.UtcNow,
            };
        }
    }

    [TestFixture]
    public sealed class BridgeActTests
    {
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
                    Name = "move",
                    Backend = "input_system",
                    Device = "Keyboard",
                    Control = "w",
                    DefaultLeaseS = 0.05,
                },
            });
            _server = new BridgeServer(
                new BridgeServerOptions { Port = 0, HandshakeTimeoutMs = 3000, ReadTimeoutMs = 0 },
                _status,
                _source,
                _catalog,
                _input,
                BridgeTestSupport.Token);
            _server.Start();
        }

        [TearDown]
        public void TearDown()
        {
            _server?.Dispose();
        }

        [Test]
        public void Act_RunsOnce_ReportsMeasuredTimingAndRelease()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);

                client.Send(Act("a1", observation.Seq, leaseS: 0.05));
                ExecutionRecordMessage running = ReadExecution(client);
                ExecutionRecordMessage completed = ReadExecution(client);

                Assert.That(running.ActionId, Is.EqualTo("a1"));
                Assert.That(running.Status, Is.EqualTo(BridgeExecStatus.Running));
                Assert.That(completed.ActionId, Is.EqualTo("a1"));
                Assert.That(completed.Status, Is.EqualTo(BridgeExecStatus.Completed));
                Assert.That(completed.ValidatedAgainstSeq, Is.EqualTo(observation.Seq));
                Assert.That(completed.AppliedDurationS, Is.Not.Null);
                Assert.That(completed.AppliedDurationS.Value, Is.GreaterThan(0));
                Assert.That(completed.ReleaseVerified, Is.True);
                Assert.That(_input.Applied, Is.EqualTo(new[] { "move" }));
                Assert.That(_input.Releases, Is.GreaterThanOrEqualTo(1));
            }
        }

        [Test]
        public void Act_StaleAge_IsRejectedWithoutInput()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);
                Thread.Sleep(60);

                client.Send(Act("a-stale", observation.Seq, maxAgeS: 0.001));
                ExecutionRecordMessage rejected = ReadExecution(client);

                Assert.That(rejected.Status, Is.EqualTo(BridgeExecStatus.Rejected));
                Assert.That(rejected.RejectReason, Is.EqualTo(BridgeRejectReason.StaleAge));
                Assert.That(_input.Applied, Is.Empty);
            }
        }

        [Test]
        public void Act_ChangedDependency_IsRejectedWithoutInput()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage first = ObserveOnce(client);
                _source.Current = ActFakeObservationSource.Snapshot(true, 2);
                ObservationMessage second = ObserveOnce(client);
                Assert.That(second.Seq, Is.GreaterThan(first.Seq));

                client.Send(Act("a-fields", first.Seq, dependsOn: new List<string> { "public.value" }));
                ExecutionRecordMessage rejected = ReadExecution(client);

                Assert.That(rejected.Status, Is.EqualTo(BridgeExecStatus.Rejected));
                Assert.That(rejected.RejectReason, Is.EqualTo(BridgeRejectReason.StaleFields));
                Assert.That(_input.Applied, Is.Empty);
            }
        }

        [Test]
        public void Act_NotPlaying_IsRejected()
        {
            _source.Current = ActFakeObservationSource.Snapshot(false, 1);
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);

                client.Send(Act("a-not-playing", observation.Seq));
                ExecutionRecordMessage rejected = ReadExecution(client);

                Assert.That(rejected.Status, Is.EqualTo(BridgeExecStatus.Rejected));
                Assert.That(rejected.RejectReason, Is.EqualTo(BridgeRejectReason.NotPlaying));
                Assert.That(_input.Applied, Is.Empty);
            }
        }

        [Test]
        public void Act_UnknownAction_IsRejected()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);

                client.Send(Act("a-unknown", observation.Seq, action: "not-in-catalog"));
                ExecutionRecordMessage rejected = ReadExecution(client);

                Assert.That(rejected.Status, Is.EqualTo(BridgeExecStatus.Rejected));
                Assert.That(rejected.RejectReason, Is.EqualTo(BridgeRejectReason.UnknownAction));
                Assert.That(_input.Applied, Is.Empty);
            }
        }

        [Test]
        public void SecondLease_SupersedesTheFirst()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage first = ObserveOnce(client);
                client.Send(Act("a-first", first.Seq, leaseS: 5.0));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Running));

                _source.Current = ActFakeObservationSource.Snapshot(true, 3);
                ObservationMessage second = ObserveOnce(client);
                client.Send(Act("a-second", second.Seq, leaseS: 0.05));

                ExecutionRecordMessage cancelled = ReadExecution(client);
                ExecutionRecordMessage running = ReadExecution(client);
                ExecutionRecordMessage completed = ReadExecution(client);

                Assert.That(cancelled.ActionId, Is.EqualTo("a-first"));
                Assert.That(cancelled.Status, Is.EqualTo(BridgeExecStatus.Cancelled));
                Assert.That(cancelled.ReleaseVerified, Is.True);
                Assert.That(running.ActionId, Is.EqualTo("a-second"));
                Assert.That(running.Status, Is.EqualTo(BridgeExecStatus.Running));
                Assert.That(completed.ActionId, Is.EqualTo("a-second"));
                Assert.That(completed.Status, Is.EqualTo(BridgeExecStatus.Completed));
                Assert.That(_input.Releases, Is.GreaterThanOrEqualTo(2));
            }
        }

        [Test]
        public void StaleAct_DoesNotDisturbTheCurrentLease()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage first = ObserveOnce(client);
                client.Send(Act("a-held", first.Seq, leaseS: 5.0));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Running));
                int releasesBefore = _input.Releases;

                Thread.Sleep(60);
                client.Send(Act("a-stale", first.Seq, maxAgeS: 0.001));
                ExecutionRecordMessage rejected = ReadExecution(client);

                Assert.That(rejected.RejectReason, Is.EqualTo(BridgeRejectReason.StaleAge));
                Assert.That(_input.Releases, Is.EqualTo(releasesBefore), "a stale act must not release the current lease");
                Assert.That(_input.Applied, Is.EqualTo(new[] { "move" }));
            }
        }

        [Test]
        public void Act_OversizedLease_IsRejectedAndSessionSurvives()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);

                client.Send(Act("a-huge", observation.Seq, leaseS: 1e300));
                ExecutionRecordMessage rejected = ReadExecution(client);

                Assert.That(rejected.Status, Is.EqualTo(BridgeExecStatus.Rejected));
                Assert.That(rejected.RejectReason, Is.EqualTo(BridgeRejectReason.InvalidParams));
                Assert.That(_input.Applied, Is.Empty);

                ObservationMessage again = ObserveOnce(client);
                Assert.That(again.Seq, Is.GreaterThan(observation.Seq));
            }
        }

        [Test]
        public void Act_NullDependencyValue_IsHandledWithoutDisconnecting()
        {
            _source.Current = new BridgeObservationSnapshot
            {
                Scene = "scene-test",
                Playing = true,
                TimeScale = 1,
                Frame = 10,
                GameS = 1.5,
                Public = new Dictionary<string, object>(),
                Ui = null,
                ReaderErrors = new List<ObservationReaderError>(),
                CapturedWallUtc = DateTime.UtcNow,
            };

            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);

                client.Send(Act("a-null-field", observation.Seq, dependsOn: new List<string> { "ui" }));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Running));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Completed));
            }
        }

        [Test]
        public void Act_BackendValidationFailure_DoesNotReleaseTheHeldLease()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage first = ObserveOnce(client);
                client.Send(Act("a-held", first.Seq, leaseS: 5.0));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Running));
                int releasesBefore = _input.Releases;

                _input.FailValidate = true;
                client.Send(Act("a-invalid", first.Seq));
                ExecutionRecordMessage rejected = ReadExecution(client);
                Assert.That(rejected.RejectReason, Is.EqualTo(BridgeRejectReason.InvalidParams));
                Assert.That(_input.Releases, Is.EqualTo(releasesBefore));

                _input.FailValidate = false;
                _source.Current = ActFakeObservationSource.Snapshot(true, 2);
                ObservationMessage second = ObserveOnce(client);
                client.Send(Act("a-next", second.Seq, leaseS: 0.05));
                ExecutionRecordMessage cancelled = ReadExecution(client);
                Assert.That(cancelled.ActionId, Is.EqualTo("a-held"));
                Assert.That(cancelled.Status, Is.EqualTo(BridgeExecStatus.Cancelled));
            }
        }

        [Test]
        public void Disconnect_ReleasesTheHeldLease()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);
                client.Send(Act("a-held", observation.Seq, leaseS: 5.0));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Running));
            }

            Thread.Sleep(150);
            Assert.That(_input.Releases, Is.GreaterThanOrEqualTo(1));
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
            double maxAgeS = 1.0,
            double? leaseS = 0.05,
            string action = "move",
            List<string> dependsOn = null)
        {
            return new ActRequestMessage
            {
                Session = "s-act",
                Seq = 1,
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
    }

    [TestFixture]
    public sealed class BridgeActionCatalogTests
    {
        [Test]
        public void Load_ReadsGenericActionBindings()
        {
            string path = Path.Combine(Path.GetTempPath(), "upa-actions-" + Guid.NewGuid().ToString("N") + ".json");
            File.WriteAllText(path, "{\"actions\":{\"move\":{\"backend\":\"input_system\",\"device\":\"Keyboard\",\"control\":\"w\",\"default_lease_s\":0.3}}}");
            try
            {
                BridgeActionCatalog catalog = BridgeActionCatalog.Load(path);
                Assert.That(catalog.Count, Is.EqualTo(1));
                Assert.That(catalog.TryGet("move", out BridgeActionDefinition action), Is.True);
                Assert.That(action.Control, Is.EqualTo("w"));
                Assert.That(action.DefaultLeaseS, Is.EqualTo(0.3).Within(0.0001));
            }
            finally
            {
                File.Delete(path);
            }
        }

        [Test]
        public void Load_MissingFileIsEmpty()
        {
            Assert.That(BridgeActionCatalog.Load(Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"))).Count, Is.Zero);
        }

        [Test]
        public void Load_MalformedCatalogThrows()
        {
            string path = Path.Combine(Path.GetTempPath(), "upa-actions-" + Guid.NewGuid().ToString("N") + ".json");
            File.WriteAllText(path, "{\"actions\":{\"move\":{\"backend\":\"input_system\"}}}");
            try
            {
                Assert.Throws<InvalidDataException>(() => BridgeActionCatalog.Load(path));
            }
            finally
            {
                File.Delete(path);
            }
        }
    }
}
