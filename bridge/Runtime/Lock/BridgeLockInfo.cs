using System;
using Newtonsoft.Json;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Lock
{
    /// <summary>
    /// Contents of the project lock file. The file format is bridge-local JSON
    /// (not a wire contract): protocol, pid, port, token, started_utc.
    /// </summary>
    public sealed class BridgeLockInfo
    {
        [JsonProperty("protocol")]
        public string Protocol { get; set; } = ProtocolVersion.Current;

        [JsonProperty("pid")]
        public int Pid { get; set; }

        [JsonProperty("port")]
        public int Port { get; set; }

        [JsonProperty("token")]
        public string Token { get; set; }

        [JsonProperty("started_utc")]
        public DateTime StartedUtc { get; set; }
    }
}
