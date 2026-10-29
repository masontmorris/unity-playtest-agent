// SPDX-License-Identifier: Apache-2.0
using System.Collections.Generic;
using Newtonsoft.Json;

namespace Playtest.Bridge.Protocol
{
    /// <summary>Core -> Editor. Ask for one inspection stage; never valid outside the Editor.</summary>
    public sealed class InspectRequestMessage : Envelope
    {
        public InspectRequestMessage() : base("inspect")
        {
        }

        [JsonProperty("stage", Order = 10)]
        public string Stage { get; set; }
    }

    /// <summary>
    /// How completely one collection was sampled. A null <see cref="Observed"/> means the total
    /// was not counted; it is never inferred from <see cref="Returned"/>.
    /// </summary>
    public sealed class InspectionCap
    {
        [JsonProperty("limit", Order = 0)]
        public long Limit { get; set; }

        [JsonProperty("observed", Order = 1, NullValueHandling = NullValueHandling.Include)]
        public long? Observed { get; set; }

        [JsonProperty("returned", Order = 2)]
        public long Returned { get; set; }

        [JsonProperty("truncated", Order = 3, NullValueHandling = NullValueHandling.Include)]
        public bool? Truncated { get; set; }
    }

    /// <summary>
    /// Editor -> core. One inspection snapshot. <see cref="Data"/> is open because each stage
    /// carries reflection descriptors the contract does not fix; <see cref="Caps"/> is mandatory
    /// even when empty.
    /// </summary>
    public sealed class InspectionResultMessage : Envelope
    {
        public InspectionResultMessage() : base("inspection")
        {
        }

        [JsonProperty("stage", Order = 10)]
        public string Stage { get; set; }

        [JsonProperty("in_reply_to", Order = 11)]
        public long InReplyTo { get; set; }

        [JsonProperty("data", Order = 12)]
        public Dictionary<string, object> Data { get; set; } = new Dictionary<string, object>();

        [JsonProperty("caps", Order = 13)]
        public Dictionary<string, InspectionCap> Caps { get; set; } = new Dictionary<string, InspectionCap>();

        [JsonProperty("errors", Order = 14)]
        public List<string> Errors { get; set; } = new List<string>();
    }
}
