using System;
using System.Collections.Generic;
using UnityEngine;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>Bounded, opt-in capture of runtime error logs during an active preflight probe.</summary>
    public static class SceneRuntimeErrors
    {
        private const int Limit = 256;
        private const int TextLimit = 2048;
        private static readonly object Gate = new object();
        private static readonly List<Dictionary<string, object>> Events = new List<Dictionary<string, object>>();
        private static bool _active;
        private static long _observed;
        private static bool _truncated;

        public static void Start()
        {
            lock (Gate)
            {
                if (_active) throw new InvalidOperationException("runtime error capture is already active");
                Events.Clear();
                _observed = 0;
                _truncated = false;
                _active = true;
                Application.logMessageReceivedThreaded += OnLog;
            }
        }

        public static void Stop()
        {
            lock (Gate)
            {
                if (!_active) return;
                Application.logMessageReceivedThreaded -= OnLog;
                _active = false;
            }
        }

        public static Dictionary<string, object> Snapshot()
        {
            lock (Gate)
            {
                var copy = new List<object>();
                if (_active)
                    foreach (var item in Events) copy.Add(new Dictionary<string, object>(item));
                return new Dictionary<string, object>
                {
                    ["active"] = _active,
                    ["observed"] = _active ? (long?)_observed : null,
                    ["limit"] = Limit,
                    ["truncated"] = _active ? (bool?)_truncated : null,
                    ["events"] = copy,
                };
            }
        }

        private static void OnLog(string message, string stack, LogType type)
        {
            if (type != LogType.Error && type != LogType.Exception && type != LogType.Assert) return;
            lock (Gate)
            {
                if (!_active) return;
                _observed++;
                string text = Trim(message);
                string trace = Trim(stack);
                double now = (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
                if (Events.Count > 0)
                {
                    var last = Events[Events.Count - 1];
                    if (Equals(last["type"], type.ToString()) && Equals(last["message"], text) && Equals(last["stack"], trace))
                    {
                        last["last_sequence"] = _observed;
                        last["last_wall_s"] = now;
                        last["count"] = (long)last["count"] + 1;
                        return;
                    }
                }
                if (Events.Count >= Limit) { _truncated = true; return; }
                Events.Add(new Dictionary<string, object>
                {
                    ["type"] = type.ToString(), ["message"] = text, ["stack"] = trace,
                    ["first_sequence"] = _observed, ["last_sequence"] = _observed,
                    ["first_wall_s"] = now, ["last_wall_s"] = now, ["count"] = 1L,
                });
            }
        }

        private static string Trim(string value)
        {
            if (value == null || value.Length <= TextLimit) return value;
            return value.Substring(0, TextLimit);
        }
    }
}
