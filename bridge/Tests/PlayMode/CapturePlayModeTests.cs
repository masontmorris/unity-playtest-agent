// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections;
using System.IO;
using System.Net;
using System.Net.Sockets;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Editor;
using Playtest.Bridge.Lock;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;
using Playtest.Bridge.Unity;
using UnityEngine;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Measures the offscreen capture path with a graphics device: the startup self-test, JPEG
    /// encoding, the hidden-camera window, and the frame reference carried by an observation.
    /// </summary>
    [TestFixture]
    public sealed class CapturePlayModeTests
    {
        private static readonly Color CameraColor = new Color(0.9f, 0.1f, 0.8f, 1f);

        [UnityTest]
        public IEnumerator SelfTest_PassesAndWritesADecodableJpeg()
        {
            string directory = NewDirectory();
            UnityCapture capture = null;
            try
            {
                capture = new UnityCapture(directory, 640, 10, 5);
                Assert.That(capture.Available, Is.True, capture.LastError);
                Assert.That(capture.SelfTest, Is.EqualTo(UnityCapture.Passed), capture.LastError);
                Assert.That(File.Exists(capture.SelfTestPath), Is.True, "the self-test JPEG was not written");

                byte[] jpeg = File.ReadAllBytes(capture.SelfTestPath);
                var decoded = new Texture2D(2, 2, TextureFormat.RGBA32, false);
                try
                {
                    Assert.That(ImageConversion.LoadImage(decoded, jpeg), Is.True);
                    Assert.That(decoded.width, Is.EqualTo(640));
                    Color top = decoded.GetPixel(decoded.width / 2, Mathf.RoundToInt(decoded.height * 0.75f));
                    Color bottom = decoded.GetPixel(decoded.width / 2, Mathf.RoundToInt(decoded.height * 0.25f));
                    Assert.That(
                        Mathf.Abs(top.g - 0.15f),
                        Is.LessThan(0.16f),
                        "the overlay half of the self-test is not in the JPEG: " + top);
                    Assert.That(
                        Mathf.Abs(bottom.b - 0.85f),
                        Is.LessThan(0.16f),
                        "the camera half of the self-test is not in the JPEG: " + bottom);
                    Debug.Log("[playtest-bridge] capture self-test: " + capture.SelfTest
                        + " width=" + decoded.width);
                }
                finally
                {
                    UnityEngine.Object.DestroyImmediate(decoded);
                }

                yield return null;
            }
            finally
            {
                if (capture != null)
                {
                    capture.Dispose();
                }

                DeleteDirectory(directory);
            }
        }

        [UnityTest]
        public IEnumerator Capture_WritesAHiddenCameraFrameAndTrimsTheWindow()
        {
            string directory = NewDirectory();
            GameObject cameraHost = null;
            UnityCapture capture = null;
            try
            {
                cameraHost = MakeCamera();
                yield return null;

                capture = new UnityCapture(directory, 640, 10, 3);
                Assert.That(capture.Available, Is.True, capture.LastError);
                capture.Start();

                float deadline = Time.realtimeSinceStartup + 8f;
                yield return new WaitUntil(() => capture.Written >= 1 || Time.realtimeSinceStartup >= deadline);
                string first = capture.LatestFrameRef;
                Assert.That(first, Is.Not.Null, "the first frame was not written");
                Assert.That(File.Exists(first), Is.True);

                deadline = Time.realtimeSinceStartup + 8f;
                yield return new WaitUntil(() => capture.Written >= 5 || Time.realtimeSinceStartup >= deadline);
                Assert.That(capture.Written, Is.GreaterThanOrEqualTo(5), "capture did not keep its cadence");

                yield return null;
                string latest = capture.LatestFrameRef;
                Assert.That(latest, Is.Not.Null);
                Assert.That(latest, Is.Not.EqualTo(first), "the window did not move to a new frame");
                Assert.That(File.Exists(latest), Is.True);

                string[] frames = Directory.GetFiles(directory, "f-*.jpg");
                Assert.That(frames.Length, Is.LessThanOrEqualTo(3), "the window kept more than its size");
                Assert.That(File.Exists(first), Is.False, "the oldest frame was not trimmed");

                var decoded = new Texture2D(2, 2, TextureFormat.RGBA32, false);
                try
                {
                    Assert.That(ImageConversion.LoadImage(decoded, File.ReadAllBytes(latest)), Is.True);
                    Assert.That(decoded.width, Is.EqualTo(640));
                    Color center = decoded.GetPixel(decoded.width / 2, decoded.height / 2);
                    Assert.That(center.grayscale, Is.GreaterThan(0.1f), "the captured frame is black: " + center);
                }
                finally
                {
                    UnityEngine.Object.DestroyImmediate(decoded);
                }

                var stopwatch = System.Diagnostics.Stopwatch.StartNew();
                const int sampleCount = 5;
                for (int i = 0; i < sampleCount; i++)
                {
                    capture.CaptureOnce();
                }

                stopwatch.Stop();
                double averageMs = stopwatch.Elapsed.TotalMilliseconds / sampleCount;
                Debug.Log("[playtest-bridge] capture: written=" + capture.Written
                    + " skipped=" + capture.Skipped
                    + " window=" + frames.Length
                    + " average_capture_ms=" + averageMs.ToString("0.00"));
                Assert.That(averageMs, Is.LessThan(50.0), "one capture took too long for a 10 Hz cadence");
            }
            finally
            {
                if (capture != null)
                {
                    capture.Dispose();
                }

                if (cameraHost != null)
                {
                    UnityEngine.Object.DestroyImmediate(cameraHost);
                }

                DeleteDirectory(directory);
            }
        }

        [UnityTest]
        public IEnumerator Host_ReportsTheSelfTestInStatus()
        {
            BridgeEditorHost.Start();
            try
            {
                Assert.That(BridgeEditorHost.IsRunning, Is.True, BridgeEditorHost.LastError);
                BridgeLockInfo lockInfo = BridgeLockFile.Read(BridgeEditorHost.LockPath);
                Assert.That(lockInfo, Is.Not.Null);

                using (var client = new FrameClient(BridgeEditorHost.Port, lockInfo.Token))
                {
                    StatusMessage status = client.Handshake();
                    Assert.That(status.Capture, Is.True, "the host must report capture");
                    Assert.That(
                        status.CaptureSelfTest,
                        Is.EqualTo(UnityCapture.Passed),
                        "the startup self-test must appear in status");
                }
            }
            finally
            {
                BridgeEditorHost.Stop();
            }

            yield return null;
        }

        [UnityTest]
        public IEnumerator Observation_CarriesFrameRefOnlyWhenRequested()
        {
            string directory = NewDirectory();
            GameObject cameraHost = null;
            UnityCapture capture = null;
            UnityObservationSource observations = null;
            BridgeServer server = null;
            FrameClient client = null;
            string token = BridgeToken.Create();
            try
            {
                cameraHost = MakeCamera();
                yield return null;

                capture = new UnityCapture(directory, 640, 10, 5);
                Assert.That(capture.Available, Is.True, capture.LastError);
                capture.Start();
                float captureDeadline = Time.realtimeSinceStartup + 8f;
                yield return new WaitUntil(
                    () => capture.Written >= 1 || Time.realtimeSinceStartup >= captureDeadline);
                Assert.That(capture.Written, Is.GreaterThanOrEqualTo(1), "capture wrote no frame");

                observations = new UnityObservationSource(null, capture);
                observations.Start();
                var status = new UnityStatusSource(CurrentProcessId())
                {
                    Capture = capture.Available,
                    CaptureSelfTest = capture.SelfTest,
                };

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
                    token);
                server.Start();

                client = new FrameClient(server.Port, token);
                client.Handshake();
                client.SendObserve(withFrame: true);
                ObservationMessage withFrame = client.ReadObservation();
                Assert.That(withFrame.FrameRef, Is.Not.Null, "with_frame did not carry a frame reference");
                Assert.That(File.Exists(withFrame.FrameRef), Is.True, "the frame reference does not name a file");
                Assert.That(Path.GetExtension(withFrame.FrameRef), Is.EqualTo(".jpg"));

                client.SendObserve(withFrame: false);
                ObservationMessage withoutFrame = client.ReadObservation();
                Assert.That(withoutFrame.FrameRef, Is.Null, "frame_ref must only be sent when requested");
                Debug.Log("[playtest-bridge] capture frame_ref: written=" + capture.Written
                    + " ref=" + withFrame.FrameRef);
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

                if (observations != null)
                {
                    observations.Stop();
                    observations.Dispose();
                }

                if (capture != null)
                {
                    capture.Dispose();
                }

                if (cameraHost != null)
                {
                    UnityEngine.Object.DestroyImmediate(cameraHost);
                }

                DeleteDirectory(directory);
            }
        }

        private static GameObject MakeCamera()
        {
            var host = new GameObject("capture-playmode-camera");
            var camera = host.AddComponent<Camera>();
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = CameraColor;
            camera.cullingMask = 0;
            camera.orthographic = true;
            camera.depth = 0;
            return host;
        }

        private static string NewDirectory()
        {
            string directory = Path.Combine(
                Path.GetTempPath(),
                "upa-capture-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(directory);
            return directory;
        }

        private static int CurrentProcessId()
        {
            using (System.Diagnostics.Process process = System.Diagnostics.Process.GetCurrentProcess())
            {
                return process.Id;
            }
        }

        private static void DeleteDirectory(string directory)
        {
            try
            {
                if (Directory.Exists(directory))
                {
                    Directory.Delete(directory, true);
                }
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
        }

        private sealed class FrameClient : IDisposable
        {
            private readonly TcpClient _tcp;
            private readonly JsonLineConnection _connection;
            private readonly string _token;
            private readonly string _session = "s-capture-playmode";

            public FrameClient(int port, string token)
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
                string line = _connection.ReadLine(5000);
                Assert.That(line, Is.Not.Null, "no status reply");
                Assert.That(
                    BridgeProtocol.TryReadStatus(line, out StatusMessage status, out ProtocolError error),
                    Is.True,
                    error?.Message);
                Assert.That(status.Capture, Is.True, "status must report capture as available");
                return status;
            }

            public void SendObserve(bool withFrame)
            {
                _connection.WriteLine(BridgeProtocol.Serialize(new ObserveRequestMessage
                {
                    Session = _session,
                    Seq = 1,
                    WallUtc = DateTime.UtcNow,
                    Mode = "once",
                    WithFrame = withFrame,
                }));
            }

            public ObservationMessage ReadObservation()
            {
                string line = _connection.ReadLine(5000);
                Assert.That(line, Is.Not.Null, "no observation reply");
                Assert.That(
                    BridgeProtocol.TryReadObservation(line, out ObservationMessage observation, out ProtocolError error),
                    Is.True,
                    error?.Message);
                return observation;
            }

            public void Dispose()
            {
                _tcp.Close();
            }
        }
    }
}
