// SPDX-License-Identifier: Apache-2.0
using System;
using Newtonsoft.Json;

namespace Playtest.Bridge.Protocol
{
    /// <summary>Fields every wire message carries (build bible 5).</summary>
    public abstract class Envelope
    {
        protected Envelope(string type)
        {
            Type = type;
        }

        [JsonProperty("protocol", Order = 0)]
        public string Protocol { get; set; } = ProtocolVersion.Current;

        [JsonProperty("session", Order = 1)]
        public string Session { get; set; }

        [JsonProperty("seq", Order = 2)]
        public long Seq { get; set; }

        [JsonProperty("game_s", Order = 3)]
        public double? GameS { get; set; }

        [JsonProperty("wall_utc", Order = 4)]
        public DateTime WallUtc { get; set; } = DateTime.UtcNow;

        [JsonProperty("type", Order = 5)]
        public string Type { get; protected set; }
    }
}
