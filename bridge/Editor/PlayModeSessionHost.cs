// SPDX-License-Identifier: Apache-2.0
using System;
using System.IO;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Editor.Session;
using Playtest.Bridge.Transport;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Playtest.Bridge.Editor
{
    /// <summary>
    /// The Editor-owned live session host. The wrapper launches Unity with
    /// <c>-executeMethod Playtest.Bridge.Editor.PlayModeSessionHost.Start</c> and this class owns Play
    /// Mode from then on: the initial fresh scene instance, the pinned manifest catalog, the bridge and
    /// the generation-scoped capture. It is not a PlayMode test coroutine, so ending a play instance
    /// never kills the client process and the client never owns the Editor's lifetime.
    ///
    /// An S1-selected restart arrives as <c>request.json</c> in the control directory. The host executes
    /// that one ticket: release the bridge and its lock, leave Play Mode, observe
    /// <c>EnteredEditMode</c>, re-enter, observe <c>EnteredPlayMode</c>, load a fresh build scene 0,
    /// validate the catalog, mint one new generation, start a new bridge with a new token and lock, and
    /// only then publish <c>ready</c>. It never invents a request, never retries a failed operation and
    /// never reports readiness it did not observe. The decision itself belongs to S1; this host only
    /// executes it.
    /// </summary>
    [InitializeOnLoad]
    public static class PlayModeSessionHost
    {
        public const string LogPrefix = "[Playtest Session]";

        /// <summary>The control directory variable read by both the host and the Python client.</summary>
        public const string ControlDirectoryVariable = SessionControlDirectory.VariableName;

        /// <summary>The operator-facing entry point the wrapper passes to <c>-executeMethod</c>.</summary>
        public const string EntryPoint = "Playtest.Bridge.Editor.PlayModeSessionHost.Start";

        private const string SessionStateKey = "Playtest.Bridge.PlayModeSessionHost.State";
        private const string BegunKey = "Playtest.Bridge.PlayModeSessionHost.Begun";
        private const double MailboxPollSeconds = 0.25;
        private const double SceneLoadSeconds = 120;
        private const double EditModeWaitSeconds = 60;
        private const double ExitWaitSeconds = 60;

        private static SessionControlDirectory _control;
        private static SessionConfig _config;
        private static PlayModeSessionMachine _machine;
        private static BridgeActionCatalog _catalog;
        private static int _publishedRevision = -1;

        private static bool _begun;
        private static bool _editModePrepared;
        private static bool _restartPrepared;
        private static string _scenePath;
        private static AsyncOperation _sceneLoad;
        private static double _sceneLoadDeadline;
        private static double _nextMailboxPoll;
        private static DateTime? _playModeExitRequestedUtc;
        private static bool _exitRequested;
        private static bool _exitFailed;
        private static DateTime? _exitRequestedUtc;
        private static bool _exited;
        private static string _lastRequestError;
        private static string _lastCancelError;
        private static string _lastRejectedTicketId;
        private static string _lastRejectedReason;
        private static string _lastIgnoredTicketId;
        private static string _lastIgnoredReason;

        static PlayModeSessionHost()
        {
            if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable(ControlDirectoryVariable)))
            {
                // An ordinary Editor, a bench run or a test run is not a supervised session: stay inert.
                return;
            }

            EditorApplication.update -= Tick;
            EditorApplication.update += Tick;
            EditorApplication.playModeStateChanged -= OnPlayModeStateChanged;
            EditorApplication.playModeStateChanged += OnPlayModeStateChanged;
            AssemblyReloadEvents.beforeAssemblyReload -= Persist;
            AssemblyReloadEvents.beforeAssemblyReload += Persist;
            EditorApplication.quitting -= OnQuitting;
            EditorApplication.quitting += OnQuitting;
        }

        /// <summary>
        /// Starts the supervised session. Called once per Unity process by <c>-executeMethod</c>; a
        /// domain reload resumes through InitializeOnLoad and SessionState instead.
        /// </summary>
        public static void Start()
        {
            if (_begun || SessionState.GetBool(BegunKey, false))
            {
                return;
            }

            SessionControlDirectory control;
            SessionConfig config;
            try
            {
                control = SessionControlDirectory.FromEnvironment();
                config = SessionConfig.Parse(control.ReadConfigText());
            }
            catch (Exception ex)
            {
                // Nothing can be published without a control directory, so the launcher's only
                // evidence is this process exiting: fail fast rather than sit idle until its timeout.
                Debug.LogError(
                    LogPrefix + " cannot read the session control directory and its config.json: "
                    + ex.GetType().Name + ": " + ex.Message);
                EditorApplication.Exit(1);
                return;
            }

            string projectRoot = ProjectRoot();
            if (!string.Equals(
                    Path.GetFullPath(projectRoot).TrimEnd(Path.DirectorySeparatorChar),
                    Path.GetFullPath(config.ProjectPath).TrimEnd(Path.DirectorySeparatorChar),
                    StringComparison.OrdinalIgnoreCase))
            {
                Debug.LogError(
                    LogPrefix + " config.json.project_path is '" + config.ProjectPath
                    + "', but Unity opened '" + projectRoot + "'");
                EditorApplication.Exit(1);
                return;
            }

            _control = control;
            _config = config;
            _machine = new PlayModeSessionMachine(config);
            _begun = true;
            SessionState.SetBool(BegunKey, true);
            Persist();
            PublishIfChanged();
            Log("host started for run '" + config.RunId + "' in " + control.Root
                + "; deadline " + SessionRecordJson.Timestamp(config.DeadlineUtc));
        }

        private static void Tick()
        {
            if (_exited)
            {
                return;
            }

            try
            {
                if (_machine == null)
                {
                    if (!SessionState.GetBool(BegunKey, false))
                    {
                        return;
                    }

                    Resume();
                    if (_machine == null)
                    {
                        return;
                    }
                }

                DateTime now = DateTime.UtcNow;
                PollCancel(now);

                if (_control.ShutdownRequested)
                {
                    BeginShutdown(now);
                }
                else if (!_exitRequested)
                {
                    _machine.ExpireIfNeeded(now);
                }

                PublishIfChanged();

                if (_exitRequested)
                {
                    StepExit(now);
                    return;
                }

                switch (_machine.HostPhase)
                {
                    case PlayModeSessionPhase.Starting:
                        if (_machine.Ticket == null || _machine.Ticket.IsTerminal)
                        {
                            StepInitialStartup(now);
                        }
                        else
                        {
                            StepRestart(now);
                        }

                        break;
                    case PlayModeSessionPhase.Ready:
                        PollRequest(now);
                        break;
                }

                PublishIfChanged();
            }
            catch (Exception ex)
            {
                FailSession("host_exception: " + ex.GetType().Name + ": " + ex.Message, DateTime.UtcNow);
            }
        }

        /// <summary>A domain reload during a healthy session dropped the bridge; that is fatal, never resumed.</summary>
        private static void Resume()
        {
            try
            {
                SessionControlDirectory control = SessionControlDirectory.FromEnvironment();
                SessionConfig config = SessionConfig.Parse(control.ReadConfigText());
                string saved = SessionState.GetString(SessionStateKey, string.Empty);
                if (string.IsNullOrWhiteSpace(saved))
                {
                    throw new SessionRecordException("SessionState has no saved host state");
                }

                _control = control;
                _config = config;
                _machine = SessionPersistence.Restore(saved, config, control.Root);
                if (_machine.HostPhase == PlayModeSessionPhase.Ready)
                {
                    // The bridge, its token and its lock lived in the reloaded domain, so the client's
                    // transport is already gone. That is an accidental disconnect: fatal, never resumed.
                    throw new SessionRecordException(
                        "the bridge did not survive the domain reload; an unexpected disconnect is fatal");
                }

                _publishedRevision = -1;
                Log("host resumed after a domain reload in the '" + HostStateRecord.WireName(_machine.HostPhase) + "' phase");
                Persist();
                PublishIfChanged();
            }
            catch (Exception ex)
            {
                string reason = "session_state_lost: " + ex.GetType().Name + ": " + ex.Message;
                Log(reason);
                try
                {
                    if (_control != null && _config != null)
                    {
                        _machine = new PlayModeSessionMachine(_config);
                        _machine.Fail(reason, DateTime.UtcNow);
                        PublishIfChanged();
                    }
                }
                catch (Exception)
                {
                }

                _exitRequested = true;
                _exitFailed = true;
                _exitRequestedUtc = DateTime.UtcNow;
                BridgeEditorHost.Stop();
                if (EditorApplication.isPlayingOrWillChangePlaymode)
                {
                    EditorApplication.ExitPlaymode();
                }
                else
                {
                    EditorApplication.Exit(1);
                }
            }
        }

        private static void StepInitialStartup(DateTime now)
        {
            if (_machine.Generation == null
                && !EditorApplication.isPlaying
                && !EditorApplication.isPlayingOrWillChangePlaymode)
            {
                if (!_editModePrepared)
                {
                    PrepareEditMode();
                    _editModePrepared = true;
                }

                Persist();
                _sceneLoad = null;
                Log("entering Play Mode for the initial instance");
                EditorApplication.EnterPlaymode();
                return;
            }

            if (!EditorApplication.isPlaying)
            {
                return;
            }

            if (!EnsureFreshSceneLoaded(now))
            {
                return;
            }

            Scene scene = RequireFreshScene();
            SceneManager.SetActiveScene(scene);
            if (!_machine.MarkInitialSceneLoaded(_scenePath, RawSceneHandle(scene)))
            {
                throw new InvalidOperationException(
                    "the host refused the initial scene: " + _machine.LastRejectedTransition);
            }

            BridgeActionCatalog catalog = LoadCatalog();
            string generation = _machine.MintGenerationForPlayInstance();
            Persist(); // the generation is on record before the bridge that serves it starts

            BridgeEditorHost.StartForPreflight(catalog, generation);
            if (!BridgeEditorHost.IsRunning)
            {
                throw new InvalidOperationException("the bridge did not start: " + BridgeEditorHost.LastError);
            }

            if (!_machine.MarkInitialReady(DateTime.UtcNow))
            {
                throw new InvalidOperationException(
                    "the host refused readiness: " + _machine.LastRejectedTransition);
            }

            Persist();
            Log("initial instance ready: generation " + generation + ", scene " + _scenePath);
        }

        private static void StepRestart(DateTime now)
        {
            RestartTicket ticket = _machine.Ticket;
            if (ticket == null)
            {
                return;
            }

            switch (ticket.Phase)
            {
                case RestartPhase.Accepted:
                    // Release the old transport first: its lock, token, inputs and capture belong to the
                    // play instance that is about to end and are never carried into the new one.
                    BridgeEditorHost.Stop();
                    if (!_machine.MarkBridgeStopped(DateTime.UtcNow))
                    {
                        return;
                    }

                    Persist();
                    PublishIfChanged();
                    _playModeExitRequestedUtc = DateTime.UtcNow;
                    Persist();
                    Log("leaving Play Mode for restart ticket '" + ticket.TicketId + "'");
                    EditorApplication.ExitPlaymode();
                    return;

                case RestartPhase.Stopped:
                    if (!EditorApplication.isPlayingOrWillChangePlaymode
                        && _playModeExitRequestedUtc.HasValue
                        && (DateTime.UtcNow - _playModeExitRequestedUtc.Value).TotalSeconds > EditModeWaitSeconds)
                    {
                        throw new TimeoutException("the editor never reported EnteredEditMode for this ticket");
                    }

                    return;

                case RestartPhase.Starting:
                    if (ticket.EnteredEditModeUtc != null && ticket.EnteredPlayModeUtc == null)
                    {
                        if (EditorApplication.isPlayingOrWillChangePlaymode)
                        {
                            return;
                        }

                        if (!_restartPrepared)
                        {
                            PrepareEditMode();
                            _restartPrepared = true;
                        }

                        Persist();
                        _sceneLoad = null;
                        Log("re-entering Play Mode for restart ticket '" + ticket.TicketId + "'");
                        EditorApplication.EnterPlaymode();
                        return;
                    }

                    if (ticket.EnteredPlayModeUtc == null)
                    {
                        return;
                    }

                    StepRestartPlayInstance(now, ticket);
                    return;
            }
        }

        private static void StepRestartPlayInstance(DateTime now, RestartTicket ticket)
        {
            if (!EnsureFreshSceneLoaded(now))
            {
                return;
            }

            Scene scene = RequireFreshScene();
            SceneManager.SetActiveScene(scene);
            if (!_machine.MarkSceneLoaded(_scenePath, RawSceneHandle(scene)))
            {
                throw new InvalidOperationException(
                    "the host refused the fresh scene: " + _machine.LastRejectedTransition);
            }

            BridgeActionCatalog catalog = LoadCatalog();
            string generation = _machine.MintGenerationForPlayInstance();
            Persist(); // one generation per play instance, on record before its bridge starts

            BridgeEditorHost.StartForPreflight(catalog, generation);
            if (!BridgeEditorHost.IsRunning)
            {
                throw new InvalidOperationException("the bridge did not start: " + BridgeEditorHost.LastError);
            }

            if (!_machine.MarkBridgeStarted(generation, DateTime.UtcNow))
            {
                throw new InvalidOperationException(
                    "the host refused readiness: " + _machine.LastRejectedTransition);
            }

            _restartPrepared = false;
            Persist();
            Log("restart ticket '" + ticket.TicketId + "' ready: generation " + generation
                + ", scene " + _scenePath);
        }

        private static bool EnsureFreshSceneLoaded(DateTime now)
        {
            if (_sceneLoad == null)
            {
                _scenePath = string.IsNullOrEmpty(_scenePath) ? BuildScenePath() : _scenePath;
                // A fresh instance even when the project disables scene reload on entering Play Mode:
                // this explicit load replaces whatever the Editor transitioned with.
                _sceneLoad = SceneManager.LoadSceneAsync(_scenePath, LoadSceneMode.Single);
                if (_sceneLoad == null)
                {
                    throw new InvalidOperationException("the fresh scene load could not start: " + _scenePath);
                }

                _sceneLoadDeadline = EditorApplication.timeSinceStartup + SceneLoadSeconds;
                return false;
            }

            if (!_sceneLoad.isDone)
            {
                if (EditorApplication.timeSinceStartup > _sceneLoadDeadline)
                {
                    throw new TimeoutException("the fresh scene did not load in time: " + _scenePath);
                }

                return false;
            }

            _sceneLoad = null;
            return true;
        }

        private static int RawSceneHandle(Scene scene)
        {
#if UNITY_6000_5_OR_NEWER
            return scene.handle.GetRawData();
#else
            return scene.handle;
#endif
        }

        private static Scene RequireFreshScene()
        {
            Scene scene = SceneManager.GetSceneByPath(_scenePath);
            if (!scene.IsValid() || !scene.isLoaded)
            {
                throw new InvalidOperationException("the build scene 0 is not loaded after the fresh load: " + _scenePath);
            }

            return scene;
        }

        /// <summary>Loads build scene 0 in edit mode, makes it active and validates the pinned catalog.</summary>
        private static void PrepareEditMode()
        {
            string scenePath = BuildScenePath();
            Scene scene = EditorSceneManager.OpenScene(scenePath, OpenSceneMode.Single);
            if (!scene.IsValid() || !scene.isLoaded)
            {
                throw new InvalidOperationException("build scene 0 did not load in edit mode: " + scenePath);
            }

            SceneManager.SetActiveScene(scene);
            _scenePath = scenePath;
            _catalog = null;
            LoadCatalog(); // the pinned manifest catalog must verify before any play instance starts
            Log("edit-mode preparation loaded " + scenePath);
        }

        private static string BuildScenePath()
        {
            string scenePath = SceneUtility.GetScenePathByBuildIndex(0);
            if (string.IsNullOrWhiteSpace(scenePath))
            {
                throw new InvalidOperationException(
                    "the build settings have no enabled scene at index 0, so there is no scene to load");
            }

            return scenePath;
        }

        private static BridgeActionCatalog LoadCatalog()
        {
            if (_catalog != null)
            {
                return _catalog;
            }

            // Strict: absent file, altered digest, invalid or empty catalog all throw. There is no fallback.
            _catalog = BridgeSessionCatalog.LoadExported(_config.CatalogPath, _config.CatalogSha256);
            return _catalog;
        }

        private static void PollRequest(DateTime now)
        {
            if (EditorApplication.timeSinceStartup < _nextMailboxPoll)
            {
                return;
            }

            _nextMailboxPoll = EditorApplication.timeSinceStartup + MailboxPollSeconds;

            string json;
            if (!_control.TryReadText(_control.RequestPath, out json))
            {
                return;
            }

            RestartRequestRecord request;
            string error;
            if (!RestartRequestRecord.TryParse(json, out request, out error))
            {
                if (!string.Equals(error, _lastRequestError, StringComparison.Ordinal))
                {
                    _lastRequestError = error;
                    Log("ignoring an invalid request.json: " + error);
                }

                return;
            }

            _lastRequestError = null;
            string reason;
            RestartAcceptance outcome = _machine.TryAccept(request, now, out reason);
            switch (outcome)
            {
                case RestartAcceptance.Accepted:
                    _restartPrepared = false;
                    _playModeExitRequestedUtc = null;
                    _lastIgnoredTicketId = null;
                    _lastIgnoredReason = null;
                    Persist();
                    PublishIfChanged();
                    Log("accepted restart ticket '" + request.TicketId + "' from generation "
                        + request.FromGeneration);
                    break;
                case RestartAcceptance.Duplicate:
                case RestartAcceptance.Busy:
                case RestartAcceptance.ForeignRun:
                    // Never overwrite an in-flight or completed result, and never act on another run's
                    // record. The client may leave its record in place, so this is said once per record.
                    if (!string.Equals(_lastIgnoredTicketId, request.TicketId, StringComparison.Ordinal)
                        || !string.Equals(_lastIgnoredReason, reason, StringComparison.Ordinal))
                    {
                        _lastIgnoredTicketId = request.TicketId;
                        _lastIgnoredReason = reason;
                        Log("ignored restart ticket '" + request.TicketId + "': " + reason);
                    }

                    break;
                default:
                    PublishRejection(request, reason);
                    break;
            }
        }

        private static void PublishRejection(RestartRequestRecord request, string reason)
        {
            if (string.Equals(_lastRejectedTicketId, request.TicketId, StringComparison.Ordinal)
                && string.Equals(_lastRejectedReason, reason, StringComparison.Ordinal))
            {
                return;
            }

            try
            {
                _control.WriteResult(RestartResultRecord.Rejected(_config.RunId, request, reason));
                _lastRejectedTicketId = request.TicketId;
                _lastRejectedReason = reason;
                Log("rejected restart ticket '" + request.TicketId + "': " + reason);
            }
            catch (Exception ex)
            {
                FailSession("result publication failed: " + ex.GetType().Name + ": " + ex.Message, DateTime.UtcNow);
            }
        }

        private static void PollCancel(DateTime now)
        {
            if (_machine.Ticket == null || _machine.Ticket.IsTerminal)
            {
                return;
            }

            string json;
            if (!_control.TryReadText(_control.CancelPath, out json))
            {
                return;
            }

            RestartCancelRecord cancel;
            try
            {
                cancel = RestartCancelRecord.Parse(json);
            }
            catch (SessionRecordException ex)
            {
                if (!string.Equals(ex.Message, _lastCancelError, StringComparison.Ordinal))
                {
                    _lastCancelError = ex.Message;
                    Log("ignoring an invalid cancel.json: " + ex.Message);
                }

                return;
            }

            _lastCancelError = null;
            if (!string.Equals(cancel.RunId, _config.RunId, StringComparison.Ordinal)
                || !string.Equals(cancel.TicketId, _machine.Ticket.TicketId, StringComparison.Ordinal))
            {
                return;
            }

            if (_machine.Cancel(now))
            {
                Persist();
                PublishIfChanged();
                RequestExit(true, now);
            }
        }

        private static void OnPlayModeStateChanged(PlayModeStateChange change)
        {
            try
            {
                if (_exited)
                {
                    return;
                }

                // Unity can raise a play-mode event immediately after a domain reload, before the first
                // Editor update. Restore the persisted session here as well, or the transition the ticket
                // is waiting for would be dropped and the operation would stall until its deadline.
                if (_machine == null && SessionState.GetBool(BegunKey, false))
                {
                    Resume();
                }

                if (_machine == null || _exitRequested)
                {
                    return;
                }

                SessionPlayModeChange observed;
                switch (change)
                {
                    case PlayModeStateChange.EnteredPlayMode:
                        observed = SessionPlayModeChange.EnteredPlayMode;
                        break;
                    case PlayModeStateChange.EnteredEditMode:
                        observed = SessionPlayModeChange.EnteredEditMode;
                        break;
                    case PlayModeStateChange.ExitingPlayMode:
                        observed = SessionPlayModeChange.ExitingPlayMode;
                        break;
                    default:
                        return;
                }

                DateTime now = DateTime.UtcNow;
                SessionEventEffect effect = PlayModeSessionEvents.Apply(_machine, observed, now);
                if (effect.Persist)
                {
                    Persist();
                }

                if (effect.Publish)
                {
                    PublishIfChanged();
                }

                if (effect.RequestExit)
                {
                    RequestExit(effect.ExitFailed, now);
                }
            }
            catch (Exception ex)
            {
                FailSession("play_mode_event_failed: " + ex.GetType().Name + ": " + ex.Message, DateTime.UtcNow);
            }
        }

        private static void BeginShutdown(DateTime now)
        {
            if (_exitRequested)
            {
                return;
            }

            Log("shutdown marker observed; stopping the session");
            _machine.BeginStop();
            Persist();
            PublishIfChanged();
            RequestExit(false, now);
        }

        private static void FailSession(string reason, DateTime now)
        {
            Log(reason);
            try
            {
                if (_machine != null)
                {
                    _machine.Fail(reason, now);
                    Persist();
                    PublishIfChanged();
                    RequestExit(true, now);
                    return;
                }
            }
            catch (Exception ex)
            {
                Log("failing the session also failed: " + ex.GetType().Name + ": " + ex.Message);
            }

            _exitRequested = true;
            _exitFailed = true;
            EditorApplication.Exit(1);
        }

        private static void RequestExit(bool failed, DateTime now)
        {
            if (_exitRequested)
            {
                return;
            }

            _exitRequested = true;
            _exitFailed = failed;
            _exitRequestedUtc = now;
            Persist();
            PublishIfChanged();
            BridgeEditorHost.Stop();
            PublishIfChanged();

            if (EditorApplication.isPlayingOrWillChangePlaymode)
            {
                Log("leaving Play Mode before the host exits");
                EditorApplication.ExitPlaymode();
                return;
            }

            FinishExit();
        }

        private static void StepExit(DateTime now)
        {
            if (EditorApplication.isPlayingOrWillChangePlaymode)
            {
                if (_exitRequestedUtc.HasValue
                    && (now - _exitRequestedUtc.Value).TotalSeconds > ExitWaitSeconds)
                {
                    Log("the Editor did not leave Play Mode in time; exiting anyway");
                    FinishExit();
                }

                return;
            }

            FinishExit();
        }

        private static void FinishExit()
        {
            if (_exited)
            {
                return;
            }

            _exited = true;
            if (!_exitFailed && _machine != null && _machine.HostPhase != PlayModeSessionPhase.Failed)
            {
                _machine.MarkStopped();
                Persist();
                PublishIfChanged();
            }

            Log("session host exiting (" + (_exitFailed ? "failure" : "clean") + ")");
            EditorApplication.update -= Tick;
            EditorApplication.Exit(_exitFailed ? 1 : 0);
        }

        private static void OnQuitting()
        {
            Persist();
        }

        private static void Persist()
        {
            if (_machine == null || _control == null)
            {
                return;
            }

            try
            {
                SessionState.SetString(SessionStateKey, SessionPersistence.Save(_machine, _control.Root));
                SessionState.SetBool(BegunKey, true);
            }
            catch (Exception ex)
            {
                Log("could not persist the session state: " + ex.GetType().Name + ": " + ex.Message);
            }
        }

        /// <summary>
        /// Publishes the mailbox once per state change, never once per poll. <c>result.json</c> is written
        /// only for a ticket, so a quiet poll writes nothing at all.
        /// </summary>
        private static void PublishIfChanged()
        {
            if (_machine == null || _control == null)
            {
                return;
            }

            if (_machine.Revision == _publishedRevision)
            {
                return;
            }

            try
            {
                _control.WriteHostState(HostState());
                if (_machine.Ticket != null)
                {
                    _control.WriteResult(_machine.Ticket.ToResult(_config.RunId));
                }

                _publishedRevision = _machine.Revision;
            }
            catch (Exception ex)
            {
                string reason = "control_record_publication_failed: " + ex.GetType().Name + ": " + ex.Message;
                Log(reason);
                _publishedRevision = -1;
                if (!_exitRequested)
                {
                    _machine.Fail(reason, DateTime.UtcNow);
                    RequestExit(true, DateTime.UtcNow);
                }
            }
        }

        private static HostStateRecord HostState()
        {
            return new HostStateRecord
            {
                RunId = _config.RunId,
                Phase = _machine.HostPhase,
                Generation = _machine.Generation,
                ScenePath = _machine.ScenePath,
                SceneHandle = _machine.SceneHandle,
                ReadyUtc = _machine.ReadyUtc,
                Error = _machine.Error,
                Capabilities = new[] { HostStateRecord.RestartCapability },
            };
        }

        private static string ProjectRoot()
        {
            string dataPath = Application.dataPath;
            if (!string.IsNullOrEmpty(dataPath))
            {
                string parent = Path.GetDirectoryName(dataPath.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
                if (!string.IsNullOrEmpty(parent))
                {
                    return parent;
                }
            }

            return Environment.CurrentDirectory;
        }

        private static void Log(string message)
        {
            Debug.Log(LogPrefix + " " + message);
        }
    }
}
