using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Clock;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Transport
{
    /// <summary>
    /// Loopback TCP JSONL server. One authenticated session at a time: a second caller that
    /// passes auth gets a <c>busy</c> error immediately instead of waiting for the first to
    /// finish. Transport and observation only; game verbs arrive in later issues.
    /// </summary>
    public sealed class BridgeServer : IDisposable
    {
        private readonly BridgeServerOptions _options;
        private readonly IBridgeStatusSource _status;
        private readonly IBridgeObservationSource _observations;
        private readonly BridgeActionCatalog _actions;
        private readonly IBridgeInputBackend _input;
        private readonly IBridgeClock _clock;
        private readonly BridgeObservationHistory _history = new BridgeObservationHistory();
        private readonly BridgeActionLease _lease;
        private readonly string _token;
        private readonly object _gate = new object();
        private readonly object _freshnessGate = new object();
        private readonly HashSet<TcpClient> _clients = new HashSet<TcpClient>();
        private TcpListener _listener;
        private Thread _acceptThread;
        private volatile ClientSession _active;
        private volatile bool _running;
        private int _port;

        public BridgeServer(BridgeServerOptions options, IBridgeStatusSource status, string token)
            : this(options, status, null, null, null, token)
        {
        }

        /// <summary>
        /// <paramref name="observations"/> is optional for status-only tests; an observe request
        /// without a source is answered with an <c>internal</c> error rather than invented data.
        /// </summary>
        public BridgeServer(
            BridgeServerOptions options,
            IBridgeStatusSource status,
            IBridgeObservationSource observations,
            string token)
            : this(options, status, observations, null, null, token)
        {
        }

        /// <summary>
        /// Full constructor. <paramref name="actions"/> and <paramref name="input"/> are the
        /// manifest bindings and the input backend; without them, act requests are rejected.
        /// The caller owns <paramref name="input"/> and disposes it after the server stops; the
        /// server stops the lease and releases held input but does not dispose the backend.
        /// </summary>
        public BridgeServer(
            BridgeServerOptions options,
            IBridgeStatusSource status,
            IBridgeObservationSource observations,
            BridgeActionCatalog actions,
            IBridgeInputBackend input,
            string token)
            : this(options, status, observations, actions, input, null, token)
        {
        }

        /// <summary>
        /// Full constructor. <paramref name="actions"/>, <paramref name="input"/> and
        /// <paramref name="clock"/> are optional capabilities; without them the matching verbs
        /// are rejected. The caller owns the input backend and clock and disposes them after the
        /// server stops; the server stops the lease and releases held input but does not dispose
        /// those services.
        /// </summary>
        public BridgeServer(
            BridgeServerOptions options,
            IBridgeStatusSource status,
            IBridgeObservationSource observations,
            BridgeActionCatalog actions,
            IBridgeInputBackend input,
            IBridgeClock clock,
            string token)
        {
            _options = options ?? throw new ArgumentNullException(nameof(options));
            _status = status ?? throw new ArgumentNullException(nameof(status));
            _observations = observations;
            _actions = actions ?? BridgeActionCatalog.Empty;
            _input = input;
            _clock = clock;
            if (string.IsNullOrEmpty(token))
            {
                throw new ArgumentException("token must not be empty", nameof(token));
            }

            _token = token;
            if (_input != null)
            {
                _lease = new BridgeActionLease(
                    _input,
                    EmitLeaseRecord,
                    message => Log?.Invoke(message));
            }
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
            ClientSession active = _active;
            if (active != null)
            {
                StopSession(active, "user", sendResult: true);
            }
            else
            {
                lock (_freshnessGate)
                {
                    _clock?.Reset();
                    _lease?.Stop("server stop", emitCancelled: true);
                }
            }

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
                if (_active != null)
                {
                    _active.Close();
                    _active = null;
                }

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

                lock (_freshnessGate)
                {
                    _history.Clear();
                }

                WriteStatus(session);
                client.ReceiveTimeout = _options.ReadTimeoutMs;

                while (_running && !session.IsClosed)
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
                bool wasActive;
                lock (_gate)
                {
                    wasActive = ReferenceEquals(_active, session);
                }

                if (wasActive)
                {
                    StopSession(session, "disconnect", sendResult: false);
                }

                if (session != null)
                {
                    session.Close();
                }

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
            bool written = TryWrite(
                session,
                seq => new StatusMessage
                {
                    Session = session.Session,
                    Seq = seq,
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
                });

            if (!written)
            {
                session.Close();
            }
        }

        private void HandleLine(ClientSession session, string line)
        {
            string type = null;
            long? requestSeq = null;
            try
            {
                JObject obj = JObject.Parse(line);
                type = (string)obj["type"];
                JToken seq = obj["seq"];
                if (seq != null && seq.Type == JTokenType.Integer)
                {
                    requestSeq = (long)seq;
                }
            }
            catch (JsonException)
            {
                WriteSessionError(session, null, BridgeErrorCode.Invalid, "malformed JSON");
                return;
            }

            if (string.Equals(type, "hello", StringComparison.Ordinal))
            {
                WriteSessionError(
                    session,
                    requestSeq,
                    BridgeErrorCode.Invalid,
                    "hello is only valid as the first message");
                return;
            }

            // The contract defaults `type` to "observe", so a missing type is an observe request.
            if (type == null || string.Equals(type, "observe", StringComparison.Ordinal))
            {
                HandleObserve(session, line, requestSeq);
                return;
            }

            if (string.Equals(type, "act", StringComparison.Ordinal))
            {
                HandleAct(session, line, requestSeq);
                return;
            }

            if (string.Equals(type, "stop", StringComparison.Ordinal))
            {
                HandleStop(session, line, requestSeq);
                return;
            }

            if (string.Equals(type, "clock", StringComparison.Ordinal))
            {
                HandleClock(session, line, requestSeq);
                return;
            }

            string label = string.IsNullOrEmpty(type) ? "(missing)" : type;
            WriteSessionError(
                session,
                requestSeq,
                BridgeErrorCode.Invalid,
                $"unsupported message type '{label}'");
        }

        private void HandleObserve(ClientSession session, string line, long? requestSeq)
        {
            if (!BridgeProtocol.TryReadObserveRequest(line, out ObserveRequestMessage request, out ProtocolError error))
            {
                WriteSessionError(session, requestSeq, error.Code, error.Message);
                return;
            }

            if (_observations == null)
            {
                WriteSessionError(
                    session,
                    request.Seq,
                    BridgeErrorCode.Internal,
                    "no observation source is attached to this bridge");
                return;
            }

            if (request.WithFrame && !_observations.CaptureAvailable)
            {
                WriteSessionError(
                    session,
                    request.Seq,
                    BridgeErrorCode.Invalid,
                    "with_frame is not available: capture failed its startup self-test or there is no graphics device");
                return;
            }

            session.WithFrame = request.WithFrame;

            switch (request.Mode)
            {
                case "once":
                    SendObservation(session);
                    return;

                case "subscribe":
                    Subscribe(
                        session,
                        request.Seq,
                        request.RateHz ?? BridgeServerOptions.DefaultObservationRateHz);
                    return;

                case "unsubscribe":
                    session.StopStream();
                    return;

                default:
                    WriteSessionError(
                        session,
                        request.Seq,
                        BridgeErrorCode.Invalid,
                        "mode must be subscribe, once or unsubscribe");
                    return;
            }
        }

        private void HandleStop(ClientSession session, string line, long? requestSeq)
        {
            if (!BridgeProtocol.TryReadStopRequest(line, out StopRequestMessage request, out ProtocolError error))
            {
                WriteSessionError(session, requestSeq, error.Code, error.Message);
                return;
            }

            StopSession(session, request.Reason, sendResult: true);
        }

        private void HandleClock(ClientSession session, string line, long? requestSeq)
        {
            if (!BridgeProtocol.TryReadClockRequest(line, out ClockRequestMessage request, out ProtocolError error))
            {
                WriteSessionError(session, requestSeq, error.Code, error.Message);
                return;
            }

            if (_clock == null)
            {
                WriteSessionError(
                    session,
                    request.Seq,
                    BridgeErrorCode.Invalid,
                    "clock control is not available on this bridge");
                return;
            }

            if (!_clock.TryApply(request, out string applyError))
            {
                WriteSessionError(session, request.Seq, BridgeErrorCode.Invalid, applyError);
                return;
            }

            WriteStatus(session);
        }

        /// <summary>
        /// Stops the active session for a bridge-side reason (scene change, user, reset, error).
        /// Sends a stopped result when the caller is still connected, releases every input and
        /// verifies the release before closing. Safe to call from the Unity main thread.
        /// </summary>
        public StopResultMessage StopActiveSession(string reason)
        {
            ClientSession session = _active;
            if (session == null)
            {
                return null;
            }

            return StopSession(session, reason, sendResult: true);
        }

        private StopResultMessage StopSession(ClientSession session, string reason, bool sendResult)
        {
            if (session == null)
            {
                return null;
            }

            lock (_gate)
            {
                if (!ReferenceEquals(_active, session))
                {
                    return null;
                }
            }

            lock (_freshnessGate)
            {
                session.StopStream();
                _clock?.Reset();
                if (_lease != null)
                {
                    _lease.Stop("stop:" + reason, emitCancelled: true);
                }
                else if (_input != null)
                {
                    _input.TryRelease(out _, out _);
                }

                lock (_gate)
                {
                    if (ReferenceEquals(_active, session))
                    {
                        _active = null;
                    }
                }
            }

            var released = new List<string>();
            if (_input != null)
            {
                released.AddRange(_input.ReleasedInputs);
            }

            bool verified = VerifyRelease(_options.ReleaseVerifyTimeoutMs);

            var result = new StopResultMessage
            {
                Session = session.Session,
                Seq = 0,
                WallUtc = DateTime.UtcNow,
                Released = released,
                ReleaseVerified = verified,
            };

            if (sendResult && !session.IsClosed)
            {
                bool written = TryWrite(session, seq =>
                {
                    result.Session = session.Session;
                    result.Seq = seq;
                    result.WallUtc = DateTime.UtcNow;
                    return result;
                });

                if (!written)
                {
                    session.Close();
                }
            }

            session.Close();
            Log?.Invoke("stop (" + reason + "): released=" + released.Count + " verified=" + verified);
            return result;
        }

        private bool VerifyRelease(int timeoutMs)
        {
            if (_input == null)
            {
                return true;
            }

            DateTime deadline = DateTime.UtcNow.AddMilliseconds(Math.Max(0, timeoutMs));
            while (true)
            {
                if (_input.TryVerifyRelease(out _))
                {
                    return true;
                }

                if (DateTime.UtcNow >= deadline)
                {
                    return false;
                }

                Thread.Sleep(10);
            }
        }

        private void HandleAct(ClientSession session, string line, long? requestSeq)
        {            if (!_running)
            {
                return;
            }

            if (!BridgeProtocol.TryReadActRequest(line, out ActRequestMessage request, out ProtocolError error))
            {
                WriteSessionError(session, requestSeq, error.Code, error.Message);
                return;
            }

            if (_lease == null || _input == null)
            {
                WriteSessionError(
                    session,
                    request.Seq,
                    BridgeErrorCode.Internal,
                    "no input backend is attached to this bridge");
                return;
            }

            if (!_actions.TryGet(request.Action, out BridgeActionDefinition action))
            {
                RejectAct(session, request, BridgeRejectReason.UnknownAction, "unknown action '" + request.Action + "'", null);
                return;
            }

            lock (_freshnessGate)
            {
                if (!_running)
                {
                    return;
                }

                if (!_history.TryGet(request.BasedOnSeq, out BridgeObservationSnapshot basedOn))
                {
                    RejectAct(session, request, BridgeRejectReason.StaleAge, "no observation with seq " + request.BasedOnSeq, null);
                    return;
                }

                double ageS = (DateTime.UtcNow - basedOn.CapturedWallUtc).TotalSeconds;
                if (ageS > request.MaxAgeS)
                {
                    RejectAct(
                        session,
                        request,
                        BridgeRejectReason.StaleAge,
                        "observation is " + ageS.ToString("0.###") + " s old; max_age_s is " + request.MaxAgeS.ToString("0.###"),
                        request.BasedOnSeq);
                    return;
                }

                if (!basedOn.Playing)
                {
                    RejectAct(session, request, BridgeRejectReason.NotPlaying, "the project is not playing", request.BasedOnSeq);
                    return;
                }

                if (!_history.TryGetLatest(out BridgeObservationSnapshot latest))
                {
                    latest = basedOn;
                }

                if (!latest.Playing)
                {
                    RejectAct(session, request, BridgeRejectReason.NotPlaying, "the project is not playing", request.BasedOnSeq);
                    return;
                }

                if (!DependsOnUnchanged(basedOn, latest, request.DependsOn))
                {
                    RejectAct(session, request, BridgeRejectReason.StaleFields, "a declared field changed since seq " + request.BasedOnSeq, request.BasedOnSeq);
                    return;
                }

                if (!_lease.TryStart(request, action, request.BasedOnSeq, out string startError))
                {
                    RejectAct(session, request, BridgeRejectReason.InvalidParams, startError, request.BasedOnSeq);
                }
            }
        }

        private static bool DependsOnUnchanged(
            BridgeObservationSnapshot basedOn,
            BridgeObservationSnapshot latest,
            List<string> dependsOn)
        {
            if (dependsOn == null || dependsOn.Count == 0)
            {
                return true;
            }

            for (int i = 0; i < dependsOn.Count; i++)
            {
                string path = dependsOn[i];
                if (!BridgeFieldPath.TryResolve(basedOn, path, out object before)
                    || !BridgeFieldPath.TryResolve(latest, path, out object after))
                {
                    return false;
                }

                if (!SameValue(before, after))
                {
                    return false;
                }
            }

            return true;
        }

        private static bool SameValue(object before, object after)
        {
            if (before == null || after == null)
            {
                return before == null && after == null;
            }

            return JToken.DeepEquals(JToken.FromObject(before), JToken.FromObject(after));
        }

        private void RejectAct(
            ClientSession session,
            ActRequestMessage request,
            string reason,
            string message,
            long? validatedAgainstSeq)
        {
            Log?.Invoke("act rejected: " + reason);
            EmitExecution(session, new ExecutionRecordMessage
            {
                ActionId = request.ActionId,
                Status = BridgeExecStatus.Rejected,
                RejectReason = reason,
                ValidatedAgainstSeq = validatedAgainstSeq,
                WallUtc = DateTime.UtcNow,
            });
        }

        private void EmitLeaseRecord(ExecutionRecordMessage record)
        {
            ClientSession session = _active;
            if (session != null)
            {
                EmitExecution(session, record);
            }
        }

        private void EmitExecution(ClientSession session, ExecutionRecordMessage record)
        {
            if (session == null || session.IsClosed)
            {
                return;
            }

            bool written = TryWrite(session, seq =>
            {
                record.Session = session.Session;
                record.Seq = seq;
                record.WallUtc = DateTime.UtcNow;
                return record;
            });

            if (!written)
            {
                session.Close();
            }
        }

        private void Subscribe(ClientSession session, long requestSeq, double rateHz)
        {
            if (double.IsNaN(rateHz) || double.IsInfinity(rateHz) || rateHz <= 0 || rateHz > 60)
            {
                WriteSessionError(
                    session,
                    requestSeq,
                    BridgeErrorCode.Invalid,
                    "rate_hz must be greater than 0 and at most 60");
                return;
            }

            // The first observation acknowledges the subscription before the timer starts, so a
            // tick can never be sequenced ahead of its acknowledgement.
            if (!SendObservation(session))
            {
                return;
            }

            int periodMs = (int)Math.Round(1000.0 / rateHz, MidpointRounding.AwayFromZero);
            if (periodMs < 1)
            {
                periodMs = 1;
            }

            session.StartStream(OnObservationTick, session, periodMs);
        }

        private void OnObservationTick(object state)
        {
            var session = (ClientSession)state;
            if (session.IsClosed)
            {
                session.StopStream();
                return;
            }

            try
            {
                if (!SendObservation(session))
                {
                    session.StopStream();
                }
            }
            catch (Exception ex)
            {
                Log?.Invoke("observation stream stopped after " + ex.GetType().Name);
                session.StopStream();
            }
        }

        private bool SendObservation(ClientSession session)
        {
            if (_observations == null)
            {
                WriteSessionError(
                    session,
                    null,
                    BridgeErrorCode.Internal,
                    "no observation source is attached to this bridge");
                return false;
            }

            BridgeObservationSnapshot snapshot;
            try
            {
                snapshot = _observations.GetObservation();
            }
            catch (Exception ex)
            {
                Log?.Invoke("observation source failed: " + ex.GetType().Name);
                WriteSessionError(
                    session,
                    null,
                    BridgeErrorCode.Internal,
                    "the observation source failed");
                return false;
            }

            if (snapshot == null)
            {
                WriteSessionError(
                    session,
                    null,
                    BridgeErrorCode.Internal,
                    "the observation source returned no snapshot");
                return false;
            }

            long sentSeq = -1;
            lock (_freshnessGate)
            {
                if (!ReferenceEquals(_active, session))
                {
                    return false;
                }

                if (!TryWrite(session, seq =>
                {
                    sentSeq = seq;
                    return BuildObservation(session, snapshot, seq);
                }))
                {
                    session.Close();
                    return false;
                }

                _history.Add(sentSeq, snapshot);
            }

            return true;
        }

        private static ObservationMessage BuildObservation(
            ClientSession session,
            BridgeObservationSnapshot snapshot,
            long seq)
        {
            return new ObservationMessage
            {
                Session = session.Session,
                Seq = seq,
                GameS = snapshot.GameS,
                WallUtc = snapshot.CapturedWallUtc,
                ClockMode = snapshot.ClockMode,
                Scene = snapshot.Scene,
                Playing = snapshot.Playing,
                Paused = snapshot.Paused,
                TimeScale = snapshot.TimeScale,
                Frame = snapshot.Frame,
                Public = snapshot.Public,
                Ui = snapshot.Ui,
                FrameRef = session.WithFrame ? snapshot.FrameRef : null,
                ReaderErrors = snapshot.ReaderErrors,
            };
        }

        /// <summary>
        /// Assigns the session's next sequence number and writes the message under the per-session
        /// write lock, so wire order and seq order agree even with a stream timer running.
        /// </summary>
        private bool TryWrite(ClientSession session, Func<long, Envelope> build)
        {
            try
            {
                lock (session.WriteGate)
                {
                    Envelope message = build(session.NextSeq());
                    session.Connection.WriteLine(BridgeProtocol.Serialize(message));
                }

                return true;
            }
            catch (Exception ex) when (IsWriteFailure(ex))
            {
                Log?.Invoke("bridge write failed: " + ex.GetType().Name);
                return false;
            }
        }

        private void WriteSessionError(ClientSession session, long? inReplyTo, string code, string message)
        {
            bool written = TryWrite(
                session,
                seq => BridgeProtocol.Error(session.Session, seq, code, message, inReplyTo));

            if (!written)
            {
                session.Close();
            }
        }

        private static bool IsWriteFailure(Exception ex)
        {
            return ex is IOException
                || ex is SocketException
                || ex is ObjectDisposedException
                || ex is InvalidOperationException
                || ex is JsonException;
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

        private sealed class ClientSession
        {
            private long _nextSeq = -1;
            private int _closed;
            private Timer _timer;
            private readonly object _streamGate = new object();

            public TcpClient Client;

            public JsonLineConnection Connection;

            public string Session;

            /// <summary>Whether this session's observations carry a frame reference.</summary>
            public bool WithFrame;

            public readonly object WriteGate = new object();

            public bool IsClosed
            {
                get { return Volatile.Read(ref _closed) != 0; }
            }

            public long NextSeq()
            {
                return Interlocked.Increment(ref _nextSeq);
            }

            public void StartStream(TimerCallback callback, object state, int periodMs)
            {
                lock (_streamGate)
                {
                    StopStreamLocked();
                    if (IsClosed)
                    {
                        return;
                    }

                    _timer = new Timer(callback, state, periodMs, periodMs);
                }
            }

            public void StopStream()
            {
                lock (_streamGate)
                {
                    StopStreamLocked();
                }
            }

            public void Close()
            {
                if (Interlocked.Exchange(ref _closed, 1) != 0)
                {
                    return;
                }

                StopStream();
                try
                {
                    Client.Close();
                }
                catch (Exception)
                {
                }
            }

            private void StopStreamLocked()
            {
                Timer timer = _timer;
                _timer = null;
                if (timer != null)
                {
                    timer.Dispose();
                }
            }
        }
    }
}
