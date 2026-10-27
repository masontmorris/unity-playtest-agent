using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Threading;
using Playtest.Bridge.Actions;
#if UNITY_EDITOR
using UnityEditor;
#endif

namespace Playtest.Bridge.Unity
{
    /// <summary>
    /// Input System keyboard backend. Device state is queued by the transport/lease threads and
    /// applied on the Unity main thread; the Input System's Temp allocations and device calls are
    /// not safe off it. The manifest action names a device control, never a game control.
    /// </summary>
    public sealed class InputSystemInputBackend : IBridgeInputBackend
    {
        private readonly object _gate = new object();
        private readonly HashSet<UnityEngine.InputSystem.Key> _held =
            new HashSet<UnityEngine.InputSystem.Key>();
        private readonly List<UnityEngine.InputSystem.Key> _released =
            new List<UnityEngine.InputSystem.Key>();
        private readonly ConcurrentQueue<Action> _commands = new ConcurrentQueue<Action>();
        private readonly ManualResetEventSlim _verifyEvent = new ManualResetEventSlim(false);
        private readonly int _mainThreadId;
        private UnityEngine.InputSystem.Keyboard _keyboard;
        private bool _addedKeyboard;
        private bool _subscribed;
        private bool _verifyPending;
        private bool _releaseNeedsFlush;
        private volatile bool _verifyResult;
        private string _verifyError;
#if ENABLE_INPUT_SYSTEM
        private UnityEngine.InputSystem.InputSettings.BackgroundBehavior _previousBackgroundBehavior;
        private UnityEngine.InputSystem.InputSettings.EditorInputBehaviorInPlayMode _previousEditorInputBehavior;
        private bool _settingsCaptured;
        private bool _settingsChanged;
#endif

        public InputSystemInputBackend()
        {
            _mainThreadId = Thread.CurrentThread.ManagedThreadId;
#if ENABLE_INPUT_SYSTEM
            // Device creation and Input System state changes belong on the Unity main thread.
            // In batch mode the Game view has no focus, so the Input System disables a newly
            // added keyboard and routes its events to editor updates instead of the game loop.
            // Unity's own input tests ignore focus for the same reason. This is scoped to batch
            // mode and restored on Dispose; an interactive Editor is left alone.
            CaptureAndApplyInputSettings();
            EnsureKeyboard(create: true);
            EnableKeyboardIfNeeded();
            Subscribe();
#endif
        }

        public string Name
        {
            get { return "input_system"; }
        }

        public IReadOnlyList<string> ReleasedInputs
        {
            get
            {
                lock (_gate)
                {
                    var names = new List<string>(_released.Count);
                    for (int i = 0; i < _released.Count; i++)
                    {
                        names.Add("key:" + _released[i].ToString().ToLowerInvariant());
                    }

                    return names;
                }
            }
        }

        public bool IsAvailable
        {
            get
            {
#if ENABLE_INPUT_SYSTEM
                return true;
#else
                return false;
#endif
            }
        }

        public bool TryValidate(BridgeActionDefinition action, out string error)
        {
            error = null;
            if (action == null)
            {
                error = "missing action definition";
                return false;
            }

#if ENABLE_INPUT_SYSTEM
            if (!string.Equals(action.Backend, "input_system", StringComparison.OrdinalIgnoreCase))
            {
                error = "unsupported input backend '" + action.Backend + "'";
                return false;
            }

            if (!string.Equals(action.Device, "Keyboard", StringComparison.OrdinalIgnoreCase))
            {
                error = "unsupported input device '" + action.Device + "' in v0";
                return false;
            }

            if (!Enum.TryParse(action.Control, true, out UnityEngine.InputSystem.Key key)
                || key == UnityEngine.InputSystem.Key.None)
            {
                error = "unknown key '" + action.Control + "'";
                return false;
            }

            return true;
#else
            error = "the Input System backend is not enabled in this project";
            return false;
#endif
        }

        public bool TryApply(BridgeActionDefinition action, out string error)
        {
            if (!TryValidate(action, out error))
            {
                return false;
            }

#if ENABLE_INPUT_SYSTEM
            if (!Enum.TryParse(action.Control, true, out UnityEngine.InputSystem.Key key))
            {
                error = "unknown key '" + action.Control + "'";
                return false;
            }

            lock (_gate)
            {
                if (_keyboard == null)
                {
                    error = "no keyboard device is available";
                    return false;
                }

                _held.Add(key);
                _released.Clear();
                _verifyPending = false;
                _verifyResult = false;
                _verifyEvent.Reset();
            }

            RunOnMain(() => ApplyKey(key));
            return true;
#else
            return false;
#endif
        }

        public bool TryRelease(out bool releaseVerified, out string error)
        {
            error = null;
            releaseVerified = false;
#if ENABLE_INPUT_SYSTEM
            lock (_gate)
            {
                _released.Clear();
                foreach (UnityEngine.InputSystem.Key key in _held)
                {
                    _released.Add(key);
                }

                _held.Clear();
                _verifyPending = _released.Count > 0;
                _verifyResult = false;
                _verifyError = null;
                _verifyEvent.Reset();
                if (!_verifyPending)
                {
                    _verifyEvent.Set();
                }
            }

            RunOnMain(ReleaseKey);
            releaseVerified = true;
            return true;
#else
            error = "the Input System backend is not enabled in this project";
            return false;
#endif
        }

        public bool TryVerifyRelease(out string error)
        {
            error = null;
#if ENABLE_INPUT_SYSTEM
            bool onMain = Thread.CurrentThread.ManagedThreadId == _mainThreadId;
            if (onMain)
            {
                // Flush the queued release before reading the device state. Without this a stop
                // issued from the main thread (scene change, host shutdown) could not observe the
                // release it just requested, because the main thread is blocked inside the stop.
                Pump();
                lock (_gate)
                {
                    if (_released.Count == 0)
                    {
                        return true;
                    }
                }

                return VerifyReleasedKeys(out error);
            }

            if (_verifyEvent.Wait(200))
            {
                if (_verifyResult)
                {
                    return true;
                }

                error = _verifyError ?? "release was not verified";
                return false;
            }

            error = _verifyError ?? "release verification timed out";
            return false;
#else
            error = "the Input System backend is not enabled in this project";
            return false;
#endif
        }

        public void Dispose()
        {
#if ENABLE_INPUT_SYSTEM
            Unsubscribe();
            if (Thread.CurrentThread.ManagedThreadId == _mainThreadId)
            {
                Pump();
            }

            lock (_gate)
            {
                try
                {
                    if (_keyboard != null)
                    {
                        UnityEngine.InputSystem.InputSystem.QueueStateEvent(
                            _keyboard,
                            new UnityEngine.InputSystem.LowLevel.KeyboardState());
                    }

                    if (_addedKeyboard && _keyboard != null)
                    {
                        UnityEngine.InputSystem.InputSystem.RemoveDevice(_keyboard);
                    }
                }
                catch (Exception)
                {
                }
                finally
                {
                    _keyboard = null;
                    _addedKeyboard = false;
                    _held.Clear();
                    _released.Clear();
                    _verifyPending = false;
                    _verifyEvent.Set();
                }
            }

            RestoreInputSettings();
#endif
        }

#if ENABLE_INPUT_SYSTEM
        private void Subscribe()
        {
            if (_subscribed)
            {
                return;
            }

            _subscribed = true;
#if UNITY_EDITOR
            EditorApplication.update += Pump;
#else
            UnityEngine.Application.onBeforeRender += Pump;
#endif
        }

        private void Unsubscribe()
        {
            if (!_subscribed)
            {
                return;
            }

            _subscribed = false;
#if UNITY_EDITOR
            EditorApplication.update -= Pump;
#else
            UnityEngine.Application.onBeforeRender -= Pump;
#endif
        }

        private void RunOnMain(Action command)
        {
            if (command == null)
            {
                return;
            }

            if (Thread.CurrentThread.ManagedThreadId == _mainThreadId)
            {
                command();
                return;
            }

            _commands.Enqueue(command);
        }

        private void Pump()
        {
            if (Thread.CurrentThread.ManagedThreadId != _mainThreadId)
            {
                return;
            }

            while (_commands.TryDequeue(out Action command))
            {
                try
                {
                    command();
                }
                catch (Exception ex)
                {
                    _verifyError = ex.GetType().Name + ": " + ex.Message;
                    _verifyEvent.Set();
                }
            }

            bool flush;
            lock (_gate)
            {
                flush = _releaseNeedsFlush;
                _releaseNeedsFlush = false;
            }

            if (flush)
            {
                FlushInput();
            }

            lock (_gate)
            {
                if (!_verifyPending || _released.Count == 0)
                {
                    return;
                }
            }

            if (VerifyReleasedKeys(out string error))
            {
                lock (_gate)
                {
                    _released.Clear();
                    _verifyPending = false;
                    _verifyResult = true;
                    _verifyError = null;
                }

                _verifyEvent.Set();
            }
            else
            {
                _verifyError = error;
            }
        }

        private void ApplyKey(UnityEngine.InputSystem.Key key)
        {
            if (_keyboard == null)
            {
                return;
            }

            UnityEngine.InputSystem.InputSystem.QueueStateEvent(
                _keyboard,
                new UnityEngine.InputSystem.LowLevel.KeyboardState(key));
        }

        private void ReleaseKey()
        {
            if (_keyboard == null)
            {
                return;
            }

            UnityEngine.InputSystem.InputSystem.QueueStateEvent(
                _keyboard,
                new UnityEngine.InputSystem.LowLevel.KeyboardState());
            _releaseNeedsFlush = true;
        }

        /// <summary>
        /// Applies queued device state now so the released state can be read in the same call.
        /// Safe while the main thread is blocked in a stop; the Input System is not already
        /// inside its own update on that path.
        /// </summary>
        private void FlushInput()
        {
            try
            {
                UnityEngine.InputSystem.InputSystem.Update();
            }
            catch (Exception ex)
            {
                _verifyError = ex.GetType().Name + ": " + ex.Message;
            }
        }

        private bool VerifyReleasedKeys(out string error)
        {
            error = null;
            lock (_gate)
            {
                if (_released.Count == 0)
                {
                    return true;
                }

                UnityEngine.InputSystem.Keyboard keyboard = UnityEngine.InputSystem.Keyboard.current;
                if (keyboard == null)
                {
                    _released.Clear();
                    return true;
                }

                for (int i = 0; i < _released.Count; i++)
                {
                    if (keyboard[_released[i]].isPressed)
                    {
                        error = "key still pressed: " + _released[i];
                        return false;
                    }
                }

                _released.Clear();
                return true;
            }
        }

        private void CaptureAndApplyInputSettings()
        {
            if (!UnityEngine.Application.isBatchMode)
            {
                return;
            }

            try
            {
                UnityEngine.InputSystem.InputSettings settings = UnityEngine.InputSystem.InputSystem.settings;
                if (settings == null)
                {
                    return;
                }

                _previousBackgroundBehavior = settings.backgroundBehavior;
                _previousEditorInputBehavior = settings.editorInputBehaviorInPlayMode;
                _settingsCaptured = true;

                if (settings.backgroundBehavior != UnityEngine.InputSystem.InputSettings.BackgroundBehavior.IgnoreFocus)
                {
                    settings.backgroundBehavior = UnityEngine.InputSystem.InputSettings.BackgroundBehavior.IgnoreFocus;
                    _settingsChanged = true;
                }

                if (settings.editorInputBehaviorInPlayMode !=
                    UnityEngine.InputSystem.InputSettings.EditorInputBehaviorInPlayMode.AllDeviceInputAlwaysGoesToGameView)
                {
                    settings.editorInputBehaviorInPlayMode =
                        UnityEngine.InputSystem.InputSettings.EditorInputBehaviorInPlayMode.AllDeviceInputAlwaysGoesToGameView;
                    _settingsChanged = true;
                }
            }
            catch (Exception)
            {
                // Injection still reports its own failures; a settings change that cannot be made
                // must not stop the bridge from starting.
            }
        }

        private void RestoreInputSettings()
        {
            if (!_settingsCaptured)
            {
                return;
            }

            _settingsCaptured = false;
            if (!_settingsChanged)
            {
                return;
            }

            _settingsChanged = false;
            try
            {
                UnityEngine.InputSystem.InputSettings settings = UnityEngine.InputSystem.InputSystem.settings;
                if (settings == null)
                {
                    return;
                }

                settings.backgroundBehavior = _previousBackgroundBehavior;
                settings.editorInputBehaviorInPlayMode = _previousEditorInputBehavior;
            }
            catch (Exception)
            {
            }
        }

        private void EnableKeyboardIfNeeded()
        {
            if (_keyboard == null || _keyboard.enabled)
            {
                return;
            }

            try
            {
                UnityEngine.InputSystem.InputSystem.EnableDevice(_keyboard);
            }
            catch (Exception)
            {
            }
        }

        private bool EnsureKeyboard(bool create, out string error)
        {
            error = null;
            if (_keyboard != null)
            {
                return true;
            }

            _keyboard = UnityEngine.InputSystem.Keyboard.current;
            if (_keyboard == null && create)
            {
                try
                {
                    _keyboard = UnityEngine.InputSystem.InputSystem.AddDevice<UnityEngine.InputSystem.Keyboard>();
                    _addedKeyboard = _keyboard != null;
                }
                catch (Exception ex)
                {
                    error = ex.GetType().Name + ": " + ex.Message;
                    return false;
                }
            }

            if (_keyboard == null)
            {
                error = "no keyboard device is available";
                return false;
            }

            return true;
        }

        private void EnsureKeyboard(bool create)
        {
            EnsureKeyboard(create, out _);
        }
#endif
    }
}
