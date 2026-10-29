using System;
using System.Collections.Generic;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Clock;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;

namespace Playtest.Bridge.Tests
{
    internal sealed class FakeClock : IBridgeClock
    {
        public string ClockMode { get; private set; } = "realtime";
        public bool Paused { get; private set; }
        public double TimeScale { get; private set; } = 1;
        public bool Accept = true;
        public int Resets;
        public readonly List<ClockRequestMessage> Requests = new List<ClockRequestMessage>();

        public bool TryApply(ClockRequestMessage request, out string error)
        {
            Requests.Add(request);
            if (!Accept)
            {
                error = "clock refused";
                return false;
            }

            switch (request.Op)
            {
                case BridgeClockOps.Pause:
                    Paused = true;
                    ClockMode = "stepped";
                    TimeScale = 0;
                    break;
                case BridgeClockOps.Resume:
                    Paused = false;
                    ClockMode = "realtime";
                    TimeScale = 1;
                    break;
                case BridgeClockOps.SetScale:
                    TimeScale = request.TimeScale ?? 1;
                    Paused = TimeScale == 0;
                    ClockMode = Paused ? "stepped" : "realtime";
                    break;
                case BridgeClockOps.Step:
                    Paused = true;
                    ClockMode = "stepped";
                    TimeScale = 0;
                    break;
            }

            error = null;
            return true;
        }

        public void Reset()
        {
            Resets++;
            Paused = false;
            ClockMode = "realtime";
            TimeScale = 1;
        }
    }

    [TestFixture]
    public sealed class BridgeClockTests
    {
        private FakeStatusSource _status;
        private FakeClock _clock;
        private BridgeServer _server;

        [SetUp]
        public void SetUp()
        {
            _status = new FakeStatusSource();
            _status.Status.ClockControl = true;
            _clock = new FakeClock();
            _server = new BridgeServer(
                new BridgeServerOptions { Port = 0, HandshakeTimeoutMs = 3000, ReadTimeoutMs = 0 },
                _status,
                null,
                BridgeActionCatalog.Empty,
                null,
                _clock,
                BridgeTestSupport.Token);
            _server.Start();
        }

        [TearDown]
        public void TearDown()
        {
            _server?.Dispose();
        }

        [Test]
        public void Clock_PauseIsAcknowledgedWithStatus()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                client.Send(Clock(BridgeClockOps.Pause));

                StatusMessage ack = client.ReadStatus();
                Assert.That(ack.Type, Is.EqualTo("status"));
                Assert.That(ack.ClockControl, Is.True);
                Assert.That(_clock.Requests.Count, Is.EqualTo(1));
                Assert.That(_clock.Paused, Is.True);
            }
        }

        [Test]
        public void Clock_InvalidArgumentsAreRejectedAndSessionSurvives()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                client.SendRaw("{\"protocol\":\"" + ProtocolVersion.Current + "\",\"session\":\"s-clock\",\"seq\":1,\"wall_utc\":\"2026-10-26T18:00:00Z\",\"type\":\"clock\",\"op\":\"step\"}");
                BridgeErrorMessage error = client.ReadError();
                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));

                client.Send(Clock(BridgeClockOps.Pause));
                Assert.That(client.ReadStatus().Type, Is.EqualTo("status"));
            }
        }

        [Test]
        public void Clock_WithoutAControllerIsRejected()
        {
            var server = new BridgeServer(
                new BridgeServerOptions { Port = 0, HandshakeTimeoutMs = 3000, ReadTimeoutMs = 0 },
                _status,
                null,
                BridgeActionCatalog.Empty,
                null,
                null,
                BridgeTestSupport.Token);
            server.Start();
            try
            {
                using (var client = new TestClient(server))
                {
                    Handshake(client, "s-clock");
                    client.Send(Clock(BridgeClockOps.Pause));
                    BridgeErrorMessage error = client.ReadError();
                    Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
                }
            }
            finally
            {
                server.Dispose();
            }
        }

        [Test]
        public void Clock_BackendFailureIsAnInvalidError()
        {
            _clock.Accept = false;
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                client.Send(Clock(BridgeClockOps.Resume));
                BridgeErrorMessage error = client.ReadError();
                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
                Assert.That(error.InReplyTo, Is.EqualTo(1));
            }
        }

        [Test]
        public void Stop_ResetsTheClock()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                client.Send(Clock(BridgeClockOps.Pause));
                client.ReadStatus();

                client.Send(Stop());
                StopResultMessage stopped = ReadStop(client);
                Assert.That(stopped.ReleaseVerified, Is.True);
                Assert.That(_clock.Resets, Is.GreaterThanOrEqualTo(1));
            }
        }

        private static void Handshake(TestClient client, string session = "s-clock")
        {
            client.Send(BridgeTestSupport.Hello(session: session));
            client.ReadStatus();
        }

        private static ClockRequestMessage Clock(string op)
        {
            return new ClockRequestMessage
            {
                Session = "s-clock",
                Seq = 1,
                WallUtc = DateTime.UtcNow,
                Op = op,
            };
        }

        private static StopRequestMessage Stop()
        {
            return new StopRequestMessage
            {
                Session = "s-clock",
                Seq = 2,
                WallUtc = DateTime.UtcNow,
                Reason = "user",
            };
        }

        private static StopResultMessage ReadStop(TestClient client)
        {
            string line = client.ReadLine();
            Assert.That(
                BridgeProtocol.TryReadStopResult(line, out StopResultMessage result, out ProtocolError error),
                Is.True,
                error?.Message);
            return result;
        }
    }
}
