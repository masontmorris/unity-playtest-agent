// SPDX-License-Identifier: Apache-2.0
using System.Collections.Generic;
using Newtonsoft.Json;

namespace Playtest.Bridge.Protocol
{
    /// <summary>Core -> bridge. Stop now, release every input and verify the release.</summary>
    public sealed class StopRequestMessage : Envelope
    {
        public StopRequestMessage() : base("stop")
        {
        }

        [JsonProperty("reason", Order = 10)]
        public string Reason { get; set; }
    }

    /// <summary>Bridge -> core. The inputs released and whether their release was verified.</summary>
    public sealed class StopResultMessage : Envelope
    {
        public StopResultMessage() : base("stopped")
        {
        }

        [JsonProperty("released", Order = 10)]
        public List<string> Released { get; set; } = new List<string>();

        [JsonProperty("release_verified", Order = 11)]
        public bool ReleaseVerified { get; set; }
    }

    public static class BridgeStopReason
    {
        public const string User = "user";
        public const string Reset = "reset";
        public const string SceneChange = "scene_change";
        public const string Disconnect = "disconnect";
        public const string Error = "error";
        public const string Finish = "finish";

        public static readonly IReadOnlyList<string> All = new[]
        {
            User, Reset, SceneChange, Disconnect, Error, Finish,
        };
    }

    /// <summary>Core -> bridge. Pause, resume, step the game clock, or set its scale.</summary>
    public sealed class ClockRequestMessage : Envelope
    {
        public ClockRequestMessage() : base("clock")
        {
        }

        [JsonProperty("op", Order = 10)]
        public string Op { get; set; }

        [JsonProperty("step_game_s", Order = 11)]
        public double? StepGameS { get; set; }

        [JsonProperty("time_scale", Order = 12)]
        public double? TimeScale { get; set; }
    }

    public static class BridgeClockOps
    {
        public const string Pause = "pause";
        public const string Resume = "resume";
        public const string Step = "step";
        public const string SetScale = "set_scale";

        public static readonly IReadOnlyList<string> All = new[]
        {
            Pause, Resume, Step, SetScale,
        };
    }
}
