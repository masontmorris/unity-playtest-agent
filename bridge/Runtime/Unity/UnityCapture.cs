// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using UnityEngine;
using UnityEngine.UI;
#if UNITY_EDITOR
using UnityEditor;
#endif

namespace Playtest.Bridge.Unity
{
    /// <summary>
    /// Offscreen frame capture. Every enabled screen camera is rendered into a dedicated render
    /// target, then the Screen Space - Overlay canvases are lent a temporary camera and blended
    /// on top. The composite is read back, encoded as JPEG and written by a worker thread into a
    /// bounded window. Nothing reads the Game view back buffer, so capture keeps working while that
    /// view is hidden or unfocused.
    ///
    /// The startup self-test renders a temporary camera and overlay canvas through the same path
    /// and checks the pixels, so status.capture means "this path rendered", not "a render texture
    /// was created". Without a graphics device the capture stays unavailable and fails closed.
    /// </summary>
    public sealed class UnityCapture : IBridgeCapture, IDisposable
    {
        public const string Unknown = "unknown";
        public const string Passed = "passed";
        public const string Failed = "failed";

        private const int DefaultWidth = 640;
        private const int DefaultRateHz = 10;
        private const int DefaultWindowSize = 120;
        private const int JpegQuality = 80;
        private const int PendingFrameLimit = 3;
        private const float OverlayPlaneDistance = 100f;
        private const int SelfTestLayer = 5;

        private static readonly Color SelfTestCameraColor = new Color(0.1f, 0.35f, 0.85f, 1f);
        private static readonly Color SelfTestOverlayColor = new Color(0.85f, 0.15f, 0.1f, 1f);

        private readonly string _directory;
        private readonly int _width;
        private readonly int _rateHz;
        private readonly int _windowSize;
        private readonly int _mainThreadId;
        private readonly object _queueGate = new object();
        private readonly Queue<byte[]> _queue = new Queue<byte[]>();
        private readonly Queue<string> _recent = new Queue<string>();
        private readonly string _selfTestPath;

        private Thread _writer;
        private volatile bool _running;
        private volatile bool _disposed;
        private volatile string _latestRef;
        private volatile string _selfTest = Unknown;
        private bool _available;
        private string _lastError;
        private bool _errorLogged;
        private int _written;
        private int _skipped;
        private int _counter;
        private double _nextCapture;
        private int _lastFrame = -1;

        private RenderTexture _target;
        private RenderTexture _layer;
        private RenderTexture _overlayLayer;
        private Texture2D _texture;
        private Camera _overlayCamera;
        private Material _blend;

        public UnityCapture(
            string directory,
            int width = DefaultWidth,
            int rateHz = DefaultRateHz,
            int windowSize = DefaultWindowSize)
        {
            _directory = directory;
            _width = Math.Max(2, width);
            _rateHz = Math.Max(1, rateHz);
            _windowSize = Math.Max(1, windowSize);
            _mainThreadId = Thread.CurrentThread.ManagedThreadId;
            _selfTestPath = Path.Combine(_directory, "self-test.jpg");

            if (!HasGraphicsDevice())
            {
                _selfTest = Failed;
                _lastError = "no graphics device (Unity is running with -nographics or has no renderer)";
                return;
            }

            try
            {
                Directory.CreateDirectory(_directory);
                ClearDirectory();
                _available = RunSelfTest(out string selfTestError);
                _selfTest = _available ? Passed : Failed;
                if (!_available)
                {
                    _lastError = selfTestError;
                    Debug.LogWarning("playtest bridge capture self-test failed: " + selfTestError);
                }
            }
            catch (Exception ex)
            {
                _available = false;
                _selfTest = Failed;
                _lastError = ex.GetType().Name + ": " + ex.Message;
            }
        }

        public bool Available
        {
            get { return _available; }
        }

        public string SelfTest
        {
            get { return _selfTest; }
        }

        public string LatestFrameRef
        {
            get { return _latestRef; }
        }

        public string SelfTestPath
        {
            get { return _selfTestPath; }
        }

        public string LastError
        {
            get { return _lastError; }
        }

        public int Written
        {
            get { return Volatile.Read(ref _written); }
        }

        public int Skipped
        {
            get { return Volatile.Read(ref _skipped); }
        }

        public void Start()
        {
            if (!_available || _running || _disposed)
            {
                return;
            }

            _running = true;
            _nextCapture = 0;
            _lastFrame = -1;
            _writer = new Thread(WriteLoop)
            {
                IsBackground = true,
                Name = "playtest-bridge-capture-writer",
            };
            _writer.Start();
#if UNITY_EDITOR
            EditorApplication.update -= Tick;
            EditorApplication.update += Tick;
#else
            Application.onBeforeRender -= Tick;
            Application.onBeforeRender += Tick;
#endif
        }

        public void Stop()
        {
            if (!_running)
            {
                return;
            }

            _running = false;
#if UNITY_EDITOR
            EditorApplication.update -= Tick;
#else
            Application.onBeforeRender -= Tick;
#endif

            Thread writer = _writer;
            _writer = null;
            lock (_queueGate)
            {
                Monitor.PulseAll(_queueGate);
            }

            if (writer != null && writer.IsAlive && writer != Thread.CurrentThread)
            {
                writer.Join(2000);
            }
        }

        /// <summary>Renders and queues one frame now. Main thread only; used by tests.</summary>
        public bool CaptureOnce()
        {
            if (!_available || _disposed)
            {
                return false;
            }

            if (Thread.CurrentThread.ManagedThreadId != _mainThreadId)
            {
                return false;
            }

            try
            {
                int height = TargetHeight();
                EnsureTargets(_width, height);
                if (!RenderComposite(_target, null, null))
                {
                    RecordError("no enabled screen camera rendered a frame");
                    return false;
                }

                ReadTarget(_target, _texture);
                byte[] jpeg = ImageConversion.EncodeToJPG(_texture, JpegQuality);
                if (jpeg == null || jpeg.Length == 0)
                {
                    RecordError("JPEG encoding produced no data");
                    return false;
                }

                Enqueue(jpeg);
                return true;
            }
            catch (Exception ex)
            {
                RecordError(ex.GetType().Name + ": " + ex.Message);
                return false;
            }
        }

        public void Dispose()
        {
            if (_disposed)
            {
                return;
            }

            _disposed = true;
            Stop();
            DestroyTargets();
            ReleaseHelpers();
        }

        private void Tick()
        {
            if (!_running || _disposed)
            {
                return;
            }

            if (Thread.CurrentThread.ManagedThreadId != _mainThreadId)
            {
                return;
            }

            if (!Application.isPlaying)
            {
                return;
            }

            double now = Now();
            if (now < _nextCapture)
            {
                return;
            }

            int frame = Time.frameCount;
            if (frame == _lastFrame)
            {
                return;
            }

            _nextCapture = now + 1.0 / _rateHz;
            if (_nextCapture < now)
            {
                _nextCapture = now + 1.0 / _rateHz;
            }

            _lastFrame = frame;
            CaptureOnce();
        }

        private static bool HasGraphicsDevice()
        {
            return SystemInfo.graphicsDeviceType != UnityEngine.Rendering.GraphicsDeviceType.Null
                && SystemInfo.supportsRenderTextures;
        }

        private bool RunSelfTest(out string error)
        {
            error = null;
            GameObject cameraHost = null;
            GameObject canvasHost = null;
            try
            {
                var cameraObject = new GameObject("playtest-bridge-capture-self-test-camera");
                cameraObject.hideFlags = HideFlags.HideAndDontSave;
                cameraHost = cameraObject;
                var camera = cameraObject.AddComponent<Camera>();
                camera.enabled = false;
                camera.clearFlags = CameraClearFlags.SolidColor;
                camera.backgroundColor = SelfTestCameraColor;
                camera.cullingMask = 0;
                camera.orthographic = true;
                camera.depth = -100;

                var canvasObject = new GameObject(
                    "playtest-bridge-capture-self-test-canvas",
                    typeof(RectTransform),
                    typeof(Canvas));
                canvasObject.hideFlags = HideFlags.HideAndDontSave;
                canvasObject.layer = SelfTestLayer;
                canvasHost = canvasObject;
                var canvas = canvasObject.GetComponent<Canvas>();
                canvas.renderMode = RenderMode.ScreenSpaceOverlay;
                canvas.sortingOrder = 30000;

                var imageObject = new GameObject(
                    "overlay",
                    typeof(RectTransform),
                    typeof(CanvasRenderer),
                    typeof(Image));
                imageObject.hideFlags = HideFlags.HideAndDontSave;
                imageObject.layer = SelfTestLayer;
                imageObject.transform.SetParent(canvasObject.transform, false);
                var imageRect = imageObject.GetComponent<RectTransform>();
                imageRect.anchorMin = new Vector2(0f, 0.5f);
                imageRect.anchorMax = new Vector2(1f, 1f);
                imageRect.offsetMin = Vector2.zero;
                imageRect.offsetMax = Vector2.zero;
                var image = imageObject.GetComponent<Image>();
                image.color = SelfTestOverlayColor;
                image.raycastTarget = false;

                EnsureTargets(_width, 360);
                if (!RenderComposite(_target, new[] { camera }, new[] { canvas }))
                {
                    error = "the self-test composite rendered nothing";
                    return false;
                }

                ReadTarget(_target, _texture);
                Color top = _texture.GetPixel(_texture.width / 2, Mathf.RoundToInt(_texture.height * 0.75f));
                Color bottom = _texture.GetPixel(_texture.width / 2, Mathf.RoundToInt(_texture.height * 0.25f));
                if (!Near(top, SelfTestOverlayColor) || !Near(bottom, SelfTestCameraColor))
                {
                    error = "the self-test composite pixels are wrong: top=" + Describe(top)
                        + " bottom=" + Describe(bottom);
                    return false;
                }

                byte[] jpeg = ImageConversion.EncodeToJPG(_texture, JpegQuality);
                if (jpeg == null || jpeg.Length == 0)
                {
                    error = "the self-test JPEG encoder produced no data";
                    return false;
                }

                File.WriteAllBytes(_selfTestPath, jpeg);
                var decoded = new Texture2D(2, 2, TextureFormat.RGBA32, false);
                try
                {
                    if (!ImageConversion.LoadImage(decoded, jpeg))
                    {
                        error = "the self-test JPEG is unreadable";
                        return false;
                    }

                    Color decodedTop = decoded.GetPixel(
                        decoded.width / 2,
                        Mathf.RoundToInt(decoded.height * 0.75f));
                    Color decodedBottom = decoded.GetPixel(
                        decoded.width / 2,
                        Mathf.RoundToInt(decoded.height * 0.25f));
                    if (!Near(decodedTop, SelfTestOverlayColor) || !Near(decodedBottom, SelfTestCameraColor))
                    {
                        error = "the self-test JPEG pixels are wrong: top=" + Describe(decodedTop)
                            + " bottom=" + Describe(decodedBottom);
                        return false;
                    }
                }
                finally
                {
                    UnityEngine.Object.DestroyImmediate(decoded);
                }

                return true;
            }
            catch (Exception ex)
            {
                error = ex.GetType().Name + ": " + ex.Message;
                return false;
            }
            finally
            {
                if (canvasHost != null)
                {
                    UnityEngine.Object.DestroyImmediate(canvasHost);
                }

                if (cameraHost != null)
                {
                    UnityEngine.Object.DestroyImmediate(cameraHost);
                }
            }
        }

        private bool RenderComposite(
            RenderTexture target,
            IList<Camera> cameras,
            IList<Canvas> canvases)
        {
            if (target == null || !target.IsCreated())
            {
                return false;
            }

            List<Camera> cameraList = cameras != null
                ? new List<Camera>(cameras)
                : ScreenCameras();
            if (cameraList.Count == 0)
            {
                return false;
            }

            RenderTexture previousActive = RenderTexture.active;
            try
            {
                ClearTarget(target);
                bool first = true;
                foreach (Camera camera in cameraList)
                {
                    if (camera == null)
                    {
                        continue;
                    }

                    RenderTexture destination = first ? target : _layer;
                    if (!RenderCamera(camera, destination, first))
                    {
                        continue;
                    }

                    if (!first)
                    {
                        Blit(_layer, target);
                    }

                    first = false;
                }

                if (first)
                {
                    return false;
                }

                RenderOverlayCanvases(target, canvases);
                return true;
            }
            finally
            {
                RenderTexture.active = previousActive;
            }
        }

        /// <summary>
        /// The cameras that draw to the screen, in compositing order. The order is shared with the
        /// game-view reading, so the view the bridge reports is the camera this capture draws first.
        /// </summary>
        private static List<Camera> ScreenCameras()
        {
            return UnityGameView.ScreenCameras();
        }

        private bool RenderCamera(Camera camera, RenderTexture destination, bool first)
        {
            RenderTexture previousTarget = camera.targetTexture;
            CameraClearFlags previousClear = camera.clearFlags;
            Color previousBackground = camera.backgroundColor;
            RenderTexture previousActive = RenderTexture.active;
            try
            {
                if (!first)
                {
                    camera.clearFlags = CameraClearFlags.SolidColor;
                    camera.backgroundColor = Color.clear;
                }

                camera.targetTexture = destination;
                camera.Render();
                return true;
            }
            catch (Exception ex)
            {
                RecordError("camera " + camera.name + " did not render: " + ex.GetType().Name + ": " + ex.Message);
                return false;
            }
            finally
            {
                camera.targetTexture = previousTarget;
                camera.clearFlags = previousClear;
                camera.backgroundColor = previousBackground;
                RenderTexture.active = previousActive;
            }
        }

        private void RenderOverlayCanvases(RenderTexture target, IList<Canvas> canvases)
        {
            var switched = new List<Canvas>();
            int cullingMask = 0;
            if (canvases != null)
            {
                foreach (Canvas canvas in canvases)
                {
                    AddOverlayCanvas(canvas, switched, ref cullingMask, requireScene: false);
                }
            }
            else
            {
                foreach (Canvas canvas in Resources.FindObjectsOfTypeAll<Canvas>())
                {
                    AddOverlayCanvas(canvas, switched, ref cullingMask, requireScene: true);
                }
            }

            if (switched.Count == 0 || cullingMask == 0 || _overlayLayer == null)
            {
                return;
            }

            Camera overlayCamera = EnsureOverlayCamera(target.width, target.height, cullingMask);
            if (overlayCamera == null)
            {
                return;
            }

            var modes = new RenderMode[switched.Count];
            var worldCameras = new Camera[switched.Count];
            var distances = new float[switched.Count];
            try
            {
                ClearTarget(_overlayLayer);
                for (int i = 0; i < switched.Count; i++)
                {
                    Canvas canvas = switched[i];
                    modes[i] = canvas.renderMode;
                    worldCameras[i] = canvas.worldCamera;
                    distances[i] = canvas.planeDistance;
                    canvas.worldCamera = overlayCamera;
                    canvas.planeDistance = OverlayPlaneDistance;
                    canvas.renderMode = RenderMode.ScreenSpaceCamera;
                }

                Canvas.ForceUpdateCanvases();
                overlayCamera.targetTexture = _overlayLayer;
                overlayCamera.enabled = true;
                overlayCamera.Render();
            }
            catch (Exception ex)
            {
                RecordError("overlay canvases did not render: " + ex.GetType().Name + ": " + ex.Message);
            }
            finally
            {
                overlayCamera.enabled = false;
                overlayCamera.targetTexture = null;
                for (int i = 0; i < switched.Count; i++)
                {
                    Canvas canvas = switched[i];
                    if (canvas == null)
                    {
                        continue;
                    }

                    canvas.renderMode = modes[i];
                    canvas.worldCamera = worldCameras[i];
                    canvas.planeDistance = distances[i];
                }

                Blit(_overlayLayer, target);
            }
        }

        private static void AddOverlayCanvas(
            Canvas canvas,
            List<Canvas> switched,
            ref int cullingMask,
            bool requireScene)
        {
            if (canvas == null)
            {
                return;
            }

            if (!canvas.isRootCanvas || !canvas.isActiveAndEnabled)
            {
                return;
            }

            if (canvas.renderMode != RenderMode.ScreenSpaceOverlay)
            {
                return;
            }

            if (requireScene && (canvas.gameObject == null || !canvas.gameObject.scene.IsValid()))
            {
                return;
            }

            switched.Add(canvas);
            cullingMask |= 1 << canvas.gameObject.layer;
            foreach (CanvasRenderer renderer in canvas.GetComponentsInChildren<CanvasRenderer>(true))
            {
                if (renderer != null && renderer.gameObject != null)
                {
                    cullingMask |= 1 << renderer.gameObject.layer;
                }
            }
        }

        private Camera EnsureOverlayCamera(int width, int height, int cullingMask)
        {
            if (_overlayCamera == null)
            {
                var host = new GameObject("playtest-bridge-capture-overlay-camera");
                host.hideFlags = HideFlags.HideAndDontSave;
                _overlayCamera = host.AddComponent<Camera>();
                _overlayCamera.enabled = false;
                _overlayCamera.orthographic = true;
                _overlayCamera.nearClipPlane = 0.01f;
                _overlayCamera.farClipPlane = 1000f;
                _overlayCamera.clearFlags = CameraClearFlags.SolidColor;
                _overlayCamera.backgroundColor = Color.clear;
                _overlayCamera.depth = 1000;
                _overlayCamera.allowHDR = false;
                _overlayCamera.allowMSAA = false;
                _overlayCamera.transform.position = new Vector3(0f, 0f, -OverlayPlaneDistance);
                _overlayCamera.transform.rotation = Quaternion.identity;
            }

            _overlayCamera.orthographicSize = Math.Max(0.5f, height / 2f);
            _overlayCamera.aspect = width / (float)Math.Max(1, height);
            _overlayCamera.cullingMask = cullingMask;
            return _overlayCamera;
        }

        private void Blit(RenderTexture source, RenderTexture destination)
        {
            Material blend = BlendMaterial();
            if (blend != null)
            {
                Graphics.Blit(source, destination, blend);
            }
            else
            {
                Graphics.Blit(source, destination);
            }
        }

        private Material BlendMaterial()
        {
            if (_blend != null)
            {
                return _blend;
            }

            Shader shader = Shader.Find("UI/Default");
            if (shader == null)
            {
                shader = Shader.Find("Sprites/Default");
            }

            if (shader == null)
            {
                return null;
            }

            _blend = new Material(shader)
            {
                hideFlags = HideFlags.HideAndDontSave,
            };
            return _blend;
        }

        private static void ClearTarget(RenderTexture target)
        {
            RenderTexture previous = RenderTexture.active;
            try
            {
                RenderTexture.active = target;
                GL.Clear(true, true, Color.black);
            }
            finally
            {
                RenderTexture.active = previous;
            }
        }

        private static void ReadTarget(RenderTexture target, Texture2D texture)
        {
            RenderTexture previous = RenderTexture.active;
            try
            {
                RenderTexture.active = target;
                texture.ReadPixels(new Rect(0, 0, target.width, target.height), 0, 0);
                texture.Apply();
            }
            finally
            {
                RenderTexture.active = previous;
            }
        }

        private int TargetHeight()
        {
            Camera camera = null;
            foreach (Camera candidate in ScreenCameras())
            {
                camera = candidate;
                break;
            }

            int sourceWidth = 0;
            int sourceHeight = 0;
            if (camera != null)
            {
                sourceWidth = camera.pixelWidth;
                sourceHeight = camera.pixelHeight;
            }

            if (sourceWidth <= 0 || sourceHeight <= 0)
            {
                sourceWidth = Screen.width;
                sourceHeight = Screen.height;
            }

            int height;
            if (sourceWidth > 0 && sourceHeight > 0)
            {
                height = Mathf.RoundToInt(_width * (float)sourceHeight / sourceWidth);
            }
            else
            {
                height = Mathf.RoundToInt(_width * 9f / 16f);
            }

            height = Mathf.Clamp(height, 2, 4096);
            if ((height & 1) != 0)
            {
                height++;
            }

            return height;
        }

        private void EnsureTargets(int width, int height)
        {
            if (_target != null && _target.width == width && _target.height == height)
            {
                return;
            }

            DestroyTargets();
            _target = CreateTarget(width, height, 24);
            _layer = CreateTarget(width, height, 24);
            _overlayLayer = CreateTarget(width, height, 24);
            _texture = new Texture2D(width, height, TextureFormat.RGBA32, false)
            {
                hideFlags = HideFlags.HideAndDontSave,
            };
            _texture.Apply();
        }

        private static RenderTexture CreateTarget(int width, int height, int depth)
        {
            var target = new RenderTexture(width, height, depth, RenderTextureFormat.ARGB32)
            {
                hideFlags = HideFlags.HideAndDontSave,
                filterMode = FilterMode.Bilinear,
                wrapMode = TextureWrapMode.Clamp,
            };
            target.Create();
            return target;
        }

        private void DestroyTargets()
        {
            DestroyTarget(ref _target);
            DestroyTarget(ref _layer);
            DestroyTarget(ref _overlayLayer);
            if (_texture != null)
            {
                UnityEngine.Object.DestroyImmediate(_texture);
                _texture = null;
            }
        }

        private static void DestroyTarget(ref RenderTexture target)
        {
            if (target == null)
            {
                return;
            }

            target.Release();
            UnityEngine.Object.DestroyImmediate(target);
            target = null;
        }

        private void ReleaseHelpers()
        {
            if (_overlayCamera != null)
            {
                GameObject host = _overlayCamera.gameObject;
                _overlayCamera = null;
                if (host != null)
                {
                    UnityEngine.Object.DestroyImmediate(host);
                }
            }

            if (_blend != null)
            {
                UnityEngine.Object.DestroyImmediate(_blend);
                _blend = null;
            }
        }

        private void Enqueue(byte[] jpeg)
        {
            lock (_queueGate)
            {
                if (_queue.Count >= PendingFrameLimit)
                {
                    Interlocked.Increment(ref _skipped);
                    return;
                }

                _queue.Enqueue(jpeg);
                Monitor.Pulse(_queueGate);
            }
        }

        private void WriteLoop()
        {
            while (true)
            {
                byte[] jpeg;
                lock (_queueGate)
                {
                    while (_queue.Count == 0 && _running)
                    {
                        Monitor.Wait(_queueGate, 100);
                    }

                    if (_queue.Count == 0)
                    {
                        if (!_running)
                        {
                            return;
                        }

                        continue;
                    }

                    jpeg = _queue.Dequeue();
                }

                WriteFrame(jpeg);
            }
        }

        private void WriteFrame(byte[] jpeg)
        {
            string name = "f-" + Interlocked.Increment(ref _counter).ToString("D6") + ".jpg";
            string path = Path.Combine(_directory, name);
            string temporary = path + ".tmp";
            try
            {
                File.WriteAllBytes(temporary, jpeg);
                if (File.Exists(path))
                {
                    File.Delete(path);
                }

                File.Move(temporary, path);
            }
            catch (Exception ex)
            {
                RecordError("writing a frame failed: " + ex.GetType().Name + ": " + ex.Message);
                TryDelete(temporary);
                return;
            }

            _latestRef = path;
            Interlocked.Increment(ref _written);

            string old = null;
            lock (_recent)
            {
                _recent.Enqueue(path);
                while (_recent.Count > _windowSize)
                {
                    old = _recent.Dequeue();
                }
            }

            if (old != null)
            {
                TryDelete(old);
            }
        }

        private void ClearDirectory()
        {
            if (!Directory.Exists(_directory))
            {
                return;
            }

            foreach (string file in Directory.GetFiles(_directory, "*.jpg"))
            {
                TryDelete(file);
            }

            foreach (string file in Directory.GetFiles(_directory, "*.tmp"))
            {
                TryDelete(file);
            }
        }

        private static void TryDelete(string path)
        {
            try
            {
                if (File.Exists(path))
                {
                    File.Delete(path);
                }
            }
            catch (Exception)
            {
            }
        }

        private static bool Near(Color actual, Color expected)
        {
            return Mathf.Abs(actual.r - expected.r) <= 0.16f
                && Mathf.Abs(actual.g - expected.g) <= 0.16f
                && Mathf.Abs(actual.b - expected.b) <= 0.16f;
        }

        private static string Describe(Color color)
        {
            return "(" + color.r.ToString("0.00") + ", " + color.g.ToString("0.00") + ", "
                + color.b.ToString("0.00") + ")";
        }

        private void RecordError(string message)
        {
            _lastError = message;
            if (_errorLogged)
            {
                return;
            }

            _errorLogged = true;
            try
            {
                Debug.LogWarning("playtest bridge capture: " + message);
            }
            catch (Exception)
            {
            }
        }

        private static double Now()
        {
#if UNITY_EDITOR
            return EditorApplication.timeSinceStartup;
#else
            return Time.realtimeSinceStartup;
#endif
        }
    }
}
