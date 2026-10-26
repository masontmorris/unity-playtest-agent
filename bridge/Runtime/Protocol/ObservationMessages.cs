using System;
using System.Collections.Generic;
using Newtonsoft.Json;

namespace Playtest.Bridge.Protocol
{
    /// <summary>Core -> bridge. Subscribe to a stream, ask for one sample, or stop streaming.</summary>
    public sealed class ObserveRequestMessage : Envelope
    {
        public ObserveRequestMessage() : base("observe")
        {
        }

        [JsonProperty("mode", Order = 10)]
        public string Mode { get; set; }

        [JsonProperty("with_frame", Order = 11)]
        public bool WithFrame { get; set; }

        [JsonProperty("rate_hz", Order = 12)]
        public double? RateHz { get; set; }
    }

    /// <summary>One missing or failed reader. A missing value is never invented.</summary>
    public sealed class ObservationReaderError
    {
        [JsonProperty("reader", Order = 0)]
        public string Reader { get; set; }

        [JsonProperty("error", Order = 1)]
        public string Error { get; set; }
    }

    /// <summary>Bridge -> core. One generic sample of the project.</summary>
    public sealed class ObservationMessage : Envelope
    {
        public ObservationMessage() : base("observation")
        {
        }

        [JsonProperty("clock_mode", Order = 10)]
        public string ClockMode { get; set; } = "realtime";

        [JsonProperty("scene", Order = 11)]
        public string Scene { get; set; }

        [JsonProperty("playing", Order = 12)]
        public bool Playing { get; set; }

        [JsonProperty("paused", Order = 13)]
        public bool Paused { get; set; }

        [JsonProperty("time_scale", Order = 14)]
        public double TimeScale { get; set; } = 1;

        [JsonProperty("frame", Order = 15)]
        public long Frame { get; set; }

        [JsonProperty("public", Order = 16)]
        public Dictionary<string, object> Public { get; set; } = new Dictionary<string, object>();

        [JsonProperty("ui", Order = 17)]
        public Dictionary<string, object> Ui { get; set; }

        [JsonProperty("frame_ref", Order = 18)]
        public string FrameRef { get; set; }

        [JsonProperty("reader_errors", Order = 19)]
        public List<ObservationReaderError> ReaderErrors { get; set; } = new List<ObservationReaderError>();
    }
}
