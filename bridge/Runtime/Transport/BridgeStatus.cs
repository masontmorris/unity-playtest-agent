using System;
using System.Collections.Generic;

namespace Playtest.Bridge.Transport
{
    /// <summary>
    /// Everything the bridge can report without a running game. The source is read from the
    /// transport thread, so implementations must be safe to call off the Unity main thread.
    /// The Editor implementation snapshots these values when the bridge starts.
    /// </summary>
    public sealed class BridgeStatusSnapshot
    {
        public string BridgeVersion { get; set; } = "0.1.0";

        public string UnityVersion { get; set; } = "unknown";

        public string Project { get; set; } = "unknown";

        public IReadOnlyList<string> InputBackends { get; set; } = Array.Empty<string>();

        public bool ClockControl { get; set; }

        public bool Capture { get; set; }

        public string CaptureSelfTest { get; set; } = "unknown";

        public int OwnerPid { get; set; }
    }

    public interface IBridgeStatusSource
    {
        BridgeStatusSnapshot GetStatus();
    }

    public sealed class BridgeServerOptions
    {
        public System.Net.IPAddress BindAddress { get; set; } = System.Net.IPAddress.Loopback;

        /// <summary>0 asks the OS for an ephemeral loopback port.</summary>
        public int Port { get; set; }

        public int MaxLineBytes { get; set; } = 4 * 1024 * 1024;

        public int HandshakeTimeoutMs { get; set; } = 5000;

        /// <summary>0 blocks until the peer sends data or disconnects.</summary>
        public int ReadTimeoutMs { get; set; }
    }
}
