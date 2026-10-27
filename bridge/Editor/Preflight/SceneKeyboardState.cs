using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.InputSystem.LowLevel;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>Captures actual keyboard state in game input updates, not the Editor input buffer.</summary>
    public static class SceneKeyboardState
    {
        private const double MinimumIntervalS = 0.02;
        private static readonly object Gate = new object();
        private static bool _active;
        private static List<string> _keys;
        private static double? _realS;
        private static double? _wallS;
        private static string _updateType;
        private static string _error;

        public static void Start()
        {
            lock (Gate)
            {
                if (_active) throw new InvalidOperationException("keyboard observation is already active");
                _keys = null; _realS = null; _wallS = null; _updateType = null; _error = null;
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
                _active = false; _keys = null; _realS = null; _wallS = null; _updateType = null; _error = null;
            }
        }

        public static Dictionary<string, object> Snapshot()
        {
            lock (Gate)
                return new Dictionary<string, object>
                {
                    ["active"] = _active,
                    ["keys"] = _keys == null ? null : new List<string>(_keys),
                    ["real_time_s"] = _realS,
                    ["wall_s"] = _wallS,
                    ["update_type"] = _updateType,
                    ["error"] = _error,
                };
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
                    Keyboard keyboard = Keyboard.current;
                    List<string> pressed = null;
                    if (keyboard != null)
                    {
                        pressed = new List<string>();
                        foreach (var key in keyboard.allKeys)
                            if (key.isPressed) pressed.Add(key.keyCode.ToString().ToLowerInvariant());
                    }
                    _keys = pressed;
                    _realS = now;
                    _wallS = (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
                    _updateType = type.ToString().ToLowerInvariant();
                    _error = null;
                }
                catch (Exception ex)
                {
                    _keys = null; _realS = null; _wallS = null; _updateType = null;
                    _error = ex.GetType().Name + ": " + ex.Message;
                }
            }
        }
#endif
    }
}
