// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.Threading;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;

namespace Playtest.Bridge.Tests
{
    [TestFixture]
    public sealed class BridgeStopTests
    {
        private BridgeServer _server;
        private FakeStatusSource _status;
        private ActFakeObservationSource _source;
        private FakeInputBackend _input;

        [SetUp]
        public void SetUp()
        {
            _status = new FakeStatusSource();
            _source = new ActFakeObservationSource();
            _input = new FakeInputBackend();
            BridgeActionCatalog catalog = BridgeActionCatalog.FromDefinitions(new[]
            {
                new BridgeActionDefinition
                {
                    Name = "move",
                    Backend = "input_system",
                    Device = "Keyboard",
                    Control = "w",
                    DefaultLeaseS = 0.2,
                },
            });
            _server = new BridgeServer(
                new BridgeServerOptions
                {
                    Port = 0,
                    HandshakeTimeoutMs = 3000,
                    ReadTimeoutMs = 0,
                    ReleaseVerifyTimeoutMs = 100,
                },
                _status,
                _source,
                catalog,
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
        public void Stop_ReleasesHeldInputAndClosesTheSession()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);
                client.Send(Act("a-held", observation.Seq, leaseS: 5.0));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Running));

                client.Send(Stop("user"));
                ExecutionRecordMessage cancelled = ReadExecution(client);
                StopResultMessage stopped = ReadStopResult(client);

                Assert.That(cancelled.ActionId, Is.EqualTo("a-held"));
                Assert.That(cancelled.Status, Is.EqualTo(BridgeExecStatus.Cancelled));
                Assert.That(stopped.Released, Does.Contain("move"));
                Assert.That(stopped.ReleaseVerified, Is.True);
                Assert.That(_input.Releases, Is.GreaterThanOrEqualTo(1));
                Assert.That(client.WaitForClose(), Is.True, "the bridge must close after stopped");
            }

            Assert.That(_server.IsRunning, Is.True, "stopping a session must not stop the bridge");
        }

        [TestCase("user")]
        [TestCase("reset")]
        [TestCase("scene_change")]
        [TestCase("disconnect")]
        [TestCase("error")]
        [TestCase("finish")]
        public void Stop_EveryReasonReleasesAndVerifies(string reason)
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);
                client.Send(Act("a-" + reason, observation.Seq, leaseS: 5.0));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Running));

                client.Send(Stop(reason));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Cancelled));
                StopResultMessage stopped = ReadStopResult(client);

                Assert.That(stopped.ReleaseVerified, Is.True);
                Assert.That(stopped.Released, Is.Not.Empty);
            }
        }

        [Test]
        public void Stop_WithoutAHeldLease_ReleasesNothingAndVerifies()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);

                client.Send(Stop("user"));
                StopResultMessage stopped = ReadStopResult(client);

                Assert.That(stopped.Released, Is.Empty);
                Assert.That(stopped.ReleaseVerified, Is.True);
            }
        }

        [Test]
        public void Stop_UnverifiedReleaseIsReportedAsFalse()
        {
            _input.VerifyRelease = false;
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);
                client.Send(Act("a-held", observation.Seq, leaseS: 5.0));
                ReadExecution(client);

                client.Send(Stop("user"));
                ReadExecution(client);
                StopResultMessage stopped = ReadStopResult(client);

                Assert.That(stopped.ReleaseVerified, Is.False);
            }
        }

        [Test]
        public void SceneChangeStop_ReleasesAndClosesTheActiveSession()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);
                client.Send(Act("a-held", observation.Seq, leaseS: 5.0));
                ReadExecution(client);

                StopResultMessage stopped = _server.StopActiveSession("scene_change");

                Assert.That(stopped, Is.Not.Null);
                Assert.That(stopped.ReleaseVerified, Is.True);
                Assert.That(stopped.Released, Does.Contain("move"));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Cancelled));
                Assert.That(ReadStopResult(client).ReleaseVerified, Is.True);
                Assert.That(client.WaitForClose(), Is.True);
            }
        }

        [Test]
        public void Disconnect_ReleasesTheHeldLeaseAndAcceptsTheNextSession()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);
                client.Send(Act("a-held", observation.Seq, leaseS: 5.0));
                ReadExecution(client);
            }

            Thread.Sleep(150);
            Assert.That(_input.Releases, Is.GreaterThanOrEqualTo(1));

            using (var next = new TestClient(_server))
            {
                StatusMessage status = Handshake(next, "s-next");
                Assert.That(status.Session, Is.EqualTo("s-next"));
            }
        }

        [Test]
        public void NoMessagesArriveAfterStopped()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);
                client.Send(Act("a-held", observation.Seq, leaseS: 5.0));
                ReadExecution(client);

                client.Send(Stop("user"));
                ReadExecution(client);
                ReadStopResult(client);

                Assert.That(client.WaitForClose(), Is.True);
            }
        }

        private static StatusMessage Handshake(TestClient client, string session = "s-stop")
        {
            client.Send(BridgeTestSupport.Hello(session: session));
            return client.ReadStatus();
        }

        private static ObservationMessage ObserveOnce(TestClient client)
        {
            client.Send(new ObserveRequestMessage
            {
                Session = "s-stop",
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

        private static StopResultMessage ReadStopResult(TestClient client)
        {
            string line = client.ReadLine();
            Assert.That(
                BridgeProtocol.TryReadStopResult(line, out StopResultMessage result, out ProtocolError error),
                Is.True,
                error?.Message);
            return result;
        }

        private static ActRequestMessage Act(string actionId, long basedOnSeq, double leaseS)
        {
            return new ActRequestMessage
            {
                Session = "s-stop",
                Seq = 1,
                WallUtc = DateTime.UtcNow,
                ActionId = actionId,
                ChoiceId = "c1",
                Action = "move",
                Params = new Dictionary<string, object>(),
                BasedOnSeq = basedOnSeq,
                MaxAgeS = 1.0,
                DependsOn = new List<string>(),
                LeaseS = leaseS,
            };
        }

        private static StopRequestMessage Stop(string reason)
        {
            return new StopRequestMessage
            {
                Session = "s-stop",
                Seq = 2,
                WallUtc = DateTime.UtcNow,
                Reason = reason,
            };
        }
    }
}
