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
                if (!EnsureKeyboard(out error))
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
#endif
        }

#if ENABLE_INPUT_SYSTEM
        private bool EnsureKeyboard(out string error)
        {
            error = null;
            if (_keyboard != null)
            {
                return true;
            }

            _keyboard = UnityEngine.InputSystem.Keyboard.current;
            if (_keyboard == null)
            {
                _keyboard = UnityEngine.InputSystem.InputSystem.AddDevice<UnityEngine.InputSystem.Keyboard>();
                _addedKeyboard = _keyboard != null;
            }

            if (_keyboard == null)
            {
                error = "no keyboard device is available";
                return false;
            }

            return true;
        }
#endif
    }
}
