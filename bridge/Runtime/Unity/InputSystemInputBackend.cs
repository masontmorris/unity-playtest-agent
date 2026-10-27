using System;
using System.Collections.Generic;
using Playtest.Bridge.Actions;

namespace Playtest.Bridge.Unity
{
    /// <summary>
    /// Input System keyboard backend. The manifest action names a key; the bridge queues a
    /// keyboard state and releases it at the lease boundary. No game control name is compiled in:
    /// the device and control come from the action catalog.
    /// </summary>
    public sealed class InputSystemInputBackend : IBridgeInputBackend
    {
        private readonly object _gate = new object();
        private readonly HashSet<UnityEngine.InputSystem.Key> _held =
            new HashSet<UnityEngine.InputSystem.Key>();
        private UnityEngine.InputSystem.Keyboard _keyboard;
        private bool _addedKeyboard;
#if ENABLE_INPUT_SYSTEM
        private UnityEngine.InputSystem.InputSettings.BackgroundBehavior _previousBackgroundBehavior;
        private UnityEngine.InputSystem.InputSettings.EditorInputBehaviorInPlayMode _previousEditorInputBehavior;
        private bool _settingsCaptured;
        private bool _settingsChanged;
#endif

        public InputSystemInputBackend()
        {
#if ENABLE_INPUT_SYSTEM
            // Device creation belongs on the Unity main thread; the constructor is called there.
            // QueueStateEvent is safe from the transport thread afterwards.
            //
            // In batch mode the Game view has no focus, so the Input System disables a newly added
            // keyboard and routes its events to editor updates instead of the game loop. Unity's
            // own input tests ignore focus for the same reason. This is scoped to batch mode and
            // restored on Dispose; an interactive Editor is left alone.
            CaptureAndApplyInputSettings();
            EnsureKeyboard(create: true);
            EnableKeyboardIfNeeded();
#endif
        }

        public string Name
        {
            get { return "input_system"; }
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

            lock (_gate)
            {
                if (!EnsureKeyboard(create: false, out error))
                {
                    return false;
                }

                _held.Add(key);
                UnityEngine.InputSystem.InputSystem.QueueStateEvent(
                    _keyboard,
                    new UnityEngine.InputSystem.LowLevel.KeyboardState(key));
                return true;
            }
#else
            error = "the Input System backend is not enabled in this project";
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
                try
                {
                    if (_keyboard != null)
                    {
                        UnityEngine.InputSystem.InputSystem.QueueStateEvent(
                            _keyboard,
                            new UnityEngine.InputSystem.LowLevel.KeyboardState());
                    }

                    _held.Clear();
                    releaseVerified = true;
                    return true;
                }
                catch (Exception ex)
                {
                    error = ex.GetType().Name + ": " + ex.Message;
                    return false;
                }
            }
#else
            error = "the Input System backend is not enabled in this project";
            return false;
#endif
        }

        public void Dispose()
        {
#if ENABLE_INPUT_SYSTEM
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
                }
            }

            RestoreInputSettings();
#endif
        }

#if ENABLE_INPUT_SYSTEM
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
                // A keyboard that existed before the settings change may still carry the
                // "disabled while in background" flag; clear it before the first action.
                UnityEngine.InputSystem.InputSystem.EnableDevice(_keyboard);
            }
            catch (Exception)
            {
            }
        }
#endif

#if ENABLE_INPUT_SYSTEM
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
            try
            {
                EnsureKeyboard(create, out _);
            }
            catch (Exception)
            {
            }
        }
#endif
    }
}
