// SPDX-License-Identifier: Apache-2.0
using System;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Scope in which a game's logged errors, exceptions and assertions are evidence rather than test
    /// failures. Calibration probes input the game was not written for, so the game may log; the
    /// bridge's <c>SceneRuntimeErrors</c> counts every such message and the calibration client marks
    /// the trial invalid. The Test Framework must not turn the same message into a failed test.
    /// </summary>
    internal sealed class CalibrationLogTolerance : IDisposable
    {
        public CalibrationLogTolerance()
        {
            LogAssert.ignoreFailingMessages = true;
        }

        public void Dispose()
        {
            LogAssert.ignoreFailingMessages = false;
        }
    }
}
