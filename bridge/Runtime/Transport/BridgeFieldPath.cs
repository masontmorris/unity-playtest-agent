using System;
using System.Collections;
using System.Collections.Generic;

namespace Playtest.Bridge.Transport
{
    /// <summary>
    /// Resolves the dotted field paths a manifest action declares in <c>depends_on</c>, for
    /// example <c>public.r_entity.pos</c> or <c>ui.canvases</c>. The paths refer to the observation
    /// contract, not to a game. A missing path is unresolved; the caller must fail closed.
    /// </summary>
    public static class BridgeFieldPath
    {
        public static bool TryResolve(BridgeObservationSnapshot snapshot, string path, out object value)
        {
            value = null;
            if (snapshot == null || string.IsNullOrEmpty(path))
            {
                return false;
            }

            string[] parts = path.Split('.');
            object current;
            int index;

            if (string.Equals(parts[0], "public", StringComparison.Ordinal))
            {
                current = snapshot.Public;
                index = 1;
            }
            else if (string.Equals(parts[0], "ui", StringComparison.Ordinal))
            {
                current = snapshot.Ui;
                index = 1;
            }
            else
            {
                if (!TryReadTopLevel(snapshot, parts[0], out current))
                {
                    return false;
                }

                index = 1;
            }

            for (; index < parts.Length; index++)
            {
                if (!TryReadMember(current, parts[index], out current))
                {
                    return false;
                }
            }

            value = current;
            return true;
        }

        private static bool TryReadTopLevel(BridgeObservationSnapshot snapshot, string name, out object value)
        {
            switch (name)
            {
                case "clock_mode":
                    value = snapshot.ClockMode;
                    return true;
                case "scene":
                    value = snapshot.Scene;
                    return true;
                case "playing":
                    value = snapshot.Playing;
                    return true;
                case "paused":
                    value = snapshot.Paused;
                    return true;
                case "time_scale":
                    value = snapshot.TimeScale;
                    return true;
                case "frame":
                    value = snapshot.Frame;
                    return true;
                case "game_s":
                    value = snapshot.GameS;
                    return true;
                case "public":
                    value = snapshot.Public;
                    return true;
                case "ui":
                    value = snapshot.Ui;
                    return true;
                case "frame_ref":
                    value = null;
                    return false;
                default:
                    value = null;
                    return false;
            }
        }

        private static bool TryReadMember(object current, string name, out object value)
        {
            value = null;
            if (current == null)
            {
                return false;
            }

            if (current is IDictionary<string, object> dictionary)
            {
                return dictionary.TryGetValue(name, out value);
            }

            if (current is IDictionary legacy)
            {
                if (!legacy.Contains(name))
                {
                    return false;
                }

                value = legacy[name];
                return true;
            }

            if (int.TryParse(name, out int index))
            {
                if (current is IList list)
                {
                    if (index < 0 || index >= list.Count)
                    {
                        return false;
                    }

                    value = list[index];
                    return true;
                }

                if (current is Array array)
                {
                    if (index < 0 || index >= array.Length)
                    {
                        return false;
                    }

                    value = array.GetValue(index);
                    return true;
                }
            }

            return false;
        }
    }
}
