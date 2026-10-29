// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Clock;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;
using Playtest.Bridge.Unity;
using UnityEngine;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Measures the clock verb against the live Unity clock: pause, resume, scale and a stepped
    /// advance. The observation source is read on the main thread; the transport client only
    /// carries the requests and the status acknowledgements.
    /// </summary>
    [TestFixture]
    public sealed class ClockPlayModeTests
    {
        [UnityTest]
        public IEnumerator Clock_PauseResumeScaleAndStep()
        {
            BridgeServer server = null;
            UnityClockController clock = null;
            UnityObservationSource observations = null;
            ClockClient client = null;
            string token = BridgeToken.Create();
            try
            {
                var status = new UnityStatusSource(CurrentProcessId());
                clock = new UnityClockController();
                clock.Start();
                observations = new UnityObservationSource(clock);
                observations.Start();

                server = new BridgeServer(
                    new BridgeServerOptions
                    {
                        Port = 0,
                        HandshakeTimeoutMs = 5000,
                        ReadTimeoutMs = 0,
                    },
                    status,
                    observations,
                    BridgeActionCatalog.Empty,
                    null,
                    clock,
                    token);
                server.Start();

                client = new ClockClient(server.Port, token);
                StatusMessage hello = client.Handshake();
                Assert.That(hello.ClockControl, Is.True, "the bridge must report clock control");

                yield return null;
                BridgeObservationSnapshot initial = observations.GetObservation();
                Assert.That(initial.TimeScale, Is.EqualTo(1.0).Within(0.0001));
                Assert.That(initial.Paused, Is.False);
                Assert.That(initial.ClockMode, Is.EqualTo("realtime"));

                client.SendClock(BridgeClockOps.Pause);
                StatusMessage pausedAck = client.ReadStatus();
                Assert.That(pausedAck.ClockControl, Is.True);
                yield return null;
                yield return null;
                BridgeObservationSnapshot paused = observations.GetObservation();
                Assert.That(paused.Paused, Is.True, "pause must set paused");
                Assert.That(paused.TimeScale, Is.EqualTo(0.0).Within(0.0001), "pause must stop the clock");
                Assert.That(paused.ClockMode, Is.EqualTo("stepped"));
                Assert.That(paused.GameS, Is.Not.Null);
                double pausedGameS = paused.GameS.Value;

                client.SendClock(BridgeClockOps.Resume);
                client.ReadStatus();
                yield return null;
                yield return null;
                BridgeObservationSnapshot resumed = observations.GetObservation();
                Assert.That(resumed.Paused, Is.False);
                Assert.That(resumed.TimeScale, Is.EqualTo(1.0).Within(0.0001));
                Assert.That(resumed.ClockMode, Is.EqualTo("realtime"));
                Assert.That(resumed.GameS, Is.Not.Null);
                Assert.That(resumed.GameS.Value, Is.GreaterThan(pausedGameS));

                client.SendClock(BridgeClockOps.SetScale, timeScale: 0.5);
                client.ReadStatus();
                yield return null;
                yield return null;
                BridgeObservationSnapshot scaled = observations.GetObservation();
                Assert.That(scaled.TimeScale, Is.EqualTo(0.5).Within(0.001));
                Assert.That(scaled.Paused, Is.False);

                client.SendClock(BridgeClockOps.Pause);
                client.ReadStatus();
                yield return null;
                yield return null;
                double beforeStep = observations.GetObservation().GameS ?? 0;

                client.SendClock(BridgeClockOps.Step, stepGameS: 0.25);
                client.ReadStatus();
                float deadline = Time.realtimeSinceStartup + 2.0f;
                while (Time.realtimeSinceStartup < deadline)
                {
                    BridgeObservationSnapshot now = observations.GetObservation();
                    if (now.Paused && now.TimeScale == 0 && (now.GameS ?? 0) >= beforeStep + 0.15)
                    {
                        break;
                    }

                    yield return null;
                }

                BridgeObservationSnapshot stepped = observations.GetObservation();
                Assert.That(stepped.Paused, Is.True, "step must return to paused");
                Assert.That(stepped.TimeScale, Is.EqualTo(0.0).Within(0.0001));
                Assert.That(stepped.ClockMode, Is.EqualTo("stepped"));
                Assert.That(stepped.GameS, Is.Not.Null);
                Assert.That(stepped.GameS.Value - beforeStep, Is.GreaterThan(0.15));
                Assert.That(stepped.GameS.Value - beforeStep, Is.LessThan(0.75));
            }
            finally
            {
                if (client != null)
                {
                    client.Dispose();
                }

                if (server != null)
                {
                    server.Dispose();
                }

                if (clock != null)
                {
                    clock.Reset();
                    clock.Dispose();
                }

                if (observations != null)
                {
                    observations.Stop();
                    observations.Dispose();
                }

                Time.timeScale = 1;
            }
        }

        [UnityTest]
        public IEnumerator InputSystemBackend_ReleasesAndVerifiesAKey()
        {
            InputSystemInputBackend backend = null;
            try
            {
                backend = new InputSystemInputBackend();
                Assert.That(backend.IsAvailable, Is.True);
                var action = new BridgeActionDefinition
                {
                    Name = "move",
                    Backend = "input_system",
                    Device = "Keyboard",
                    Control = "w",
                    DefaultLeaseS = 0.2,
                };
                Assert.That(backend.TryValidate(action, out string validateError), Is.True, validateError);
                Assert.That(backend.TryApply(action, out string applyError), Is.True, applyError);
                yield return null;
                Assert.That(backend.ReleasedInputs, Is.Empty);
                Assert.That(
                    UnityEngine.InputSystem.Keyboard.current[UnityEngine.InputSystem.Key.W].isPressed,
                    Is.True,
                    "the test must hold the key before it can verify the release");

                Assert.That(backend.TryRelease(out bool releaseVerified, out string releaseError), Is.True, releaseError);
                Assert.That(releaseVerified, Is.True);
                Assert.That(backend.ReleasedInputs, Does.Contain("key:w"));

                // A stop issued from the main thread cannot wait for the next input update, so
                // verification must flush the queued state and succeed on this frame.
                Assert.That(backend.TryVerifyRelease(out string immediateError), Is.True, immediateError);
                Assert.That(backend.ReleasedInputs, Is.Empty);

                yield return null;
                Assert.That(backend.TryVerifyRelease(out string verifyError), Is.True, verifyError);
            }
            finally
            {
                if (backend != null)
                {
                    backend.Dispose();
                }

                Time.timeScale = 1;
            }
        }

        private static int CurrentProcessId()
        {
            using (Process process = Process.GetCurrentProcess())
            {
                return process.Id;
            }
        }

        private sealed class ClockClient : IDisposable
        {
            private readonly TcpClient _tcp;
            private readonly JsonLineConnection _connection;
            private readonly string _session = "s-clock-playmode";
            private readonly string _token;

            public ClockClient(int port, string token)
            {
                _token = token;
                _tcp = new TcpClient();
                _tcp.Connect(IPAddress.Loopback, port);
                _tcp.ReceiveTimeout = 5000;
                _connection = new JsonLineConnection(_tcp.GetStream(), 1024 * 1024);
                _connection.Stream.ReadTimeout = 5000;
            }

            public StatusMessage Handshake()
            {
                _connection.WriteLine(BridgeProtocol.Serialize(new HelloMessage
                {
                    Protocol = ProtocolVersion.Current,
                    Session = _session,
                    Seq = 0,
                    GameS = null,
                    WallUtc = DateTime.UtcNow,
                    Token = _token,
                    CoreVersion = "0.0.1",
                }));
                return ReadStatus();
            }

            public void SendClock(string op, double? stepGameS = null, double? timeScale = null)
            {
                _connection.WriteLine(BridgeProtocol.Serialize(new ClockRequestMessage
                {
                    Session = _session,
                    Seq = 1,
                    WallUtc = DateTime.UtcNow,
                    Op = op,
                    StepGameS = stepGameS,
                    TimeScale = timeScale,
                }));
            }

            public StatusMessage ReadStatus()
            {
                string line = _connection.ReadLine(5000);
                Assert.That(line, Is.Not.Null, "no status acknowledgement");
                Assert.That(
                    BridgeProtocol.TryReadStatus(line, out StatusMessage status, out ProtocolError error),
                    Is.True,
                    error?.Message);
                return status;
            }

            public void Dispose()
            {
                try
                {
                    _connection?.WriteLine(BridgeProtocol.Serialize(new StopRequestMessage
                    {
                        Session = _session,
                        Seq = 2,
                        WallUtc = DateTime.UtcNow,
                        Reason = BridgeStopReason.Finish,
                    }));
                }
                catch (Exception)
                {
                }

                _tcp.Close();
            }
        }
    }
}
