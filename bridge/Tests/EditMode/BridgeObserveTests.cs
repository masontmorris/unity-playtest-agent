// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Threading;
using NUnit.Framework;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Generic observation source for the transport tests. Every call returns a new snapshot so a
    /// stream can be distinguished from a cached copy; the values carry no game knowledge.
    /// </summary>
    internal sealed class ObserveFakeSource : IBridgeObservationSource
    {
        public static readonly DateTime Origin = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);

        private readonly object _gate = new object();
        private BridgeObservationSnapshot _snapshot;
        private int _reads;
        private long _samples;

        public ObserveFakeSource()
        {
            _snapshot = new BridgeObservationSnapshot
            {
                ClockMode = "realtime",
                Scene = "test-scene",
                Playing = true,
                Paused = false,
                TimeScale = 0.5,
                Frame = 100,
                GameS = 12.5,
                Public = new Dictionary<string, object> { { "r_value", 7 } },
                Ui = new Dictionary<string, object> { { "r_panel", true } },
                ReaderErrors = new List<ObservationReaderError>
                {
                    new ObservationReaderError { Reader = "r_missing", Error = "not present" },
                },
                CapturedWallUtc = Origin,
            };
        }

        /// <summary>Number of snapshots requested, so tests can prove a timer stopped.</summary>
        public int Reads
        {
            get
            {
                lock (_gate)
                {
                    return _reads;
                }
            }
        }

        public bool CaptureAvailable { get; set; }

        public BridgeObservationSnapshot Snapshot
        {
            get
            {
                lock (_gate)
                {
                    return _snapshot;
                }
            }

            set
            {
                lock (_gate)
                {
                    _snapshot = value;
                }
            }
        }

        public BridgeObservationSnapshot GetObservation()
        {
            lock (_gate)
            {
                _reads++;
                long sample = _samples++;
                BridgeObservationSnapshot current = _snapshot;
                return new BridgeObservationSnapshot
                {
                    ClockMode = current.ClockMode,
                    Scene = current.Scene,
                    Playing = current.Playing,
                    Paused = current.Paused,
                    TimeScale = current.TimeScale,
                    Frame = current.Frame + sample,
                    GameS = current.GameS.HasValue ? current.GameS.Value + sample * 0.01 : (double?)null,
                    Public = current.Public,
                    Ui = current.Ui,
                    FrameRef = current.FrameRef,
                    ReaderErrors = current.ReaderErrors,
                    CapturedWallUtc = current.CapturedWallUtc.AddMilliseconds(sample),
                };
            }
        }
    }

    /// <summary>
    /// Observe command and stream behaviour over the real loopback transport: one-shot samples,
    /// a paced subscription, unsubscribe, strict request rejection, and timer disposal on every
    /// way a session can end. No decision-making happens here; the bridge only reports facts.
    /// </summary>
    [TestFixture]
    public sealed class BridgeObserveTests
    {
        private BridgeServer _server;
        private FakeStatusSource _status;
        private ObserveFakeSource _observations;

        [SetUp]
        public void SetUp()
        {
            _status = new FakeStatusSource();
            _observations = new ObserveFakeSource();
            _server = new BridgeServer(
                new BridgeServerOptions
                {
                    Port = 0,
                    HandshakeTimeoutMs = 3000,
                    ReadTimeoutMs = 0,
                },
                _status,
                _observations,
                BridgeTestSupport.Token);
            _server.Start();
        }

        [TearDown]
        public void TearDown()
        {
            _server?.Dispose();
        }

        [Test]
        public void Once_SendsExactlyOneObservationWithSnapshotFields()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello(session: "s-once"));
                Assert.That(client.ReadStatus().Seq, Is.EqualTo(0));

                client.Send(Observe("once", seq: 10));

                ObservationMessage observation = ReadObservation(client);

                Assert.That(observation.Type, Is.EqualTo("observation"));
                Assert.That(observation.Session, Is.EqualTo("s-once"));
                Assert.That(observation.Seq, Is.EqualTo(1), "status is seq 0, the observation is seq 1");
                Assert.That(observation.WallUtc, Is.EqualTo(ObserveFakeSource.Origin));
                Assert.That(observation.GameS, Is.EqualTo(12.5));
                Assert.That(observation.ClockMode, Is.EqualTo("realtime"));
                Assert.That(observation.Scene, Is.EqualTo("test-scene"));
                Assert.That(observation.Playing, Is.True);
                Assert.That(observation.Paused, Is.False);
                Assert.That(observation.TimeScale, Is.EqualTo(0.5));
                Assert.That(observation.Frame, Is.EqualTo(100));
                Assert.That(observation.FrameRef, Is.Null, "frames arrive with capture, not observe");
                Assert.That(observation.Public, Is.Not.Null);
                Assert.That(Convert.ToInt64(observation.Public["r_value"]), Is.EqualTo(7));
                Assert.That(observation.Ui, Is.Not.Null);
                Assert.That(observation.Ui["r_panel"], Is.EqualTo(true));
                Assert.That(observation.ReaderErrors, Has.Count.EqualTo(1));
                Assert.That(observation.ReaderErrors[0].Reader, Is.EqualTo("r_missing"));
                Assert.That(observation.ReaderErrors[0].Error, Is.EqualTo("not present"));

                Assert.That(
                    client.Connection.ReadLine(200),
                    Is.Null,
                    "once must not start a stream or send anything else");
            }
        }

        [Test]
        public void Once_KeepsNullSnapshotValuesNull()
        {
            _observations.Snapshot = new BridgeObservationSnapshot
            {
                ClockMode = "stepped",
                Scene = "test-scene",
                Playing = false,
                Paused = true,
                TimeScale = 0,
                Frame = 5,
                GameS = null,
                Public = null,
                Ui = null,
                ReaderErrors = null,
                CapturedWallUtc = ObserveFakeSource.Origin,
            };

            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello());
                client.ReadStatus();

                client.Send(Observe("once", seq: 10));
                string line = client.ReadLine();

                Assert.That(
                    BridgeProtocol.TryReadObservation(line, out ObservationMessage observation, out ProtocolError error),
                    Is.True,
                    error?.Message);

                Assert.That(observation.GameS, Is.Null, "a missing game clock is null, never zero");
                Assert.That(observation.Ui, Is.Null);
                Assert.That(observation.FrameRef, Is.Null);

                // A null map or list is omitted rather than invented as empty: the wire model
                // applies the contract's own defaults, and a null value is never fabricated.
                Assert.That(line, Does.Not.Contain("\"game_s\""));
                Assert.That(line, Does.Not.Contain("\"public\""), "a null public map must not become an empty object");
                Assert.That(line, Does.Not.Contain("\"ui\""));
                Assert.That(line, Does.Not.Contain("\"reader_errors\""));
                Assert.That(line, Does.Not.Contain("\"frame_ref\""));
            }
        }

        [Test]
        public void Subscribe_StreamsAtAboutTwentyHertz()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello(session: "s-stream"));
                client.ReadStatus();

                client.Send(Observe("subscribe", seq: 10, rateHz: 20));

                List<ObservationMessage> observations = Collect(client, 600);

                Assert.That(observations.Count, Is.InRange(8, 14), "20 Hz over 600 ms is about twelve ticks plus the acknowledgement");
                Assert.That(observations[0].Seq, Is.EqualTo(1), "the first observation acknowledges the subscribe");
                AssertIncreasingSeq(observations);
                Assert.That(
                    observations.All(o => o.Session == "s-stream"),
                    Is.True,
                    "every streamed observation carries its session");
                Assert.That(
                    observations.Select(o => o.WallUtc).Distinct().Count(),
                    Is.EqualTo(observations.Count),
                    "each observation carries the capture time of its own snapshot");
            }
        }

        [Test]
        public void Subscribe_WithoutRate_DefaultsToTwentyHertz()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello());
                client.ReadStatus();

                client.Send(Observe("subscribe", seq: 10));

                List<ObservationMessage> observations = Collect(client, 400);

                Assert.That(
                    observations.Count,
                    Is.InRange(5, 14),
                    "the default rate is 20 Hz: about eight ticks in 400 ms, not 1 Hz and not 60 Hz");
                AssertIncreasingSeq(observations);
            }
        }

        [Test]
        public void Subscribe_AtSixtyHertz_Streams()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello());
                client.ReadStatus();

                client.Send(Observe("subscribe", seq: 10, rateHz: 60));

                List<ObservationMessage> observations = Collect(client, 400);

                Assert.That(observations.Count, Is.GreaterThan(12), "60 Hz over 400 ms is about twice the default rate");
                AssertIncreasingSeq(observations);
            }
        }

        [Test]
        public void Subscribe_ReplacesTheRate()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello());
                client.ReadStatus();

                client.Send(Observe("subscribe", seq: 10, rateHz: 10));
                List<ObservationMessage> slow = Collect(client, 250);

                client.Send(Observe("subscribe", seq: 11, rateHz: 60));
                List<ObservationMessage> fast = Collect(client, 250);

                Assert.That(slow, Is.Not.Empty);
                Assert.That(
                    fast.Count,
                    Is.GreaterThan(slow.Count + 4),
                    "a second subscribe replaces the stream rate instead of stacking a second stream");
                AssertIncreasingSeq(slow.Concat(fast).ToList());
            }
        }

        [TestCase(0.0)]
        [TestCase(61.0)]
        public void Subscribe_WithOutOfRangeRate_IsRejectedAndSessionSurvives(double rateHz)
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello(session: "s-invalid"));
                client.ReadStatus();

                client.Send(Observe("subscribe", seq: 11, rateHz: rateHz));
                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
                Assert.That(error.InReplyTo, Is.EqualTo(11));
                Assert.That(error.Seq, Is.EqualTo(1));

                client.Send(Observe("once", seq: 12));
                Assert.That(ReadObservation(client).Seq, Is.EqualTo(2), "the session stays usable after an invalid request");
            }
        }

        [Test]
        public void UnknownMode_IsRejectedAndSessionSurvives()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello(session: "s-mode"));
                client.ReadStatus();

                client.Send(Observe("stream", seq: 13));
                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
                Assert.That(error.InReplyTo, Is.EqualTo(13));

                client.Send(Observe("once", seq: 14));
                Assert.That(ReadObservation(client).Seq, Is.EqualTo(2));
            }
        }

        [Test]
        public void Observe_WithUnknownMember_IsRejectedAndSessionSurvives()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello(session: "s-strict"));
                client.ReadStatus();

                string json = BridgeProtocol.Serialize(Observe("once", seq: 15));
                client.SendRaw(json.Substring(0, json.Length - 1) + ",\"surprise\":true}");

                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
                Assert.That(error.InReplyTo, Is.EqualTo(15));
                Assert.That(error.Message, Does.Contain("surprise").IgnoreCase);

                client.Send(Observe("once", seq: 16));
                Assert.That(ReadObservation(client).Seq, Is.EqualTo(2));
            }
        }

        [Test]
        public void WithFrame_IsRejectedUntilCaptureAndSessionSurvives()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello(session: "s-frame"));
                client.ReadStatus();

                client.Send(Observe("once", seq: 17, withFrame: true));
                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
                Assert.That(error.InReplyTo, Is.EqualTo(17));
                Assert.That(error.Message, Does.Contain("with_frame"));

                Assert.That(
                    client.Connection.ReadLine(200),
                    Is.Null,
                    "a rejected with_frame request must not send an observation");

                client.Send(Observe("once", seq: 18));
                Assert.That(ReadObservation(client).Seq, Is.EqualTo(2), "the session survives a rejected with_frame request");
            }
        }

        [Test]
        public void WithFrame_CarriesTheLatestFrameRefOnceCaptureIsAvailable()
        {
            _observations.CaptureAvailable = true;
            BridgeObservationSnapshot snapshot = _observations.Snapshot;
            snapshot.FrameRef = "frames/f-000007.jpg";
            _observations.Snapshot = snapshot;

            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello(session: "s-frame"));
                client.ReadStatus();

                client.Send(Observe("once", seq: 19, withFrame: true));
                ObservationMessage withFrame = ReadObservation(client);
                Assert.That(withFrame.FrameRef, Is.EqualTo("frames/f-000007.jpg"));

                client.Send(Observe("once", seq: 20));
                ObservationMessage withoutFrame = ReadObservation(client);
                Assert.That(withoutFrame.FrameRef, Is.Null, "frame_ref is only sent when the caller asked for it");
            }
        }

        [Test]
        public void Unsubscribe_StopsTheStream()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello(session: "s-unsub"));
                client.ReadStatus();

                client.Send(Observe("subscribe", seq: 10, rateHz: 60));
                List<ObservationMessage> streaming = Collect(client, 300);
                Assert.That(streaming.Count, Is.GreaterThan(5));

                client.Send(Observe("unsubscribe", seq: 11));

                List<ObservationMessage> inFlight = Collect(client, 300);
                List<ObservationMessage> after = Collect(client, 300);

                Assert.That(
                    inFlight.Count,
                    Is.LessThanOrEqualTo(1),
                    "at most one observation may still be in flight when unsubscribe lands");
                Assert.That(after, Is.Empty, "no observation may arrive once the stream has stopped");
                AssertIncreasingSeq(streaming.Concat(inFlight).ToList());

                client.Send(Observe("once", seq: 12));
                Assert.That(
                    ReadObservation(client).Seq,
                    Is.EqualTo(1 + streaming.Count + inFlight.Count),
                    "the stream is stopped but the session and its sequence continue");
            }
        }

        [Test]
        public void StreamingSession_KeepsSecondCallerBusy()
        {
            using (var first = new TestClient(_server))
            {
                first.Send(BridgeTestSupport.Hello(session: "s-first"));
                first.ReadStatus();
                first.Send(Observe("subscribe", seq: 10, rateHz: 20, session: "s-first"));
                Assert.That(ReadObservation(first).Seq, Is.EqualTo(1));

                using (var second = new TestClient(_server))
                {
                    second.Send(BridgeTestSupport.Hello(session: "s-second"));
                    Assert.That(second.ReadError().Code, Is.EqualTo(BridgeErrorCode.Busy));
                }

                Assert.That(
                    ReadObservation(first).Seq,
                    Is.EqualTo(2),
                    "a rejected second caller must not disturb the first session's stream");
            }
        }

        [Test]
        public void Disconnect_DisposesTheStream()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello());
                client.ReadStatus();
                client.Send(Observe("subscribe", seq: 10, rateHz: 60));
                Assert.That(Collect(client, 200), Is.Not.Empty);
            }

            // The socket is closed above; the server must notice, close the session and dispose
            // the timer. A surviving timer would keep sampling at 60 Hz.
            Thread.Sleep(300);
            int stopped = _observations.Reads;
            Thread.Sleep(300);

            Assert.That(
                _observations.Reads,
                Is.EqualTo(stopped),
                "the stream timer must be disposed when the session disconnects");
        }

        [Test]
        public void Stop_DisposesTheStream()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello());
                client.ReadStatus();
                client.Send(Observe("subscribe", seq: 10, rateHz: 60));
                Assert.That(Collect(client, 200), Is.Not.Empty);

                _server.Stop();
                Assert.That(_server.IsRunning, Is.False);

                Thread.Sleep(300);
                int stopped = _observations.Reads;
                Thread.Sleep(300);

                Assert.That(
                    _observations.Reads,
                    Is.EqualTo(stopped),
                    "the stream timer must be disposed when the server stops");
            }
        }

        [Test]
        public void AfterStreamDisconnect_NextSessionGetsItsOwnStream()
        {
            using (var first = new TestClient(_server))
            {
                first.Send(BridgeTestSupport.Hello(session: "s-first"));
                first.ReadStatus();
                first.Send(Observe("subscribe", seq: 10, rateHz: 60, session: "s-first"));
                Assert.That(ReadObservation(first).Seq, Is.EqualTo(1));
            }

            Thread.Sleep(200);

            using (var next = new TestClient(_server))
            {
                next.Send(BridgeTestSupport.Hello(session: "s-next"));
                Assert.That(next.ReadStatus().Session, Is.EqualTo("s-next"));

                next.Send(Observe("subscribe", seq: 11, rateHz: 20, session: "s-next"));
                ObservationMessage acknowledgement = ReadObservation(next);

                Assert.That(acknowledgement.Session, Is.EqualTo("s-next"));
                Assert.That(acknowledgement.Seq, Is.EqualTo(1), "a new session numbers from its own status");

                List<ObservationMessage> observations = Collect(next, 200);
                Assert.That(observations, Is.Not.Empty);
                Assert.That(observations.All(o => o.Session == "s-next"), Is.True);
            }
        }

        private static ObserveRequestMessage Observe(
            string mode,
            long seq,
            double? rateHz = null,
            bool withFrame = false,
            string session = "s-test")
        {
            return new ObserveRequestMessage
            {
                Session = session,
                Seq = seq,
                WallUtc = DateTime.UtcNow,
                Mode = mode,
                WithFrame = withFrame,
                RateHz = rateHz,
            };
        }

        private static ObservationMessage ReadObservation(TestClient client)
        {
            string line = client.ReadLine();
            Assert.That(
                BridgeProtocol.TryReadObservation(line, out ObservationMessage observation, out ProtocolError error),
                Is.True,
                error?.Message);
            return observation;
        }

        /// <summary>Reads observations for <paramref name="durationMs"/> and returns them in order.</summary>
        private static List<ObservationMessage> Collect(TestClient client, int durationMs)
        {
            var observations = new List<ObservationMessage>();
            var watch = Stopwatch.StartNew();
            while (true)
            {
                int remaining = durationMs - (int)watch.ElapsedMilliseconds;
                if (remaining <= 0)
                {
                    break;
                }

                string line = client.Connection.ReadLine(remaining);
                if (line == null)
                {
                    break;
                }

                Assert.That(
                    BridgeProtocol.TryReadObservation(line, out ObservationMessage observation, out ProtocolError error),
                    Is.True,
                    "the stream produced a line that is not an observation: " + line + " (" + error?.Message + ")");
                observations.Add(observation);
            }

            return observations;
        }

        private static void AssertIncreasingSeq(IReadOnlyList<ObservationMessage> observations)
        {
            for (int i = 1; i < observations.Count; i++)
            {
                Assert.That(
                    observations[i].Seq,
                    Is.GreaterThan(observations[i - 1].Seq),
                    "observation seq must strictly increase");
            }
        }
    }
}
