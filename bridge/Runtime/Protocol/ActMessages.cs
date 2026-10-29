using System;
using System.Collections.Generic;
using Newtonsoft.Json;

namespace Playtest.Bridge.Protocol
{
    /// <summary>Core -> bridge. One bounded action, bound to the observation it was built from.</summary>
    public sealed class ActRequestMessage : Envelope
    {
        public ActRequestMessage() : base("act")
        {
        }

        [JsonProperty("action_id", Order = 10)]
        public string ActionId { get; set; }

        [JsonProperty("choice_id", Order = 11)]
        public string ChoiceId { get; set; }

        [JsonProperty("action", Order = 12)]
        public string Action { get; set; }

        [JsonProperty("params", Order = 13)]
        public Dictionary<string, object> Params { get; set; } = new Dictionary<string, object>();

        [JsonProperty("based_on_seq", Order = 14)]
        public long BasedOnSeq { get; set; }

        [JsonProperty("max_age_s", Order = 15)]
        public double MaxAgeS { get; set; }

        [JsonProperty("depends_on", Order = 16)]
        public List<string> DependsOn { get; set; } = new List<string>();

        [JsonProperty("lease_s", Order = 17)]
        public double? LeaseS { get; set; }
    }

    /// <summary>A measured or calibrated effect on one field. Unknown values stay null.</summary>
    public sealed class BridgeEffect
    {
        [JsonProperty("field", Order = 0)]
        public string Field { get; set; }

        [JsonProperty("delta", Order = 1)]
        public double? Delta { get; set; }

        [JsonProperty("spread", Order = 2)]
        public double? Spread { get; set; }

        [JsonProperty("unit", Order = 3)]
        public string Unit { get; set; }
    }

    /// <summary>
    /// Measured evidence for one input an action or skill step applied. <c>x</c> and <c>y</c> are the
    /// declared values. The times and the duration come from device-state reads: from the read that
    /// confirmed the input applied to the read that showed it neutral. A mouse delta is an impulse,
    /// so its duration stays null and <c>applied_delta_x/y</c> carry the pixels the device read back.
    /// Anything the bridge could not measure is null, never zero. Every field is written, so a null
    /// is explicit.
    /// </summary>
    public sealed class AppliedInputReceipt
    {
        [JsonProperty("step_index", Order = 0)]
        public int StepIndex { get; set; }

        [JsonProperty("device", Order = 1)]
        public string Device { get; set; }

        [JsonProperty("control", Order = 2)]
        public string Control { get; set; }

        [JsonProperty("x", Order = 3, NullValueHandling = NullValueHandling.Include)]
        public double? X { get; set; }

        [JsonProperty("y", Order = 4, NullValueHandling = NullValueHandling.Include)]
        public double? Y { get; set; }

        [JsonProperty("input_start_wall_s", Order = 5, NullValueHandling = NullValueHandling.Include)]
        public double? InputStartWallS { get; set; }

        [JsonProperty("input_end_wall_s", Order = 6, NullValueHandling = NullValueHandling.Include)]
        public double? InputEndWallS { get; set; }

        [JsonProperty("applied_duration_s", Order = 7, NullValueHandling = NullValueHandling.Include)]
        public double? AppliedDurationS { get; set; }

        [JsonProperty("applied_delta_x", Order = 8, NullValueHandling = NullValueHandling.Include)]
        public double? AppliedDeltaX { get; set; }

        [JsonProperty("applied_delta_y", Order = 9, NullValueHandling = NullValueHandling.Include)]
        public double? AppliedDeltaY { get; set; }

        [JsonProperty("release_verified", Order = 10, NullValueHandling = NullValueHandling.Include)]
        public bool? ReleaseVerified { get; set; }
    }

    /// <summary>Bridge -> core. The lifecycle and measured timing of one action.</summary>
    public sealed class ExecutionRecordMessage : Envelope
    {
        public ExecutionRecordMessage() : base("execution")
        {
        }

        [JsonProperty("action_id", Order = 10)]
        public string ActionId { get; set; }

        [JsonProperty("status", Order = 11)]
        public string Status { get; set; }

        [JsonProperty("validated_against_seq", Order = 12)]
        public long? ValidatedAgainstSeq { get; set; }

        [JsonProperty("reject_reason", Order = 13)]
        public string RejectReason { get; set; }

        [JsonProperty("input_start_game_s", Order = 14)]
        public double? InputStartGameS { get; set; }

        [JsonProperty("input_end_game_s", Order = 15)]
        public double? InputEndGameS { get; set; }

        [JsonProperty("input_start_wall_s", Order = 16)]
        public double? InputStartWallS { get; set; }

        [JsonProperty("input_end_wall_s", Order = 17)]
        public double? InputEndWallS { get; set; }

        [JsonProperty("applied_duration_s", Order = 18)]
        public double? AppliedDurationS { get; set; }

        [JsonProperty("release_verified", Order = 19)]
        public bool? ReleaseVerified { get; set; }

        [JsonProperty("expected_effect", Order = 20)]
        public List<BridgeEffect> ExpectedEffect { get; set; } = new List<BridgeEffect>();

        [JsonProperty("observed_effect", Order = 21)]
        public List<BridgeEffect> ObservedEffect { get; set; } = new List<BridgeEffect>();

        /// <summary>One receipt per input applied, in application order. Empty when nothing was applied.</summary>
        [JsonProperty("applied_inputs", Order = 22)]
        public List<AppliedInputReceipt> AppliedInputs { get; set; } = new List<AppliedInputReceipt>();
    }

    public static class BridgeExecStatus
    {
        public const string Requested = "requested";
        public const string Validated = "validated";
        public const string Running = "running";
        public const string Completed = "completed";
        public const string Failed = "failed";
        public const string Cancelled = "cancelled";
        public const string TimedOut = "timed_out";
        public const string Rejected = "rejected";

        public static readonly IReadOnlyList<string> All = new[]
        {
            Requested, Validated, Running, Completed, Failed, Cancelled, TimedOut, Rejected,
        };

        public static readonly IReadOnlyList<string> Terminal = new[]
        {
            Completed, Failed, Cancelled, TimedOut, Rejected,
        };
    }

    public static class BridgeRejectReason
    {
        public const string StaleAge = "stale_age";
        public const string StaleFields = "stale_fields";
        public const string UnknownAction = "unknown_action";
        public const string InvalidParams = "invalid_params";
        public const string NotPlaying = "not_playing";
        public const string Busy = "busy";

        public static readonly IReadOnlyList<string> All = new[]
        {
            StaleAge, StaleFields, UnknownAction, InvalidParams, NotPlaying, Busy,
        };
    }
}
