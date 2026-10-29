// SPDX-License-Identifier: Apache-2.0
using System.Collections.Generic;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>
    /// The keyboard-only view of <see cref="SceneInputState"/>, kept so existing callers and the
    /// <c>keyboard_state</c> field of a fields sample keep their shape: <c>keys</c> lists the keys
    /// held in the last game input update. Starting and stopping it starts and stops the generic
    /// capture, so the two views always describe the same update.
    /// </summary>
    public static class SceneKeyboardState
    {
        public static void Start()
        {
            SceneInputState.Start();
        }

        public static void Stop()
        {
            SceneInputState.Stop();
        }

        public static Dictionary<string, object> Snapshot()
        {
            return SceneInputState.KeyboardSnapshot();
        }
    }
}
