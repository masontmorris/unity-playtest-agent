using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using NUnit.Framework;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;
using Playtest.Bridge.Unity;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Measures the continuous observation stream against a real running scene: rate, median
    /// interval, worst gap, wall-clock age, ordering, the baseline fields and the quiet period
    /// after unsubscribing. The test records what the stream delivered; it never decides
    /// anything, and it sends no stop request.
    /// </summary>
    [TestFixture]
    public sealed class ObservationStreamPlayModeTests
    {
        private const string SessionId = "s-playmode-observe";

        private const double StreamRateHz = 20.0;

        private const double CollectionSeconds = 3.0;

        private const int MinimumObservations = 45;

        private const int MaximumObservations = 75;

        private const double MinimumMedianIntervalMs = 40.0;

        // Windows timer resolution rounds a 50 ms wait up to ~62.5 ms in batch mode. The
        // observation-count bound below (>=45 over 3 s = 15 Hz) is the rate assertion; this
        // median bound is the same 15 Hz lower edge in interval form (1000/15 = 66.7 ms).
        private const double MaximumMedianIntervalMs = 67.0;

        private const double MaximumGapMs = 250.0;

        private const double MaximumWallAgeMs = 150.0;

        private const double UnsubscribeGraceSeconds = 0.5;

        private const double SceneSettleSeconds = 0.5;

        private const double ReplyTimeoutSeconds = 10.0;

        private const double SceneLoadTimeoutSeconds = 30.0;

        [UnityTest]
        public IEnumerator Stream_ReportsRateIntervalsAndFreshObservations()
        {
            BridgeServer server = null;
            StreamClient client = null;
            UnityObservationSource observationSource = null;
            Scene loadedScene = default(Scene);
            try
            {
                string scenePath = FirstEnabledScenePath();
                string sceneName = Path.GetFileNameWithoutExtension(scenePath);

                // The scene is the project under test. A graphics-less batch run can log errors
                // from its renderers or shaders, which are not this test's subject; only loading
                // is exempt, and the assertions below still prove the scene is the running one.
                // Anything the bridge logs after this point fails the test as usual.
                LogAssert.ignoreFailingMessages = true;
                try
                {
                    // Additive, so the runner's own bootstrap scene and controller stay alive,
                    // and so the sampler has a real active scene to report.
                    AsyncOperation loading = SceneManager.LoadSceneAsync(scenePath, LoadSceneMode.Additive);
                    Assert.That(loading, Is.Not.Null, "SceneManager refused to load " + scenePath);
                    DateTime loadDeadline = DateTime.UtcNow.AddSeconds(SceneLoadTimeoutSeconds);
                    while (!loading.isDone && DateTime.UtcNow < loadDeadline)
                    {
                        yield return null;
                    }

                    Assert.That(
                        loading.isDone,
                        Is.True,
                        $"the scene did not finish loading within {SceneLoadTimeoutSeconds:0} s: {scenePath}");

                    // One frame so the scene's own Start callbacks have run.
                    yield return null;
                    loadedScene = FindLoadedScene(scenePath);
                    Assert.That(loadedScene.IsValid(), Is.True, "the loaded scene is not known to the scene manager: " + scenePath);
                    Assert.That(loadedScene.isLoaded, Is.True, "the scene is not loaded: " + scenePath);
                    SceneManager.SetActiveScene(loadedScene);
                }
                finally
                {
                    LogAssert.ignoreFailingMessages = false;
                }

                Scene active = SceneManager.GetActiveScene();
                Assert.That(active.IsValid(), Is.True, "no scene is active after loading " + scenePath);
                Assert.That(active.path, Is.EqualTo(scenePath), "the active scene is not the one selected from Build Settings");
                Assert.That(active.name, Is.EqualTo(sceneName), "the active scene name does not match its path");

                // Let the scene settle before the measurement window: startup work on the main
                // thread would otherwise look like a stream gap, which is not what it is.
                DateTime settleDeadline = DateTime.UtcNow.AddSeconds(SceneSettleSeconds);
                while (DateTime.UtcNow < settleDeadline)
                {
                    yield return null;
                }

                string token = BridgeToken.Create();
                var statusSource = new UnityStatusSource(CurrentProcessId());
                observationSource = new UnityObservationSource();
                observationSource.Start();
                server = new BridgeServer(
                    new BridgeServerOptions
                    {
                        Port = 0,
                        HandshakeTimeoutMs = 5000,
                        ReadTimeoutMs = 0,
                    },
                    statusSource,
                    observationSource,
                    token);
                server.Start();

                // The reader runs on its own thread so this coroutine yields every frame and the
                // sampler keeps taking samples on the main thread.
                client = new StreamClient(server.Port);
                client.Send(new HelloMessage
                {
                    Protocol = ProtocolVersion.Current,
                    Session = SessionId,
                    Seq = 1,
                    GameS = null,
                    WallUtc = DateTime.UtcNow,
                    Token = token,
                    CoreVersion = "0.0.1",
                });

                DateTime handshakeDeadline = DateTime.UtcNow.AddSeconds(ReplyTimeoutSeconds);
                while (client.Status == null && DateTime.UtcNow < handshakeDeadline)
                {
                    yield return null;
                }

                Assert.That(client.Status, Is.Not.Null, "no status reply before the handshake timeout; " + client.Describe());
                Assert.That(client.Status.Session, Is.EqualTo(SessionId), "the status reply belongs to another session");

                client.Send(new ObserveRequestMessage
                {
                    Protocol = ProtocolVersion.Current,
                    Session = SessionId,
                    Seq = 2,
                    GameS = null,
                    WallUtc = DateTime.UtcNow,
                    Mode = "subscribe",
                    WithFrame = false,
                    RateHz = StreamRateHz,
                });

                DateTime firstDeadline = DateTime.UtcNow.AddSeconds(ReplyTimeoutSeconds);
                while (client.ObservationCount == 0 && DateTime.UtcNow < firstDeadline)
                {
                    yield return null;
                }

                List<ReceivedObservation> first = client.Snapshot();
                Assert.That(
                    first.Count,
                    Is.GreaterThan(0),
                    "no observation arrived after observe subscribe; " + client.Describe());

                DateTime collectionStart = first[0].ReceivedUtc;
                while ((DateTime.UtcNow - collectionStart).TotalSeconds < CollectionSeconds)
                {
                    yield return null;
                }

                DateTime collectionEnd = DateTime.UtcNow;
                var window = new List<ReceivedObservation>();
                foreach (ReceivedObservation received in client.Snapshot())
                {
                    if (received.ReceivedUtc >= collectionStart && received.ReceivedUtc <= collectionEnd)
                    {
                        window.Add(received);
                    }
                }

                Assert.That(
                    window.Count,
                    Is.InRange(MinimumObservations, MaximumObservations),
                    $"a {StreamRateHz:0} Hz stream over {CollectionSeconds:0.0} s must deliver {MinimumObservations}-{MaximumObservations} observations; " +
                    client.Describe());

                var intervals = new List<double>(window.Count - 1);
                for (int i = 1; i < window.Count; i++)
                {
                    double intervalMs = (window[i].Observation.WallUtc - window[i - 1].Observation.WallUtc).TotalMilliseconds;
                    Assert.That(
                        intervalMs,
                        Is.GreaterThanOrEqualTo(0),
                        $"wall_utc went backwards between observations at index {i}; " + client.Describe());
                    intervals.Add(intervalMs);
                }

                intervals.Sort();
                double medianIntervalMs = Median(intervals);
                double maximumGapMs = intervals[intervals.Count - 1];

                Assert.That(
                    medianIntervalMs,
                    Is.InRange(MinimumMedianIntervalMs, MaximumMedianIntervalMs),
                    $"the median interval of a {StreamRateHz:0} Hz stream must be {MinimumMedianIntervalMs:0}-{MaximumMedianIntervalMs:0} ms; " + client.Describe());
                Assert.That(
                    maximumGapMs,
                    Is.LessThan(MaximumGapMs),
                    $"no gap between consecutive observations may exceed {MaximumGapMs:0} ms; " + client.Describe());

                double maximumAgeMs = double.MinValue;
                for (int i = 0; i < window.Count; i++)
                {
                    ObservationMessage observation = window[i].Observation;
                    double ageMs = (window[i].ReceivedUtc - observation.WallUtc).TotalMilliseconds;
                    maximumAgeMs = Math.Max(maximumAgeMs, ageMs);

                    Assert.That(
                        ageMs,
                        Is.LessThanOrEqualTo(MaximumWallAgeMs),
                        $"observation {i} was {ageMs:0.0} ms old when it arrived; the stream must stay fresher than {MaximumWallAgeMs:0} ms; " +
                        client.Describe());

                    if (i > 0)
                    {
                        Assert.That(
                            observation.Seq,
                            Is.GreaterThan(window[i - 1].Observation.Seq),
                            $"seq must strictly increase but did not at index {i}; " + client.Describe());
                        Assert.That(
                            observation.Frame,
                            Is.GreaterThanOrEqualTo(window[i - 1].Observation.Frame),
                            $"frame must not go backwards but did at index {i}; " + client.Describe());
                    }

                    Assert.That(observation.Scene, Is.Not.Null.And.Not.Empty, $"observation {i} has no scene name");
                    Assert.That(
                        observation.Scene.Equals(active.name, StringComparison.Ordinal) ||
                        observation.Scene.Equals(active.path, StringComparison.Ordinal),
                        Is.True,
                        $"observation {i} reports scene '{observation.Scene}', not the running scene '{active.name}' ({active.path})");
                    Assert.That(observation.Playing, Is.True, $"observation {i} reports playing=false in Play mode");
                    Assert.That(
                        observation.TimeScale,
                        Is.EqualTo(1.0).Within(0.01),
                        $"observation {i} reports time_scale {observation.TimeScale.ToString(CultureInfo.InvariantCulture)}; nothing in this test changes it");
                    Assert.That(
                        observation.Ui,
                        Is.Not.Null,
                        $"observation {i} has ui=null; a missing reader must be reported in reader_errors instead");
                    Assert.That(
                        observation.ReaderErrors,
                        Is.Not.Null,
                        $"observation {i} has reader_errors=null; the contract requires an array");
                    foreach (ObservationReaderError readerError in observation.ReaderErrors)
                    {
                        Assert.That(readerError.Reader, Is.Not.Null.And.Not.Empty, $"observation {i} has a reader error without a reader");
                        Assert.That(readerError.Error, Is.Not.Null.And.Not.Empty, $"observation {i} has a reader error without an error");
                    }
                }

                Assert.That(
                    window[window.Count - 1].Observation.Frame,
                    Is.GreaterThan(window[0].Observation.Frame),
                    "frame must advance across the collection window; " + client.Describe());

                string summary = string.Format(
                    CultureInfo.InvariantCulture,
                    "count={0} median={1:0.0}ms max_gap={2:0.0}ms max_age={3:0.0}ms first_seq={4} last_seq={5} frame={6}->{7} scene='{8}'",
                    window.Count,
                    medianIntervalMs,
                    maximumGapMs,
                    maximumAgeMs,
                    window[0].Observation.Seq,
                    window[window.Count - 1].Observation.Seq,
                    window[0].Observation.Frame,
                    window[window.Count - 1].Observation.Frame,
                    window[0].Observation.Scene);
                UnityEngine.Debug.Log("[playtest-bridge] observation stream: " + summary);

                int receivedBeforeUnsubscribe = client.ObservationCount;
                client.Send(new ObserveRequestMessage
                {
                    Protocol = ProtocolVersion.Current,
                    Session = SessionId,
                    Seq = 3,
                    GameS = null,
                    WallUtc = DateTime.UtcNow,
                    Mode = "unsubscribe",
                    WithFrame = false,
                    RateHz = null,
                });

                DateTime graceDeadline = DateTime.UtcNow.AddSeconds(UnsubscribeGraceSeconds);
                while (DateTime.UtcNow < graceDeadline)
                {
                    yield return null;
                }

                int receivedAfterUnsubscribe = client.ObservationCount - receivedBeforeUnsubscribe;
                Assert.That(
                    receivedAfterUnsubscribe,
                    Is.LessThanOrEqualTo(1),
                    $"at most one in-flight observation may arrive after unsubscribe, but {receivedAfterUnsubscribe} did; " + client.Describe());

                List<BridgeErrorMessage> errors = client.ErrorsSnapshot();
                Assert.That(errors.Count, Is.EqualTo(0), "the stream reported bridge errors: " + client.Describe());
                Assert.That(client.UnreadCount, Is.EqualTo(0), "the stream carried lines that are not status, observation or error: " + client.Describe());
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

                if (observationSource != null)
                {
                    observationSource.Dispose();
                }

                if (loadedScene.IsValid() && loadedScene.isLoaded)
                {
                    // Leave the runner's own scene active before removing the additive one.
                    for (int i = 0; i < SceneManager.sceneCount; i++)
                    {
                        Scene candidate = SceneManager.GetSceneAt(i);
                        if (candidate.IsValid() && candidate != loadedScene)
                        {
                            SceneManager.SetActiveScene(candidate);
                            break;
                        }
                    }

                    SceneManager.UnloadSceneAsync(loadedScene);
                }
            }
        }

        /// <summary>The Play mode test needs a scene; the first enabled Build Settings scene is the subject.</summary>
        private static string FirstEnabledScenePath()
        {
            string path = SceneUtility.GetScenePathByBuildIndex(0);
            Assert.That(
                string.IsNullOrEmpty(path),
                Is.False,
                "the project has no enabled scene in Build Settings; the Play mode measurement needs one");
            return path;
        }

        /// <summary>Finds the additively loaded scene by path, falling back to its name.</summary>
        private static Scene FindLoadedScene(string scenePath)
        {
            Scene scene = SceneManager.GetSceneByPath(scenePath);
            if (scene.IsValid() && scene.isLoaded)
            {
                return scene;
            }

            string sceneName = Path.GetFileNameWithoutExtension(scenePath);
            for (int i = 0; i < SceneManager.sceneCount; i++)
            {
                Scene candidate = SceneManager.GetSceneAt(i);
                if (candidate.IsValid() &&
                    candidate.isLoaded &&
                    string.Equals(candidate.path, scenePath, StringComparison.Ordinal))
                {
                    return candidate;
                }

                if (candidate.IsValid() &&
                    candidate.isLoaded &&
                    string.Equals(candidate.name, sceneName, StringComparison.Ordinal))
                {
                    return candidate;
                }
            }

            return default(Scene);
        }

        private static double Median(List<double> sorted)
        {
            int middle = sorted.Count / 2;
            if (sorted.Count % 2 == 1)
            {
                return sorted[middle];
            }

            return (sorted[middle - 1] + sorted[middle]) / 2.0;
        }

        private static int CurrentProcessId()
        {
            using (System.Diagnostics.Process process = System.Diagnostics.Process.GetCurrentProcess())
            {
                return process.Id;
            }
        }

        /// <summary>One observation plus the wall-clock time it was read, so its age is measured at arrival.</summary>
        private sealed class ReceivedObservation
        {
            public ReceivedObservation(ObservationMessage observation, DateTime receivedUtc)
            {
                Observation = observation;
                ReceivedUtc = receivedUtc;
            }

            public ObservationMessage Observation { get; }

            public DateTime ReceivedUtc { get; }
        }

        /// <summary>
        /// Reads and parses the stream on a background thread. The main thread only sends and
        /// polls, so it yields every frame while the sampler runs.
        /// </summary>
        private sealed class StreamClient : IDisposable
        {
            private readonly object _gate = new object();
            private readonly object _writeGate = new object();
            private readonly TcpClient _tcp;
            private readonly JsonLineConnection _connection;
            private readonly Thread _reader;
            private readonly List<ReceivedObservation> _observations = new List<ReceivedObservation>();
            private readonly List<BridgeErrorMessage> _errors = new List<BridgeErrorMessage>();
            private readonly List<string> _unread = new List<string>();
            private StatusMessage _status;
            private volatile bool _closed;

            public StreamClient(int port)
            {
                _tcp = new TcpClient();
                _tcp.NoDelay = true;
                _tcp.Connect(IPAddress.Loopback, port);
                _connection = new JsonLineConnection(_tcp.GetStream(), 4 * 1024 * 1024);
                _reader = new Thread(ReadLoop)
                {
                    IsBackground = true,
                    Name = "playtest-observation-reader",
                };
                _reader.Start();
            }

            public StatusMessage Status
            {
                get
                {
                    lock (_gate)
                    {
                        return _status;
                    }
                }
            }

            public int ObservationCount
            {
                get
                {
                    lock (_gate)
                    {
                        return _observations.Count;
                    }
                }
            }

            public int UnreadCount
            {
                get
                {
                    lock (_gate)
                    {
                        return _unread.Count;
                    }
                }
            }

            public void Send(Envelope message)
            {
                string line = BridgeProtocol.Serialize(message);
                lock (_writeGate)
                {
                    _connection.WriteLine(line);
                }
            }

            public List<ReceivedObservation> Snapshot()
            {
                lock (_gate)
                {
                    return new List<ReceivedObservation>(_observations);
                }
            }

            public List<BridgeErrorMessage> ErrorsSnapshot()
            {
                lock (_gate)
                {
                    return new List<BridgeErrorMessage>(_errors);
                }
            }

            public string Describe()
            {
                lock (_gate)
                {
                    string last = _observations.Count == 0
                        ? "none"
                        : "seq " + _observations[_observations.Count - 1].Observation.Seq.ToString(CultureInfo.InvariantCulture);
                    string errors = "none";
                    if (_errors.Count > 0)
                    {
                        var builder = new System.Text.StringBuilder();
                        foreach (BridgeErrorMessage error in _errors)
                        {
                            if (builder.Length > 0)
                            {
                                builder.Append("; ");
                            }

                            builder.Append(error.Code).Append(": ").Append(error.Message);
                        }

                        errors = builder.ToString();
                    }

                    string unread = _unread.Count == 0 ? "none" : _unread[0];
                    return $"status={(_status == null ? "none" : "received")} observations={_observations.Count} last={last} errors={errors} unread={unread}";
                }
            }

            public void Dispose()
            {
                _closed = true;
                try
                {
                    _tcp.Close();
                }
                catch (Exception)
                {
                }

                if (_reader != null && _reader.IsAlive && _reader != Thread.CurrentThread)
                {
                    _reader.Join(2000);
                }

                try
                {
                    _connection.Stream.Dispose();
                }
                catch (Exception)
                {
                }
            }

            private void ReadLoop()
            {
                try
                {
                    while (!_closed)
                    {
                        string line = _connection.ReadLine(0);
                        if (line == null)
                        {
                            return;
                        }

                        if (line.Length == 0)
                        {
                            continue;
                        }

                        DateTime receivedUtc = DateTime.UtcNow;
                        if (BridgeProtocol.TryReadObservation(line, out ObservationMessage observation, out ProtocolError error))
                        {
                            lock (_gate)
                            {
                                _observations.Add(new ReceivedObservation(observation, receivedUtc));
                            }

                            continue;
                        }

                        if (BridgeProtocol.TryReadStatus(line, out StatusMessage status, out error))
                        {
                            lock (_gate)
                            {
                                _status = status;
                            }

                            continue;
                        }

                        if (BridgeProtocol.TryReadBridgeError(line, out BridgeErrorMessage bridgeError, out error))
                        {
                            lock (_gate)
                            {
                                _errors.Add(bridgeError);
                            }

                            continue;
                        }

                        lock (_gate)
                        {
                            _unread.Add(line);
                        }
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
                catch (InvalidDataException)
                {
                }
                catch (Exception ex)
                {
                    lock (_gate)
                    {
                        _unread.Add("reader failed: " + ex.GetType().Name + ": " + ex.Message);
                    }
                }
            }
        }
    }
}
