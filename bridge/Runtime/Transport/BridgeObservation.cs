using System;
using System.Collections.Generic;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Transport
{
    /// <summary>
    /// One cached, thread-safe sample of the project. The Unity source is updated on the main
    /// thread; the transport reads the latest snapshot from its stream timer.
    /// </summary>
    public sealed class BridgeObservationSnapshot
    {
        public string ClockMode { get; set; } = "realtime";

        public string Scene { get; set; } = "unknown";

        public bool Playing { get; set; }

        public bool Paused { get; set; }

        public double TimeScale { get; set; } = 1;

        public long Frame { get; set; }

        public double? GameS { get; set; }

        public Dictionary<string, object> Public { get; set; } = new Dictionary<string, object>();

        public Dictionary<string, object> Ui { get; set; }

        public List<ObservationReaderError> ReaderErrors { get; set; } = new List<ObservationReaderError>();

        /// <summary>When the sample was taken; the observation's wall_utc is this value.</summary>
        public DateTime CapturedWallUtc { get; set; } = DateTime.UtcNow;
    }

    /// <summary>
    /// Source of generic observations. Called from a transport thread, so implementations must
    /// return a snapshot rather than touching Unity APIs off the main thread.
    /// </summary>
    public interface IBridgeObservationSource
    {
        BridgeObservationSnapshot GetObservation();
    }
}
