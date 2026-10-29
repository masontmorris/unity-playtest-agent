// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.InputSystem.LowLevel;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>
    /// Captures the actual keyboard, mouse and gamepad state in game input updates, not the Editor
    /// input buffer, so a sample can show which controls were held while it ran. A device that is
    /// absent is <c>null</c>, never an empty list. Values are the raw device state: a stick is
    /// read before its dead zone, and a mouse delta is only visible in the update that carried it,
    /// so a throttled capture can miss a one-update impulse. The receipts of an executed action,
    /// not this snapshot, are the evidence of an applied mouse delta.
    ///
    /// <see cref="SceneKeyboardState"/> reads the same capture and keeps its original shape.
    /// </summary>
    public static class SceneInputState
    {
        private const double MinimumIntervalS = 0.02;
        private static readonly object Gate = new object();
        private static bool _active;
        private static Dictionary<string, object> _devices;
        private static double? _realS;
        private static double? _wallS;
        private static string _updateType;
        private static string _error;

        public static void Start()
        {
            lock (Gate)
            {
                if (_active) throw new InvalidOperationException("input observation is already active");
                _devices = null; _realS = null; _wallS = null; _updateType = null; _error = null;
                _active = true;
#if ENABLE_INPUT_SYSTEM
                InputSystem.onAfterUpdate += Capture;
#endif
            }
        }

        public static void Stop()
        {
            lock (Gate)
            {
                if (!_active) return;
#if ENABLE_INPUT_SYSTEM
                InputSystem.onAfterUpdate -= Capture;
#endif
                _active = false; _devices = null; _realS = null; _wallS = null; _updateType = null; _error = null;
            }
        }

        /// <summary>
        /// <c>devices</c> maps Keyboard, Mouse and Gamepad to <c>{pressed, values}</c> or null.
        /// <c>pressed</c> lists the held buttons; <c>values</c> holds the stick, trigger, dpad and
        /// delta readings as numbers or <c>[x, y]</c> arrays.
        /// </summary>
        public static Dictionary<string, object> Snapshot()
        {
            lock (Gate)
                return new Dictionary<string, object>
                {
                    ["active"] = _active,
                    ["devices"] = _devices == null ? null : new Dictionary<string, object>(_devices),
                    ["real_time_s"] = _realS,
                    ["wall_s"] = _wallS,
                    ["update_type"] = _updateType,
                    ["error"] = _error,
                };
        }

        /// <summary>The original keyboard-only view: <c>keys</c> is null when no keyboard was read.</summary>
        internal static Dictionary<string, object> KeyboardSnapshot()
        {
            lock (Gate)
            {
                List<string> keys = null;
                if (_devices != null && _devices.TryGetValue("Keyboard", out object keyboard) && keyboard is Dictionary<string, object> state)
                    keys = new List<string>((List<string>)state["pressed"]);
                return new Dictionary<string, object>
                {
                    ["active"] = _active,
                    ["keys"] = keys,
                    ["real_time_s"] = _realS,
                    ["wall_s"] = _wallS,
                    ["update_type"] = _updateType,
                    ["error"] = _error,
                };
            }
        }

#if ENABLE_INPUT_SYSTEM
        private static void Capture()
        {
            InputUpdateType type = InputState.currentUpdateType;
            if (type != InputUpdateType.Dynamic && type != InputUpdateType.Fixed) return;
            lock (Gate)
            {
                if (!_active) return;
                try
                {
                    double now = Time.realtimeSinceStartupAsDouble;
                    if (_realS.HasValue && now - _realS.Value < MinimumIntervalS) return;
                    _devices = new Dictionary<string, object>
                    {
                        ["Keyboard"] = CaptureKeyboard(Keyboard.current),
                        ["Mouse"] = CaptureMouse(Mouse.current),
                        ["Gamepad"] = CaptureGamepad(Gamepad.current),
                    };
                    _realS = now;
                    _wallS = (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
                    _updateType = type.ToString().ToLowerInvariant();
                    _error = null;
                }
                catch (Exception ex)
                {
                    _devices = null; _realS = null; _wallS = null; _updateType = null;
                    _error = ex.GetType().Name + ": " + ex.Message;
                }
            }
        }

        private static Dictionary<string, object> CaptureKeyboard(Keyboard keyboard)
        {
            if (keyboard == null) return null;
            var pressed = new List<string>();
            foreach (var key in keyboard.allKeys)
                if (key.isPressed) pressed.Add(key.keyCode.ToString().ToLowerInvariant());
            return Device(pressed, new Dictionary<string, object>());
        }

        private static Dictionary<string, object> CaptureMouse(Mouse mouse)
        {
            if (mouse == null) return null;
            var pressed = new List<string>();
            if (mouse.leftButton.isPressed) pressed.Add("leftButton");
            if (mouse.rightButton.isPressed) pressed.Add("rightButton");
            if (mouse.middleButton.isPressed) pressed.Add("middleButton");
            if (mouse.forwardButton.isPressed) pressed.Add("forwardButton");
            if (mouse.backButton.isPressed) pressed.Add("backButton");
            Vector2 delta = mouse.delta.ReadValue();
            return Device(pressed, new Dictionary<string, object> { ["delta"] = new[] { (double)delta.x, (double)delta.y } });
        }

        private static Dictionary<string, object> CaptureGamepad(Gamepad gamepad)
        {
            if (gamepad == null) return null;
            var pressed = new List<string>();
            if (gamepad.buttonSouth.isPressed) pressed.Add("buttonSouth");
            if (gamepad.buttonNorth.isPressed) pressed.Add("buttonNorth");
            if (gamepad.buttonEast.isPressed) pressed.Add("buttonEast");
            if (gamepad.buttonWest.isPressed) pressed.Add("buttonWest");
            if (gamepad.leftShoulder.isPressed) pressed.Add("leftShoulder");
            if (gamepad.rightShoulder.isPressed) pressed.Add("rightShoulder");
            if (gamepad.leftStickButton.isPressed) pressed.Add("leftStickPress");
            if (gamepad.rightStickButton.isPressed) pressed.Add("rightStickPress");
            if (gamepad.startButton.isPressed) pressed.Add("start");
            if (gamepad.selectButton.isPressed) pressed.Add("select");
            Vector2 left = gamepad.leftStick.ReadUnprocessedValue();
            Vector2 right = gamepad.rightStick.ReadUnprocessedValue();
            Vector2 dpad = gamepad.dpad.ReadValue();
            return Device(pressed, new Dictionary<string, object>
            {
                ["leftStick"] = new[] { (double)left.x, (double)left.y },
                ["rightStick"] = new[] { (double)right.x, (double)right.y },
                ["leftTrigger"] = (double)gamepad.leftTrigger.ReadUnprocessedValue(),
                ["rightTrigger"] = (double)gamepad.rightTrigger.ReadUnprocessedValue(),
                ["dpad"] = new[] { (double)dpad.x, (double)dpad.y },
            });
        }

        private static Dictionary<string, object> Device(List<string> pressed, Dictionary<string, object> values)
        {
            return new Dictionary<string, object> { ["pressed"] = pressed, ["values"] = values };
        }
#endif
    }
}
