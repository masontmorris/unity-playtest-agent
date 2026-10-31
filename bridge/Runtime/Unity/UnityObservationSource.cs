// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.Reflection;
using System.Threading;
using Playtest.Bridge.Clock;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;
using UnityEngine;
using UnityEngine.SceneManagement;
#if UNITY_EDITOR
using UnityEditor;
#endif

namespace Playtest.Bridge.Unity
{
    /// <summary>
    /// Samples the generic Unity facts the core observes: active scene, play/pause, time scale,
    /// frame, game time, the UI tree and the active camera. Sampling runs on the Unity main
    /// thread; <see cref="GetObservation"/> only returns the latest published snapshot, so the
    /// transport may read it from its own thread.
    ///
    /// Every publish creates a new snapshot. Published snapshots are never mutated afterwards,
    /// so a reader holding an older instance keeps a consistent sample. The UI/camera walk is
    /// expensive and runs at most once per <see cref="UiRefreshIntervalSeconds"/>, and the probe
    /// at most once per <see cref="ProbeRefreshIntervalSeconds"/>; the other facts refresh on
    /// every tick.
    ///
    /// Missing facts stay missing: a reader that cannot produce a value records an
    /// <see cref="ObservationReaderError"/> instead of inventing one. Camera facts are published
    /// under <c>public.camera</c>; the UI tree is only <c>{"canvases": [...]}</c>. The probe is
    /// <c>public.probe</c> (see <see cref="UnityProbeField"/>), measured in the anchor's own frame
    /// and omitted, with a reader error, when there is no anchor. Where the game
    /// view points is the observation's own <c>view</c> (yaw and pitch of the camera the frame
    /// capture draws first, see <see cref="UnityGameView"/>), not a <c>public</c> entry, so what a
    /// decision-maker is shown under <c>public</c> does not change.
    /// </summary>
    public sealed class UnityObservationSource : IBridgeObservationSource, IDisposable
    {
        private const string ClockModeRealtime = "realtime";

        private const string UnknownScene = "unknown";

        private const string SceneReader = "scene";

        private const string CameraReader = "camera";

        private const string ViewReader = "view";

        private const string UiReader = "ui";

        private const string ProbeReader = "probe";

        private const string TextTypeMarker = "Text";

        private const string TextMemberName = "text";

        /// <summary>Upper bound on UI nodes in one sample, including canvas roots.</summary>
        private const int MaxUiNodes = 256;

        /// <summary>Deepest UI level; a canvas root is level 1.</summary>
        private const int MaxUiDepth = 8;

        /// <summary>Minimum spacing of the UI and camera walk, in unscaled seconds.</summary>
        private const double UiRefreshIntervalSeconds = 0.01;

        /// <summary>Minimum spacing of the probe, in unscaled seconds.</summary>
        private const double ProbeRefreshIntervalSeconds = 0.1;

        private static readonly Dictionary<Type, MemberInfo> TextMemberCache =
            new Dictionary<Type, MemberInfo>();

        private static readonly object TextMemberGate = new object();

        private readonly object _gate = new object();

        private readonly IBridgeClock _clock;

        private readonly IBridgeCapture _capture;

        private readonly UnityProbeField _probeField;

        private BridgeObservationSnapshot _current;

        private Dictionary<string, object> _ui;

        private Dictionary<string, object> _camera;

        private Dictionary<string, object> _probe;

        private ObservationView _view;

        private ObservationReaderError _uiError;

        private ObservationReaderError _cameraError;

        private ObservationReaderError _probeError;

        private ObservationReaderError _viewError;

        private double _lastUiRefresh;

        private double _lastProbeRefresh;

        private bool _started;

        private bool _disposed;

        public UnityObservationSource(
            IBridgeClock clock = null,
            IBridgeCapture capture = null,
            IProbeRaycasts probeRaycasts = null)
        {
            _clock = clock;
            _capture = capture;
            _probeField = new UnityProbeField(probeRaycasts);
            _ui = CreateEmptyUi();
            _lastUiRefresh = double.NegativeInfinity;
            _lastProbeRefresh = double.NegativeInfinity;
            _current = CreateUnsampledSnapshot(_ui);
        }

        /// <summary>True only when the capture service passed its startup self-test.</summary>
        public bool CaptureAvailable
        {
            get { return _capture != null && _capture.Available; }
        }

        /// <summary>
        /// Idempotent. Samples once synchronously and then keeps the cache fresh from the main
        /// thread: <c>EditorApplication.update</c> in the Editor, and the pre-render callback in
        /// other builds.
        /// </summary>
        public void Start()
        {
            lock (_gate)
            {
                if (_disposed)
                {
                    throw new ObjectDisposedException(nameof(UnityObservationSource));
                }

                if (_started)
                {
                    return;
                }

                _started = true;
            }

            try
            {
                Sample(true);
            }
            catch (Exception)
            {
                lock (_gate)
                {
                    _started = false;
                }

                throw;
            }

#if UNITY_EDITOR
            EditorApplication.update -= Tick;
            EditorApplication.update += Tick;
#else
            Application.onBeforeRender -= Tick;
            Application.onBeforeRender += Tick;
#endif
        }

        /// <summary>Idempotent. The last published snapshot stays readable after stopping.</summary>
        public void Stop()
        {
            lock (_gate)
            {
                if (!_started)
                {
                    return;
                }

                _started = false;
            }

#if UNITY_EDITOR
            EditorApplication.update -= Tick;
#else
            Application.onBeforeRender -= Tick;
#endif
        }

        /// <summary>
        /// The latest published snapshot. Safe from any thread: it reads one reference and never
        /// touches the Unity API. Before the first <see cref="Start"/>, the snapshot is empty and
        /// reports every baseline reader as not sampled.
        /// </summary>
        public BridgeObservationSnapshot GetObservation()
        {
            BridgeObservationSnapshot snapshot = Volatile.Read(ref _current);
            return snapshot ?? CreateUnsampledSnapshot(CreateEmptyUi());
        }

        public void Dispose()
        {
            lock (_gate)
            {
                if (_disposed)
                {
                    return;
                }

                _disposed = true;
            }

            Stop();
        }

        private void Tick()
        {
            try
            {
                if (!_started)
                {
                    return;
                }

                Sample(false);
            }
            catch (Exception)
            {
                // The Editor callback must never fault; the previous snapshot stays published.
            }
        }

        private void Sample(bool forceUiRefresh)
        {
            ObservationReaderError sceneError;
            string scene = ReadScene(out sceneError);
            bool playing = Application.isPlaying;
            bool paused = ReadPaused(playing);
            double timeScale = Time.timeScale;

            double now = ReadRealtimeSeconds();
            if (forceUiRefresh || now - _lastUiRefresh >= UiRefreshIntervalSeconds)
            {
                RefreshUiAndCamera();
                _lastUiRefresh = ReadRealtimeSeconds();
            }

            if (forceUiRefresh || now - _lastProbeRefresh >= ProbeRefreshIntervalSeconds)
            {
                RefreshProbe();
                _lastProbeRefresh = ReadRealtimeSeconds();
            }

            var publicFacts = new Dictionary<string, object>();
            if (_camera != null)
            {
                publicFacts["camera"] = _camera;
            }

            if (_probe != null)
            {
                publicFacts["probe"] = _probe;
            }

            var errors = new List<ObservationReaderError>(5);
            if (sceneError != null)
            {
                errors.Add(sceneError);
            }

            if (_uiError != null)
            {
                errors.Add(_uiError);
            }

            if (_cameraError != null)
            {
                errors.Add(_cameraError);
            }

            if (_probeError != null)
            {
                errors.Add(_probeError);
            }

            if (_viewError != null)
            {
                errors.Add(_viewError);
            }

            var snapshot = new BridgeObservationSnapshot
            {
                ClockMode = _clock == null ? ClockModeRealtime : _clock.ClockMode,
                Scene = scene,
                Playing = playing,
                Paused = paused,
                TimeScale = timeScale,
                Frame = Time.frameCount,
                GameS = playing ? Time.timeAsDouble : (double?)null,
                Public = publicFacts,
                Ui = _ui,
                FrameRef = _capture == null ? null : _capture.LatestFrameRef,
                ReaderErrors = errors,
                View = _view,
                CapturedWallUtc = DateTime.UtcNow,
            };

            Volatile.Write(ref _current, snapshot);
        }

        /// <summary>
        /// Rebuilds the UI tree, the camera facts and the game view. On failure the UI is empty and
        /// the camera and view are absent, each with a reader error; the caches are replaced together
        /// so a snapshot never mixes a fresh success with a stale failure.
        /// </summary>
        private void RefreshUiAndCamera()
        {
            Dictionary<string, object> ui = null;
            ObservationReaderError uiError = null;
            try
            {
                ui = BuildUi();
            }
            catch (Exception ex)
            {
                uiError = Describe(UiReader, ex);
            }

            if (ui == null)
            {
                ui = CreateEmptyUi();
                if (uiError == null)
                {
                    uiError = new ObservationReaderError
                    {
                        Reader = UiReader,
                        Error = "the UI reader returned no tree",
                    };
                }
            }

            Dictionary<string, object> camera = null;
            ObservationReaderError cameraError = null;
            try
            {
                camera = ReadCamera();
                if (camera == null)
                {
                    cameraError = new ObservationReaderError
                    {
                        Reader = CameraReader,
                        Error = "no active camera",
                    };
                }
            }
            catch (Exception ex)
            {
                cameraError = Describe(CameraReader, ex);
            }

            ObservationView view = null;
            ObservationReaderError viewError = null;
            try
            {
                string reason;
                if (!UnityGameView.TryRead(out view, out reason))
                {
                    view = null;
                    viewError = new ObservationReaderError
                    {
                        Reader = ViewReader,
                        Error = reason,
                    };
                }
            }
            catch (Exception ex)
            {
                view = null;
                viewError = Describe(ViewReader, ex);
            }

            _ui = ui;
            _camera = camera;
            _view = view;
            _uiError = uiError;
            _cameraError = cameraError;
            _viewError = viewError;
        }

        /// <summary>
        /// Rebuilds the probe. Without an anchor, or when sampling throws, the probe is absent from
        /// this tick and its error says why; it is never replaced by an empty or invented reading.
        /// </summary>
        private void RefreshProbe()
        {
            Dictionary<string, object> probe = null;
            ObservationReaderError probeError = null;
            try
            {
                string reason;
                if (!_probeField.TrySample(out probe, out reason))
                {
                    probe = null;
                    probeError = new ObservationReaderError
                    {
                        Reader = ProbeReader,
                        Error = reason,
                    };
                }
            }
            catch (Exception ex)
            {
                probe = null;
                probeError = Describe(ProbeReader, ex);
            }

            _probe = probe;
            _probeError = probeError;
        }

        private static string ReadScene(out ObservationReaderError error)
        {
            error = null;
            try
            {
                Scene scene = SceneManager.GetActiveScene();
                if (scene.IsValid() && !string.IsNullOrEmpty(scene.name))
                {
                    return scene.name;
                }

                error = new ObservationReaderError
                {
                    Reader = SceneReader,
                    Error = "the active scene is not valid",
                };
            }
            catch (Exception ex)
            {
                error = Describe(SceneReader, ex);
            }

            return UnknownScene;
        }

        private static bool ReadPaused(bool playing)
        {
#if UNITY_EDITOR
            try
            {
                if (EditorApplication.isPaused)
                {
                    return true;
                }
            }
            catch (Exception)
            {
                // Fall through to the time-scale rule instead of inventing a pause.
            }
#endif

            return playing && Time.timeScale == 0f;
        }

        private static double ReadRealtimeSeconds()
        {
            try
            {
                return Time.realtimeSinceStartupAsDouble;
            }
            catch (Exception)
            {
                // A real-time reading of 0 only makes the UI walk refresh more often.
                return 0;
            }
        }

        private static Dictionary<string, object> BuildUi()
        {
            var canvases = new List<object>();
            var budget = new NodeBudget(MaxUiNodes);
            Canvas[] found = UnityEngine.Object.FindObjectsByType<Canvas>(FindObjectsInactive.Include);

            for (int i = 0; i < found.Length; i++)
            {
                Canvas canvas = found[i];
                if (canvas == null || HasCanvasAncestor(canvas.transform))
                {
                    continue;
                }

                Dictionary<string, object> node = BuildCanvasNode(canvas, 1, budget);
                if (node == null)
                {
                    break;
                }

                canvases.Add(node);
            }

            return new Dictionary<string, object>
            {
                { "canvases", canvases },
            };
        }

        private static Dictionary<string, object> BuildCanvasNode(Canvas canvas, int depth, NodeBudget budget)
        {
            if (!budget.TryTake())
            {
                return null;
            }

            GameObject target = canvas.gameObject;
            var node = new Dictionary<string, object>
            {
                { "name", target.name },
                { "active", target.activeSelf },
                { "render_mode", canvas.renderMode.ToString() },
            };
            node["children"] = BuildChildren(canvas.transform, depth, budget);
            return node;
        }

        private static Dictionary<string, object> BuildChildNode(Transform transform, int depth, NodeBudget budget)
        {
            GameObject target = transform.gameObject;
            var node = new Dictionary<string, object>
            {
                { "name", target.name },
                { "active", target.activeSelf },
            };

            var rect = transform as RectTransform;
            if (rect != null)
            {
                Vector3 position = rect.position;
                node["position"] = new List<object> { position.x, position.y };
                Vector2 size = rect.rect.size;
                node["size"] = new List<object> { size.x, size.y };
            }
            else
            {
                Vector3 position = transform.position;
                node["position"] = new List<object> { position.x, position.y };
                node["size"] = null;
            }

            string text = ReadText(target);
            if (text != null)
            {
                node["text"] = text;
            }

            node["children"] = BuildChildren(transform, depth, budget);
            return node;
        }

        private static List<object> BuildChildren(Transform parent, int depth, NodeBudget budget)
        {
            var children = new List<object>();
            if (depth >= MaxUiDepth || parent == null)
            {
                return children;
            }

            int count = parent.childCount;
            for (int i = 0; i < count; i++)
            {
                Transform child = parent.GetChild(i);
                if (child == null)
                {
                    continue;
                }

                if (!budget.TryTake())
                {
                    break;
                }

                children.Add(BuildChildNode(child, depth + 1, budget));
            }

            return children;
        }

        private static bool HasCanvasAncestor(Transform transform)
        {
            if (transform == null)
            {
                return false;
            }

            Transform parent = transform.parent;
            while (parent != null)
            {
                if (parent.GetComponent<Canvas>() != null)
                {
                    return true;
                }

                parent = parent.parent;
            }

            return false;
        }

        /// <summary>
        /// Reads a string text member by reflection, without referencing a UI package. The
        /// component type name must contain "Text" and expose a public instance string property
        /// or field named "text"; hidden or malformed members are skipped, never guessed.
        /// </summary>
        private static string ReadText(GameObject target)
        {
            Component[] components;
            try
            {
                components = target.GetComponents<Component>();
            }
            catch (Exception)
            {
                return null;
            }

            for (int i = 0; i < components.Length; i++)
            {
                Component component = components[i];
                if (component == null)
                {
                    continue;
                }

                Type type = component.GetType();
                if (type.Name.IndexOf(TextTypeMarker, StringComparison.Ordinal) < 0)
                {
                    continue;
                }

                try
                {
                    string value = ReadTextMember(type, component);
                    if (value != null)
                    {
                        return value;
                    }
                }
                catch (Exception)
                {
                    // A destroyed or failing component has no readable text.
                }
            }

            return null;
        }

        private static string ReadTextMember(Type type, Component component)
        {
            MemberInfo member;
            lock (TextMemberGate)
            {
                if (!TextMemberCache.TryGetValue(type, out member))
                {
                    member = FindTextMember(type);
                    TextMemberCache[type] = member;
                }
            }

            if (member is PropertyInfo property)
            {
                return property.GetValue(component, null) as string;
            }

            if (member is FieldInfo field)
            {
                return field.GetValue(component) as string;
            }

            return null;
        }

        private static MemberInfo FindTextMember(Type type)
        {
            try
            {
                PropertyInfo[] properties = type.GetProperties(BindingFlags.Public | BindingFlags.Instance);
                for (int i = 0; i < properties.Length; i++)
                {
                    PropertyInfo property = properties[i];
                    if (property.PropertyType != typeof(string)
                        || !property.CanRead
                        || property.GetIndexParameters().Length != 0
                        || !string.Equals(property.Name, TextMemberName, StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }

                    return property;
                }

                FieldInfo[] fields = type.GetFields(BindingFlags.Public | BindingFlags.Instance);
                for (int i = 0; i < fields.Length; i++)
                {
                    FieldInfo field = fields[i];
                    if (field.FieldType != typeof(string)
                        || !string.Equals(field.Name, TextMemberName, StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }

                    return field;
                }
            }
            catch (Exception)
            {
                // Unreadable metadata means this component cannot supply text.
            }

            return null;
        }

        private static Dictionary<string, object> ReadCamera()
        {
            Camera camera = Camera.main;
            if (camera == null)
            {
                Camera[] cameras = Camera.allCameras;
                for (int i = 0; i < cameras.Length; i++)
                {
                    Camera candidate = cameras[i];
                    if (candidate != null && candidate.enabled)
                    {
                        camera = candidate;
                        break;
                    }
                }
            }

            if (camera == null)
            {
                return null;
            }

            Transform transform = camera.transform;
            Vector3 position = transform.position;
            Vector3 rotation = transform.eulerAngles;
            return new Dictionary<string, object>
            {
                { "name", camera.gameObject.name },
                { "position", new List<object> { position.x, position.y, position.z } },
                { "rotation", new List<object> { rotation.x, rotation.y, rotation.z } },
                { "field_of_view", camera.fieldOfView },
                { "orthographic", camera.orthographic },
            };
        }

        private static Dictionary<string, object> CreateEmptyUi()
        {
            return new Dictionary<string, object>
            {
                { "canvases", new List<object>() },
            };
        }

        private static BridgeObservationSnapshot CreateUnsampledSnapshot(Dictionary<string, object> ui)
        {
            return new BridgeObservationSnapshot
            {
                ClockMode = ClockModeRealtime,
                Scene = UnknownScene,
                Playing = false,
                Paused = false,
                TimeScale = 1,
                Frame = 0,
                GameS = null,
                Public = new Dictionary<string, object>(),
                Ui = ui,
                ReaderErrors = new List<ObservationReaderError>
                {
                    new ObservationReaderError { Reader = SceneReader, Error = "not sampled" },
                    new ObservationReaderError { Reader = UiReader, Error = "not sampled" },
                    new ObservationReaderError { Reader = CameraReader, Error = "not sampled" },
                    new ObservationReaderError { Reader = ProbeReader, Error = "not sampled" },
                    new ObservationReaderError { Reader = ViewReader, Error = "not sampled" },
                },
                View = null,
                CapturedWallUtc = DateTime.UtcNow,
            };
        }

        private static ObservationReaderError Describe(string reader, Exception exception)
        {
            string message = exception == null ? "unknown failure" : exception.GetType().Name;
            if (exception != null && !string.IsNullOrEmpty(exception.Message))
            {
                message = message + ": " + exception.Message;
            }

            return new ObservationReaderError
            {
                Reader = reader,
                Error = message,
            };
        }

        private sealed class NodeBudget
        {
            private int _remaining;

            public NodeBudget(int remaining)
            {
                _remaining = remaining;
            }

            public bool TryTake()
            {
                if (_remaining <= 0)
                {
                    return false;
                }

                _remaining--;
                return true;
            }
        }
    }
}
