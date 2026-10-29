// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.Threading;
using Playtest.Bridge.Actions;
#if ENABLE_INPUT_SYSTEM
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.InputSystem.Controls;
using UnityEngine.InputSystem.LowLevel;
#endif

namespace Playtest.Bridge.Unity
{
    /// <summary>
    /// The Input System side of the executor: it queues the declared state of a keyboard, mouse or
    /// gamepad as Input System events and reads the controls back. Everything except
    /// <see cref="IsAvailable"/> and <see cref="TryValidateControl"/> runs on the Unity main thread.
    ///
    /// Controls are read only inside <c>InputSystem.onAfterUpdate</c> of an update the game can
    /// observe (dynamic, fixed or manual), the same context <c>SceneKeyboardState</c> uses, because
    /// the Editor keeps separate state buffers for editor updates. Queued events are never flushed
    /// by this class except for a caller that is itself the main thread: a manual update would
    /// consume a mouse delta before the game's own update could, and would hide a key press edge.
    ///
    /// A device that is absent is created and owned here and removed on dispose: the keyboard when
    /// the surface is created, the mouse and gamepad when an action first needs them. A physical
    /// device that exists is used as it is, so real input on it during a run is not distinguished
    /// from injected input; verification reports what the device shows.
    /// </summary>
    internal sealed class InputSystemSurface : IBridgeInputSurface, IDisposable
    {
#if ENABLE_INPUT_SYSTEM
        private static readonly Lazy<Dictionary<string, Key>> KeysByName = new Lazy<Dictionary<string, Key>>(BuildKeyNames);

        private Keyboard _keyboard;
        private Mouse _mouse;
        private Gamepad _gamepad;
        private bool _addedKeyboard;
        private bool _addedMouse;
        private bool _addedGamepad;
        private bool _wroteMouse;
        private bool _wroteGamepad;
        private bool _subscribed;
        private long _updateCount;
        private InputSettings.BackgroundBehavior _previousBackgroundBehavior;
        private InputSettings.EditorInputBehaviorInPlayMode _previousEditorInputBehavior;
        private bool _settingsCaptured;
        private bool _settingsChanged;
#endif

        public InputSystemSurface()
        {
#if ENABLE_INPUT_SYSTEM
            // Device creation and Input System state changes belong on the Unity main thread.
            // In batch mode the Game view has no focus, so the Input System disables a newly
            // added device and routes its events to editor updates instead of the game loop.
            // Unity's own input tests ignore focus for the same reason. This is scoped to batch
            // mode and restored on Dispose; an interactive Editor is left alone.
            CaptureAndApplyInputSettings();
            EnsureKeyboard(out _);
            InputSystem.onAfterUpdate += OnAfterUpdate;
            _subscribed = true;
#endif
        }

        /// <summary>Called inside an observable input update after it was counted.</summary>
        public Action UpdateObserved { get; set; }

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

        public long UpdateCount
        {
            get
            {
#if ENABLE_INPUT_SYSTEM
                return Interlocked.Read(ref _updateCount);
#else
                return 0;
#endif
            }
        }

        public bool TryValidateControl(BridgeControlInfo info, out string error)
        {
            error = null;
#if ENABLE_INPUT_SYSTEM
            if (info.Device == BridgeInputVocabulary.Keyboard && !TryParseKey(info.Control, out _))
            {
                error = "unknown key '" + info.Control + "'";
                return false;
            }

            return true;
#else
            error = "the Input System backend is not enabled in this project";
            return false;
#endif
        }

        public bool TryQueue(string device, IReadOnlyList<BridgeInputBinding> inputs, out string error)
        {
            error = null;
#if ENABLE_INPUT_SYSTEM
            switch (device)
            {
                case BridgeInputVocabulary.Keyboard:
                    return QueueKeyboard(inputs, out error);
                case BridgeInputVocabulary.Mouse:
                    return QueueMouse(inputs, out error);
                case BridgeInputVocabulary.Gamepad:
                    return QueueGamepad(inputs, out error);
                default:
                    error = "unsupported input device '" + device + "'";
                    return false;
            }
#else
            error = "the Input System backend is not enabled in this project";
            return false;
#endif
        }

        public bool TryQueueNeutral(string device, IReadOnlyList<BridgeInputBinding> inputs, out string error)
        {
            error = null;
#if ENABLE_INPUT_SYSTEM
            switch (device)
            {
                case BridgeInputVocabulary.Keyboard:
                    if (_keyboard != null)
                    {
                        InputSystem.QueueStateEvent(_keyboard, new KeyboardState());
                    }

                    return true;

                case BridgeInputVocabulary.Mouse:
                    // A delta resets itself on the next update; only held buttons need a release.
                    if (_mouse != null && HasMouseButton(inputs))
                    {
                        InputSystem.QueueDeltaStateEvent(_mouse.leftButton, (byte)0);
                    }

                    return true;

                case BridgeInputVocabulary.Gamepad:
                    if (_gamepad != null)
                    {
                        InputSystem.QueueStateEvent(_gamepad, new GamepadState());
                    }

                    return true;

                default:
                    error = "unsupported input device '" + device + "'";
                    return false;
            }
#else
            error = "the Input System backend is not enabled in this project";
            return false;
#endif
        }

        public bool TryRead(BridgeInputBinding input, out BridgeInputReading reading, out string error)
        {
            reading = default(BridgeInputReading);
            error = null;
#if ENABLE_INPUT_SYSTEM
            if (!BridgeInputVocabulary.TryResolve(input.Device, input.Control, out BridgeControlInfo info, out error))
            {
                return false;
            }

            switch (info.Device)
            {
                case BridgeInputVocabulary.Keyboard:
                    if (_keyboard == null || !TryParseKey(info.Control, out Key key))
                    {
                        error = "no keyboard device to read";
                        return false;
                    }

                    reading.Pressed = _keyboard[key].isPressed;
                    return true;

                case BridgeInputVocabulary.Mouse:
                    return ReadMouse(info, ref reading, out error);

                default:
                    return ReadGamepad(info, ref reading, out error);
            }
#else
            error = "the Input System backend is not enabled in this project";
            return false;
#endif
        }

        public void Flush()
        {
#if ENABLE_INPUT_SYSTEM
            InputSystem.Update();
#endif
        }

        public void Dispose()
        {
#if ENABLE_INPUT_SYSTEM
            if (_subscribed)
            {
                _subscribed = false;
                InputSystem.onAfterUpdate -= OnAfterUpdate;
            }

            try
            {
                if (_keyboard != null)
                {
                    InputSystem.QueueStateEvent(_keyboard, new KeyboardState());
                }

                // Only a device this surface wrote to is reset; a physical one it never touched is left alone.
                if (_gamepad != null && _wroteGamepad)
                {
                    InputSystem.QueueStateEvent(_gamepad, new GamepadState());
                }

                if (_mouse != null && _wroteMouse)
                {
                    InputSystem.QueueDeltaStateEvent(_mouse.leftButton, (byte)0);
                }

                RemoveOwned(ref _keyboard, ref _addedKeyboard);
                RemoveOwned(ref _mouse, ref _addedMouse);
                RemoveOwned(ref _gamepad, ref _addedGamepad);
            }
            catch (Exception)
            {
            }
            finally
            {
                _keyboard = null;
                _mouse = null;
                _gamepad = null;
            }

            RestoreInputSettings();
#endif
        }

#if ENABLE_INPUT_SYSTEM
        private void OnAfterUpdate()
        {
            InputUpdateType type = InputState.currentUpdateType;
            if (type != InputUpdateType.Dynamic && type != InputUpdateType.Fixed && type != InputUpdateType.Manual)
            {
                return;
            }

            Interlocked.Increment(ref _updateCount);
            Action observed = UpdateObserved;
            if (observed != null)
            {
                observed();
            }
        }

        private static Dictionary<string, Key> BuildKeyNames()
        {
            // Names, not numbers: Enum.TryParse would also accept "1" as the key whose value is 1.
            var map = new Dictionary<string, Key>(StringComparer.OrdinalIgnoreCase);
            foreach (string name in Enum.GetNames(typeof(Key)))
            {
                var key = (Key)Enum.Parse(typeof(Key), name);
                if (key != Key.None)
                {
                    map[name] = key;
                }
            }

            return map;
        }

        private static bool TryParseKey(string control, out Key key)
        {
            key = Key.None;
            return !string.IsNullOrEmpty(control) && KeysByName.Value.TryGetValue(control, out key);
        }

        private bool QueueKeyboard(IReadOnlyList<BridgeInputBinding> inputs, out string error)
        {
            var keys = new List<Key>(inputs.Count);
            for (int i = 0; i < inputs.Count; i++)
            {
                if (!TryParseKey(inputs[i].Control, out Key key))
                {
                    error = "unknown key '" + inputs[i].Control + "'";
                    return false;
                }

                keys.Add(key);
            }

            if (!EnsureKeyboard(out error))
            {
                return false;
            }

            // One state event for every key: they are down in the same input update.
            InputSystem.QueueStateEvent(_keyboard, new KeyboardState(keys.ToArray()));
            return true;
        }

        private bool QueueMouse(IReadOnlyList<BridgeInputBinding> inputs, out string error)
        {
            if (!EnsureMouse(out error))
            {
                return false;
            }

            int buttons = 0;
            bool hasButton = false;
            bool hasDelta = false;
            Vector2 delta = Vector2.zero;
            for (int i = 0; i < inputs.Count; i++)
            {
                BridgeInputVocabulary.TryResolve(inputs[i].Device, inputs[i].Control, out BridgeControlInfo info, out error);
                if (info.Kind == BridgeControlKind.Impulse)
                {
                    hasDelta = true;
                    delta = new Vector2(
                        (float)(inputs[i].X.Value * BridgeInputVocabulary.MouseDeltaPixelsPerUnit),
                        (float)(inputs[i].Y.Value * BridgeInputVocabulary.MouseDeltaPixelsPerUnit));
                }
                else
                {
                    hasButton = true;
                    buttons |= 1 << (int)MouseButtonOf(info.Control);
                }
            }

            error = null;

            // Delta events touch only the control they name, so the pointer position and any
            // other device state is left alone. Every mouse button is stored in the first byte of the
            // button field, and the left button is the one a delta event may be addressed to.
            if (hasButton)
            {
                InputSystem.QueueDeltaStateEvent(_mouse.leftButton, (byte)buttons);
                _wroteMouse = true;
            }

            if (hasDelta)
            {
                InputSystem.QueueDeltaStateEvent(_mouse.delta, delta);
                _wroteMouse = true;
            }

            return true;
        }

        private bool QueueGamepad(IReadOnlyList<BridgeInputBinding> inputs, out string error)
        {
            if (!EnsureGamepad(out error))
            {
                return false;
            }

            var state = new GamepadState();
            for (int i = 0; i < inputs.Count; i++)
            {
                BridgeInputBinding input = inputs[i];
                BridgeInputVocabulary.TryResolve(input.Device, input.Control, out BridgeControlInfo info, out error);
                switch (info.Kind)
                {
                    case BridgeControlKind.Button:
                        state.buttons |= 1u << (int)GamepadButtonOf(info.Control);
                        break;

                    case BridgeControlKind.Dpad:
                        if (input.Y.Value > 0) state.buttons |= 1u << (int)GamepadButton.DpadUp;
                        if (input.Y.Value < 0) state.buttons |= 1u << (int)GamepadButton.DpadDown;
                        if (input.X.Value < 0) state.buttons |= 1u << (int)GamepadButton.DpadLeft;
                        if (input.X.Value > 0) state.buttons |= 1u << (int)GamepadButton.DpadRight;
                        break;

                    case BridgeControlKind.Axis:
                        if (info.Control == "leftTrigger") state.leftTrigger = (float)input.X.Value;
                        else state.rightTrigger = (float)input.X.Value;
                        break;

                    default:
                        // The declared stick values go into the state as they are: no dead zone,
                        // no normalization, no clamp.
                        var stick = new Vector2((float)input.X.Value, (float)input.Y.Value);
                        if (info.Control == "leftStick") state.leftStick = stick;
                        else state.rightStick = stick;
                        break;
                }
            }

            error = null;
            InputSystem.QueueStateEvent(_gamepad, state);
            _wroteGamepad = true;
            return true;
        }

        private bool ReadMouse(BridgeControlInfo info, ref BridgeInputReading reading, out string error)
        {
            error = null;
            if (_mouse == null)
            {
                error = "no mouse device to read";
                return false;
            }

            if (info.Kind == BridgeControlKind.Impulse)
            {
                Vector2 delta = _mouse.delta.ReadValue();
                reading.X = delta.x;
                reading.Y = delta.y;
                return true;
            }

            switch (info.Control)
            {
                case "leftButton": reading.Pressed = _mouse.leftButton.isPressed; break;
                case "rightButton": reading.Pressed = _mouse.rightButton.isPressed; break;
                case "middleButton": reading.Pressed = _mouse.middleButton.isPressed; break;
                case "forwardButton": reading.Pressed = _mouse.forwardButton.isPressed; break;
                default: reading.Pressed = _mouse.backButton.isPressed; break;
            }

            return true;
        }

        private bool ReadGamepad(BridgeControlInfo info, ref BridgeInputReading reading, out string error)
        {
            error = null;
            if (_gamepad == null)
            {
                error = "no gamepad device to read";
                return false;
            }

            switch (info.Kind)
            {
                case BridgeControlKind.Button:
                    reading.Pressed = GamepadButtonControl(info.Control).isPressed;
                    return true;

                case BridgeControlKind.Dpad:
                    DpadControl dpad = _gamepad.dpad;
                    reading.X = (dpad.right.isPressed ? 1 : 0) - (dpad.left.isPressed ? 1 : 0);
                    reading.Y = (dpad.up.isPressed ? 1 : 0) - (dpad.down.isPressed ? 1 : 0);
                    return true;

                case BridgeControlKind.Axis:
                    ButtonControl trigger = info.Control == "leftTrigger" ? _gamepad.leftTrigger : _gamepad.rightTrigger;
                    reading.X = trigger.ReadUnprocessedValue();
                    return true;

                default:
                    // The raw state, before the stick's dead zone processor.
                    Vector2 stick = (info.Control == "leftStick" ? _gamepad.leftStick : _gamepad.rightStick).ReadUnprocessedValue();
                    reading.X = stick.x;
                    reading.Y = stick.y;
                    return true;
            }
        }

        private ButtonControl GamepadButtonControl(string control)
        {
            switch (control)
            {
                case "buttonSouth": return _gamepad.buttonSouth;
                case "buttonNorth": return _gamepad.buttonNorth;
                case "buttonEast": return _gamepad.buttonEast;
                case "buttonWest": return _gamepad.buttonWest;
                case "leftShoulder": return _gamepad.leftShoulder;
                case "rightShoulder": return _gamepad.rightShoulder;
                case "leftStickPress": return _gamepad.leftStickButton;
                case "rightStickPress": return _gamepad.rightStickButton;
                case "start": return _gamepad.startButton;
                default: return _gamepad.selectButton;
            }
        }

        private static GamepadButton GamepadButtonOf(string control)
        {
            switch (control)
            {
                case "buttonSouth": return GamepadButton.South;
                case "buttonNorth": return GamepadButton.North;
                case "buttonEast": return GamepadButton.East;
                case "buttonWest": return GamepadButton.West;
                case "leftShoulder": return GamepadButton.LeftShoulder;
                case "rightShoulder": return GamepadButton.RightShoulder;
                case "leftStickPress": return GamepadButton.LeftStick;
                case "rightStickPress": return GamepadButton.RightStick;
                case "start": return GamepadButton.Start;
                default: return GamepadButton.Select;
            }
        }

        private static MouseButton MouseButtonOf(string control)
        {
            switch (control)
            {
                case "leftButton": return MouseButton.Left;
                case "rightButton": return MouseButton.Right;
                case "middleButton": return MouseButton.Middle;
                case "forwardButton": return MouseButton.Forward;
                default: return MouseButton.Back;
            }
        }

        private static bool HasMouseButton(IReadOnlyList<BridgeInputBinding> inputs)
        {
            for (int i = 0; i < inputs.Count; i++)
            {
                if (BridgeInputVocabulary.TryResolve(inputs[i].Device, inputs[i].Control, out BridgeControlInfo info, out _)
                    && info.Kind == BridgeControlKind.Button)
                {
                    return true;
                }
            }

            return false;
        }

        private bool EnsureKeyboard(out string error)
        {
            error = null;
            if (_keyboard != null)
            {
                return true;
            }

            try
            {
                _keyboard = Keyboard.current;
                if (_keyboard == null)
                {
                    _keyboard = InputSystem.AddDevice<Keyboard>();
                    _addedKeyboard = _keyboard != null;
                }

                EnableIfNeeded(_keyboard);
            }
            catch (Exception ex)
            {
                error = ex.GetType().Name + ": " + ex.Message;
                return false;
            }

            if (_keyboard == null)
            {
                error = "no keyboard device is available";
                return false;
            }

            return true;
        }

        private bool EnsureMouse(out string error)
        {
            error = null;
            if (_mouse != null)
            {
                return true;
            }

            try
            {
                _mouse = Mouse.current;
                if (_mouse == null)
                {
                    _mouse = InputSystem.AddDevice<Mouse>();
                    _addedMouse = _mouse != null;
                }

                EnableIfNeeded(_mouse);
            }
            catch (Exception ex)
            {
                error = ex.GetType().Name + ": " + ex.Message;
                return false;
            }

            if (_mouse == null)
            {
                error = "no mouse device is available";
                return false;
            }

            return true;
        }

        private bool EnsureGamepad(out string error)
        {
            error = null;
            if (_gamepad != null)
            {
                return true;
            }

            try
            {
                // A synthetic gamepad exists only because an action declared a gamepad control; it
                // is owned by this surface and removed with it.
                _gamepad = Gamepad.current;
                if (_gamepad == null)
                {
                    _gamepad = InputSystem.AddDevice<Gamepad>();
                    _addedGamepad = _gamepad != null;
                }

                EnableIfNeeded(_gamepad);
            }
            catch (Exception ex)
            {
                error = ex.GetType().Name + ": " + ex.Message;
                return false;
            }

            if (_gamepad == null)
            {
                error = "no gamepad device is available";
                return false;
            }

            return true;
        }

        private static void EnableIfNeeded(InputDevice device)
        {
            if (device == null || device.enabled)
            {
                return;
            }

            try
            {
                InputSystem.EnableDevice(device);
            }
            catch (Exception)
            {
            }
        }

        private static void RemoveOwned<TDevice>(ref TDevice device, ref bool owned)
            where TDevice : InputDevice
        {
            if (owned && device != null)
            {
                InputSystem.RemoveDevice(device);
            }

            device = null;
            owned = false;
        }

        private void CaptureAndApplyInputSettings()
        {
            if (!Application.isBatchMode)
            {
                return;
            }

            try
            {
                InputSettings settings = InputSystem.settings;
                if (settings == null)
                {
                    return;
                }

                _previousBackgroundBehavior = settings.backgroundBehavior;
                _previousEditorInputBehavior = settings.editorInputBehaviorInPlayMode;
                _settingsCaptured = true;

                if (settings.backgroundBehavior != InputSettings.BackgroundBehavior.IgnoreFocus)
                {
                    settings.backgroundBehavior = InputSettings.BackgroundBehavior.IgnoreFocus;
                    _settingsChanged = true;
                }

                if (settings.editorInputBehaviorInPlayMode != InputSettings.EditorInputBehaviorInPlayMode.AllDeviceInputAlwaysGoesToGameView)
                {
                    settings.editorInputBehaviorInPlayMode = InputSettings.EditorInputBehaviorInPlayMode.AllDeviceInputAlwaysGoesToGameView;
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
                InputSettings settings = InputSystem.settings;
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
#endif
    }
}
