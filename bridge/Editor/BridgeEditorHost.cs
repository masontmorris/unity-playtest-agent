// SPDX-License-Identifier: Apache-2.0
using System;
using System.IO;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Clock;
using Playtest.Bridge.Editor.Preflight;
using Playtest.Bridge.Lock;
using Playtest.Bridge.Transport;
using Playtest.Bridge.Unity;
using UnityEditor;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Playtest.Bridge.Editor
{
    /// <summary>
    /// Owns the Editor-side bridge lifetime: one server and one project lock per Editor process.
    /// Every member runs on the Unity main thread; the server's transport threads never read these
    /// fields except through the cached <see cref="UnityStatusSource"/>.
    /// </summary>
    [InitializeOnLoad]
    public static class BridgeEditorHost
    {
        public const string LogPrefix = "[Playtest Bridge]";

        private const string LockFileName = "playtest-bridge.lock";

        private static BridgeServer _server;
        private static UnityObservationSource _observations;
        private static InputSystemInputBackend _input;
        private static UnityClockController _clock;
        private static UnityCapture _capture;
        private static EditorInspectionSource _inspection;
        private static BridgeLockFile _lock;
        private static string _lastError;
        private static int _lockOwnerPid;

        static BridgeEditorHost()
        {
            AssemblyReloadEvents.beforeAssemblyReload += Stop;
            EditorApplication.quitting += Stop;
            SceneManager.activeSceneChanged += OnActiveSceneChanged;

            if (!Application.isBatchMode)
            {
                EditorApplication.delayCall += AutoStart;
            }
        }

        private static void OnActiveSceneChanged(Scene previous, Scene next)
        {
            BridgeServer server = _server;
            if (server != null)
            {
                server.StopActiveSession("scene_change");
            }
        }

        public static bool IsRunning
        {
            get
            {
                BridgeServer server = _server;
                return server != null && server.IsRunning;
            }
        }

        /// <summary>Loopback port while running; 0 when stopped.</summary>
        public static int Port
        {
            get
            {
                BridgeServer server = _server;
                return server != null && server.IsRunning ? server.Port : 0;
            }
        }

        /// <summary>Null when the last lifecycle action succeeded.</summary>
        public static string LastError
        {
            get { return _lastError; }
        }

        /// <summary>
        /// PID recorded by the project lock: ours while running, the conflicting owner after a busy
        /// start, and 0 when no lock is known.
        /// </summary>
        public static int LockOwnerPid
        {
            get { return _lockOwnerPid; }
        }

        /// <summary><c>&lt;project&gt;/Temp/playtest-bridge.lock</c>.</summary>
        public static string LockPath
        {
            get { return Path.GetFullPath(Path.Combine(ProjectRoot(), "Temp", LockFileName)); }
        }

        private static string ProjectRoot()
        {
            string dataPath = Application.dataPath;
            if (!string.IsNullOrEmpty(dataPath))
            {
                dataPath = dataPath.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                string parent = Path.GetDirectoryName(dataPath);
                if (!string.IsNullOrEmpty(parent))
                {
                    return parent;
                }
            }

            return Environment.CurrentDirectory;
        }

        /// <summary>Idempotent. Never throws: a failure is reported through <see cref="LastError"/>.</summary>
        public static void Start()
        {
            StartCore(null);
        }

        /// <summary>
        /// Start a new preflight session with an explicit, temporary action catalog. A running
        /// server is never silently reused with a different catalog.
        /// </summary>
        public static void StartForPreflight(BridgeActionCatalog actions)
        {
            if (actions == null) throw new ArgumentNullException(nameof(actions));
            if (IsRunning) throw new InvalidOperationException("the bridge is already running");
            StartCore(actions);
        }

        private static void StartCore(BridgeActionCatalog preflightActions)
        {
            if (IsRunning)
            {
                return;
            }

            Stop();

            string token = null;
            BridgeServer server = null;
            UnityObservationSource observations = null;
            InputSystemInputBackend input = null;
            UnityClockController clock = null;
            UnityCapture capture = null;
            EditorInspectionSource inspection = null;
            BridgeLockFile acquired = null;
            int ownerPid = 0;
            try
            {
                token = BridgeToken.Create();
                ownerPid = CurrentProcessId();
                string projectRoot = Path.GetDirectoryName(Application.dataPath);
                var status = new UnityStatusSource(ownerPid);
                capture = new UnityCapture(Path.Combine(projectRoot, "Temp", "playtest-bridge-frames"));
                status.Capture = capture.Available;
                status.CaptureSelfTest = capture.SelfTest;
                clock = new UnityClockController();
                clock.Start();
                observations = new UnityObservationSource(clock, capture);
                observations.Start();
                capture.Start();
                inspection = new EditorInspectionSource();

                string actionsPath = Path.Combine(projectRoot, "Temp", "playtest-bridge-actions.json");
                BridgeActionCatalog actions = preflightActions;
                try
                {
                    if (actions == null) actions = BridgeActionCatalog.Load(actionsPath);
                }
                catch (Exception ex)
                {
                    actions = BridgeActionCatalog.Empty;
                    Debug.LogWarning(LogPrefix + " action catalog could not be loaded: " + ex.GetType().Name + ": " + ex.Message);
                }

                input = new InputSystemInputBackend();
                if (!input.IsAvailable)
                {
                    input.Dispose();
                    input = null;
                }

                server = new BridgeServer(
                    new BridgeServerOptions
                    {
                        Port = 0,
                        HandshakeTimeoutMs = 5000,
                        ReadTimeoutMs = 0,
                    },
                    status,
                    observations,
                    actions,
                    input,
                    clock,
                    inspection,
                    token);
                server.Log = message => Debug.Log(LogPrefix + " " + Redact(message, token));

                server.Start();
                status.Port = server.Port;

                string directory = Path.GetDirectoryName(LockPath);
                if (!string.IsNullOrEmpty(directory))
                {
                    Directory.CreateDirectory(directory);
                }

                acquired = BridgeLockFile.Acquire(LockPath, ownerPid, server.Port, token);
            }
            catch (BridgeLockBusyException busy)
            {
                string owner;
                int busyOwnerPid = DescribeBusyOwner(busy, out owner);
                StopInspection(inspection);
                StopServer(server);
                StopObservations(observations);
                StopInput(input);
                StopClock(clock);
                StopCapture(capture);
                _lockOwnerPid = busyOwnerPid;
                SetError("the project lock is already held by " + owner, token);
                return;
            }
            catch (Exception ex)
            {
                StopInspection(inspection);
                StopServer(server);
                StopObservations(observations);
                StopInput(input);
                StopClock(clock);
                StopCapture(capture);
                SetError("could not start: " + ex.GetType().Name + ": " + ex.Message, token);
                return;
            }

            _server = server;
            _observations = observations;
            _input = input;
            _clock = clock;
            _capture = capture;
            _inspection = inspection;
            _lock = acquired;
            _lockOwnerPid = ownerPid;
            _lastError = null;
            Log("listening on 127.0.0.1:" + server.Port + "; lock " + LockPath);
        }

        /// <summary>Idempotent. Stops the server and releases the lock if either is held.</summary>
        public static void Stop()
        {
            BridgeServer server = _server;
            UnityObservationSource observations = _observations;
            InputSystemInputBackend input = _input;
            UnityClockController clock = _clock;
            UnityCapture capture = _capture;
            EditorInspectionSource inspection = _inspection;
            BridgeLockFile held = _lock;
            bool wasActive = server != null || held != null;

            _server = null;
            _observations = null;
            _input = null;
            _clock = null;
            _capture = null;
            _inspection = null;
            _lock = null;
            _lockOwnerPid = 0;
            _lastError = null;

            StopInspection(inspection);
            StopServer(server);
            StopObservations(observations);
            StopInput(input);
            StopClock(clock);
            StopCapture(capture);

            if (held != null)
            {
                try
                {
                    held.Release();
                }
                catch (Exception ex)
                {
                    _lastError = "releasing the project lock failed: " + ex.GetType().Name;
                    Debug.LogWarning(LogPrefix + " " + _lastError);
                }
            }

            if (wasActive)
            {
                Log("stopped");
            }
        }

        private static void AutoStart()
        {
            EditorApplication.delayCall -= AutoStart;
            if (Application.isBatchMode || IsRunning)
            {
                return;
            }

            Start();
        }

        private static void StopServer(BridgeServer server)
        {
            if (server == null)
            {
                return;
            }

            try
            {
                server.Stop();
            }
            catch (Exception ex)
            {
                _lastError = "stopping the server failed: " + ex.GetType().Name;
                Debug.LogWarning(LogPrefix + " " + _lastError);
            }
        }

        private static void StopInspection(EditorInspectionSource inspection)
        {
            if (inspection == null) return;
            try { inspection.Dispose(); }
            catch (Exception ex)
            {
                Debug.LogWarning(LogPrefix + " stopping inspection failed: " + ex.GetType().Name);
            }
        }

        private static void StopObservations(UnityObservationSource observations)
        {
            if (observations == null)
            {
                return;
            }

            try
            {
                observations.Stop();
                observations.Dispose();
            }
            catch (Exception ex)
            {
                Debug.LogWarning(LogPrefix + " stopping observation sampling failed: " + ex.GetType().Name);
            }
        }

        private static void StopInput(InputSystemInputBackend input)
        {
            if (input == null)
            {
                return;
            }

            try
            {
                input.Dispose();
            }
            catch (Exception ex)
            {
                Debug.LogWarning(LogPrefix + " stopping the input backend failed: " + ex.GetType().Name);
            }
        }

        private static void StopClock(UnityClockController clock)
        {
            if (clock == null)
            {
                return;
            }

            try
            {
                clock.Dispose();
            }
            catch (Exception ex)
            {
                Debug.LogWarning(LogPrefix + " stopping clock control failed: " + ex.GetType().Name);
            }
        }

        private static void StopCapture(UnityCapture capture)
        {
            if (capture == null)
            {
                return;
            }

            try
            {
                capture.Dispose();
            }
            catch (Exception ex)
            {
                Debug.LogWarning(LogPrefix + " stopping capture failed: " + ex.GetType().Name);
            }
        }

        private static int DescribeBusyOwner(BridgeLockBusyException busy, out string description)
        {
            description = "another process";
            try
            {
                var owner = busy.Owner;
                if (owner == null)
                {
                    return 0;
                }

                description = "pid " + owner.Pid + " on port " + owner.Port;
                return owner.Pid;
            }
            catch (Exception)
            {
                return 0;
            }
        }

        private static void SetError(string message, string token)
        {
            _lastError = Redact(message, token);
            Debug.LogWarning(LogPrefix + " " + _lastError);
        }

        private static string Redact(string message, string token)
        {
            if (string.IsNullOrEmpty(message))
            {
                return message ?? string.Empty;
            }

            if (string.IsNullOrEmpty(token))
            {
                return message;
            }

            return message.Replace(token, "<redacted>");
        }

        private static void Log(string message)
        {
            Debug.Log(LogPrefix + " " + message);
        }

        /// <summary>
        /// Environment.ProcessId is outside the .NET Standard 2.1 profile, so use the equivalent
        /// that compiles for every Unity 6 scripting backend.
        /// </summary>
        private static int CurrentProcessId()
        {
            using (System.Diagnostics.Process process = System.Diagnostics.Process.GetCurrentProcess())
            {
                return process.Id;
            }
        }
    }
}
