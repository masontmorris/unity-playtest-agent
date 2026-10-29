// SPDX-License-Identifier: Apache-2.0
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Clock
{
    /// <summary>
    /// Applies the clock verb. Implementations may queue the work to the Unity main thread;
    /// the transport acknowledges with a status and the next observation reports the effect.
    /// </summary>
    public interface IBridgeClock
    {
        string ClockMode { get; }

        bool Paused { get; }

        double TimeScale { get; }

        bool TryApply(ClockRequestMessage request, out string error);

        /// <summary>Return to realtime and restore time scale, e.g. on stop.</summary>
        void Reset();
    }
}
