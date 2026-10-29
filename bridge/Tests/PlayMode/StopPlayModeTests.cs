using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Threading.Tasks;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;
using Playtest.Bridge.Unity;
using UnityEngine;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Measures a stop issued from the Unity main thread, the path a scene change and the host
    /// shutdown use. A real key is held through the transport, the stop runs on the main thread,
    /// and the released state is verified from the live device rather than inferred from the call.
    /// </summary>
    [TestFixture]
    public sealed class StopPlayModeTests
    {
        [UnityTest]
        public IEnumerator Stop_FromMainThreadReleasesAndVerifiesAHeldKey()
        {
            BridgeServer server = null;
            InputSystemInputBackend input = null;
            UnityObservationSource observations = null;
            StopClient client = null;
            string token = BridgeToken.Create();
            try
            {
                var status = new UnityStatusSource(CurrentProcessId());
                input = new InputSystemInputBackend();
                observations = new UnityObservationSource();
                observations.Start();
                BridgeActionCatalog catalog = BridgeActionCatalog.FromDefinitions(new[]
                {
                    new BridgeActionDefinition
                    {
                        Name = "move",
                        Backend = "input_system",
                        Device = "Keyboard",
                        Control = "w",
                        DefaultLeaseS = 5.0,
                    },
                });

                server = new BridgeServer(
                    new BridgeServerOptions
                    {
                        Port = 0,
                        HandshakeTimeoutMs = 5000,
                        ReadTimeoutMs = 0,
                        ReleaseVerifyTimeoutMs = 1000,
                    },
                    status,
                    observations,
                    catalog,
                    input,
                    token);
                server.Start();

                client = new StopClient(server.Port, token);
                client.Handshake();

                client.SendObserveOnce();
                ObservationMessage observation = client.ReadObservation();
                Assert.That(observation.Playing, Is.True, "PlayMode must report the project as playing");
                client.SendAct("a-held", observation.Seq);

                // The bridge reports running only after a device read confirmed the key applied,
                // and that read needs this thread to keep updating, so the record is read on
                // another thread while this coroutine yields every frame.
                Task<ExecutionRecordMessage> runningRead = Task.Run(() => client.ReadExecution());
                DateTime runningDeadline = DateTime.UtcNow.AddSeconds(10);
                while (!runningRead.IsCompleted && DateTime.UtcNow < runningDeadline)
                {
                    yield return null;
                }

                Assert.That(runningRead.IsCompleted, Is.True, "no running record: the key was never confirmed applied");
                ExecutionRecordMessage running = runningRead.Result;
                Assert.That(running.Status, Is.EqualTo(BridgeExecStatus.Running));
                Assert.That(running.AppliedInputs.Count, Is.EqualTo(1));
                Assert.That(running.AppliedInputs[0].InputStartWallS, Is.Not.Null, "the receipt carries the device-confirmed start");
                yield return null;

                Assert.That(
                    UnityEngine.InputSystem.Keyboard.current[UnityEngine.InputSystem.Key.W].isPressed,
                    Is.True,
                    "the transport act must hold the key before the stop");

                StopResultMessage stopped = server.StopActiveSession("scene_change");
                Assert.That(stopped, Is.Not.Null, "the active session must stop");
                Assert.That(stopped.ReleaseVerified, Is.True, "a main-thread stop must flush and verify the release");
                Assert.That(stopped.Released, Does.Contain("key:w"));

                ExecutionRecordMessage cancelled = client.ReadExecution();
                Assert.That(cancelled.ActionId, Is.EqualTo("a-held"));
                Assert.That(cancelled.Status, Is.EqualTo(BridgeExecStatus.Cancelled));

                StopResultMessage wire = client.ReadStopped();
                Assert.That(wire.Session, Is.EqualTo("s-stop-playmode"));
                Assert.That(wire.ReleaseVerified, Is.True);
                Assert.That(wire.Released, Does.Contain("key:w"));
                Assert.That(client.WaitForClose(), Is.True, "the bridge must close after stopped");
                Assert.That(server.IsRunning, Is.True, "stopping a session must not stop the bridge");
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

                if (input != null)
                {
                    input.Dispose();
                }

                if (observations != null)
                {
                    observations.Stop();
                    observations.Dispose();
                }
            }
        }

        private static int CurrentProcessId()
        {
            using (Process process = Process.GetCurrentProcess())
            {
                return process.Id;
            }
        }

        private sealed class StopClient : IDisposable
        {
            private readonly TcpClient _tcp;
            private readonly JsonLineConnection _connection;
            private readonly string _session = "s-stop-playmode";
            private readonly string _token;

            public StopClient(int port, string token)
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
                string line = ReadLine();
                Assert.That(line, Is.Not.Null, "no status reply");
                Assert.That(
                    BridgeProtocol.TryReadStatus(line, out StatusMessage status, out ProtocolError error),
                    Is.True,
                    error?.Message);
                Assert.That(status.Session, Is.EqualTo(_session));
                return status;
            }

            public void SendObserveOnce()
            {
                _connection.WriteLine(BridgeProtocol.Serialize(new ObserveRequestMessage
                {
                    Session = _session,
                    Seq = 1,
                    WallUtc = DateTime.UtcNow,
                    Mode = "once",
                    WithFrame = false,
                }));
            }

            public ObservationMessage ReadObservation()
            {
                string line = ReadLine();
                Assert.That(line, Is.Not.Null, "no observation reply");
                Assert.That(
                    BridgeProtocol.TryReadObservation(line, out ObservationMessage observation, out ProtocolError error),
                    Is.True,
                    error?.Message);
                return observation;
            }

            public void SendAct(string actionId, long basedOnSeq)
            {
                _connection.WriteLine(BridgeProtocol.Serialize(new ActRequestMessage
                {
                    Session = _session,
                    Seq = 2,
                    WallUtc = DateTime.UtcNow,
                    ActionId = actionId,
                    ChoiceId = "c-stop",
                    Action = "move",
                    Params = new Dictionary<string, object>(),
                    BasedOnSeq = basedOnSeq,
                    MaxAgeS = 2.0,
                    DependsOn = new List<string>(),
                    LeaseS = 5.0,
                }));
            }

            public ExecutionRecordMessage ReadExecution()
            {
                string line = ReadLine();
                Assert.That(line, Is.Not.Null, "no execution record");
                Assert.That(
                    BridgeProtocol.TryReadExecutionRecord(line, out ExecutionRecordMessage record, out ProtocolError error),
                    Is.True,
                    error?.Message);
                return record;
            }

            public StopResultMessage ReadStopped()
            {
                string line = ReadLine();
                Assert.That(line, Is.Not.Null, "no stopped result");
                Assert.That(
                    BridgeProtocol.TryReadStopResult(line, out StopResultMessage result, out ProtocolError error),
                    Is.True,
                    error?.Message);
                return result;
            }

            public bool WaitForClose()
            {
                return ReadLine(3000) == null;
            }

            public void Dispose()
            {
                _tcp.Close();
            }

            private string ReadLine(int timeoutMs = 5000)
            {
                return _connection.ReadLine(timeoutMs);
            }
        }
    }
}
