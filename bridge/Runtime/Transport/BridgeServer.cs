using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Transport
{
    /// <summary>
    /// Loopback TCP JSONL server. One authenticated session at a time: a second caller that
    /// passes auth gets a <c>busy</c> error immediately instead of waiting for the first to
    /// finish. Transport only; game verbs arrive in later issues.
    /// </summary>
    public sealed class BridgeServer : IDisposable
    {
        private readonly BridgeServerOptions _options;
        private readonly IBridgeStatusSource _status;
        private readonly string _token;
        private readonly object _gate = new object();
        private readonly HashSet<TcpClient> _clients = new HashSet<TcpClient>();
        private TcpListener _listener;
        private Thread _acceptThread;
        private ClientSession _active;
        private volatile bool _running;
        private int _port;

        public BridgeServer(BridgeServerOptions options, IBridgeStatusSource status, string token)
        {
            _options = options ?? throw new ArgumentNullException(nameof(options));
            _status = status ?? throw new ArgumentNullException(nameof(status));
            if (string.IsNullOrEmpty(token))
            {
                throw new ArgumentException("token must not be empty", nameof(token));
            }

            _token = token;
        }

        public int Port
        {
            get { return _port; }
        }

        public bool IsRunning
        {
            get { return _running; }
        }

        /// <summary>Optional sink for lifecycle diagnostics. Never pass the token to it.</summary>
        public Action<string> Log { get; set; }

        public void Start()
        {
            if (_running)
            {
                throw new InvalidOperationException("bridge server is already running");
            }

            _listener = new TcpListener(_options.BindAddress, _options.Port);
            _listener.Start();
            _port = ((IPEndPoint)_listener.LocalEndpoint).Port;
            _running = true;
            _acceptThread = new Thread(AcceptLoop)
            {
                IsBackground = true,
                Name = "playtest-bridge-accept",
            };
            _acceptThread.Start();
            Log?.Invoke($"bridge listening on {_options.BindAddress}:{_port}");
        }

        public void Stop()
        {
            if (!_running)
            {
                return;
            }

            _running = false;
            try
            {
                _listener?.Stop();
            }
            catch (SocketException)
            {
            }
            catch (ObjectDisposedException)
            {
            }

            lock (_gate)
            {
                foreach (TcpClient client in _clients)
                {
                    try
                    {
                        client.Close();
                    }
                    catch (Exception)
                    {
                    }
                }

                _clients.Clear();
                _active = null;
            }

            Thread accept = _acceptThread;
            if (accept != null && accept.IsAlive && accept != Thread.CurrentThread)
            {
                accept.Join(2000);
            }

            _acceptThread = null;
            Log?.Invoke("bridge stopped");
        }

        public void Dispose()
        {
            Stop();
        }

        private void AcceptLoop()
        {
            while (_running)
            {
                TcpClient client;
                try
                {
                    client = _listener.AcceptTcpClient();
                }
                catch (SocketException)
                {
                    return;
                }
                catch (ObjectDisposedException)
                {
                    return;
                }

                lock (_gate)
                {
                    _clients.Add(client);
                }

                var thread = new Thread(() => HandleClient(client))
                {
                    IsBackground = true,
                    Name = "playtest-bridge-client",
                };
                thread.Start();
            }
        }

        private void HandleClient(TcpClient client)
        {
            ClientSession session = null;
            NetworkStream stream = null;
            try
            {
                client.NoDelay = true;
                client.ReceiveTimeout = _options.HandshakeTimeoutMs;
                client.SendTimeout = 5000;

                stream = client.GetStream();
                JsonLineConnection connection = new JsonLineConnection(stream, _options.MaxLineBytes);
                string first = connection.ReadLine(_options.HandshakeTimeoutMs);
                if (first == null)
                {
                    return;
                }

                if (!BridgeProtocol.TryReadHello(first, out HelloMessage hello, out ProtocolError parseError))
                {
                    WriteError(connection, null, 0, parseError.Code, parseError.Message, null);
                    return;
                }

                if (!ProtocolVersion.IsSupported(hello.Protocol))
                {
                    WriteError(
                        connection,
                        hello.Session,
                        0,
                        BridgeErrorCode.Protocol,
                        $"unsupported protocol '{hello.Protocol}'; this bridge speaks {ProtocolVersion.Current}",
                        hello.Seq);
                    return;
                }

                if (!BridgeToken.Matches(_token, hello.Token))
                {
                    WriteError(connection, hello.Session, 0, BridgeErrorCode.Auth, "invalid token", hello.Seq);
                    return;
                }

                session = new ClientSession
                {
                    Client = client,
                    Connection = connection,
                    Session = hello.Session,
                };

                lock (_gate)
                {
                    if (_active != null)
                    {
                        WriteError(
                            connection,
                            hello.Session,
                            0,
                            BridgeErrorCode.Busy,
                            "bridge already has an active session",
                            hello.Seq);
                        return;
                    }

                    _active = session;
                }

                WriteStatus(session);
                client.ReceiveTimeout = _options.ReadTimeoutMs;

                while (_running)
                {
                    string line = connection.ReadLine(_options.ReadTimeoutMs);
                    if (line == null)
                    {
                        break;
                    }

                    HandleLine(session, line);
                }
            }
            catch (IOException)
            {
            }
            catch (SocketException)
            {
            }
            catch (ObjectDisposedException)
            {
            }
            catch (Exception ex)
            {
                Log?.Invoke("client error: " + ex.GetType().Name);
            }
            finally
            {
                lock (_gate)
                {
                    if (ReferenceEquals(_active, session))
                    {
                        _active = null;
                    }

                    _clients.Remove(client);
                }

                try
                {
                    stream?.Dispose();
                }
                catch (Exception)
                {
                }

                try
                {
                    client.Close();
                }
                catch (Exception)
                {
                }
            }
        }

        private void WriteStatus(ClientSession session)
        {
            BridgeStatusSnapshot snapshot = _status.GetStatus() ?? new BridgeStatusSnapshot();
            var status = new StatusMessage
            {
                Session = session.Session,
                Seq = 0,
                GameS = null,
                WallUtc = DateTime.UtcNow,
                BridgeVersion = snapshot.BridgeVersion,
                UnityVersion = snapshot.UnityVersion,
                Project = snapshot.Project,
                InputBackends = new List<string>(snapshot.InputBackends ?? Array.Empty<string>()),
                ClockControl = snapshot.ClockControl,
                Capture = snapshot.Capture,
                CaptureSelfTest = snapshot.CaptureSelfTest,
                Lock = new LockState
                {
                    Held = true,
                    OwnerPid = snapshot.OwnerPid,
                    Port = _port,
                },
            };

            Write(session, status);
        }

        private void HandleLine(ClientSession session, string line)
        {
            string type = null;
            string requestSession = session.Session;
            long requestSeq = 0;
            try
            {
                JObject obj = JObject.Parse(line);
                type = (string)obj["type"];
                string parsedSession = (string)obj["session"];
                if (!string.IsNullOrEmpty(parsedSession))
                {
                    requestSession = parsedSession;
                }

                JToken seq = obj["seq"];
                if (seq != null && seq.Type == JTokenType.Integer)
                {
                    requestSeq = (long)seq;
                }
            }
            catch (JsonException)
            {
                WriteError(session.Connection, session.Session, 0, BridgeErrorCode.Invalid, "malformed JSON", null);
                return;
            }

            if (string.Equals(type, "hello", StringComparison.Ordinal))
            {
                WriteError(session.Connection, requestSession, 0, BridgeErrorCode.Invalid, "hello is only valid as the first message", requestSeq);
                return;
            }

            string label = string.IsNullOrEmpty(type) ? "(missing)" : type;
            WriteError(session.Connection, requestSession, 0, BridgeErrorCode.Invalid, $"unsupported message type '{label}'", requestSeq);
        }

        private static void WriteError(
            JsonLineConnection connection,
            string session,
            long seq,
            string code,
            string message,
            long? inReplyTo)
        {
            BridgeErrorMessage error = BridgeProtocol.Error(session, seq, code, message, inReplyTo);
            connection.WriteLine(BridgeProtocol.Serialize(error));
        }

        private static void Write(ClientSession session, Envelope message)
        {
            lock (session.WriteGate)
            {
                session.Connection.WriteLine(BridgeProtocol.Serialize(message));
            }
        }

        private sealed class ClientSession
        {
            public TcpClient Client;

            public JsonLineConnection Connection;

            public string Session;

            public readonly object WriteGate = new object();
        }
    }
}
