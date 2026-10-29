// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using Newtonsoft.Json;

namespace Playtest.Bridge.Protocol
{
    /// <summary>Core -> bridge. Always the first message on a connection.</summary>
    public sealed class HelloMessage : Envelope
    {
        public HelloMessage() : base("hello")
        {
        }

        [JsonProperty("token", Order = 10)]
        public string Token { get; set; }

        [JsonProperty("core_version", Order = 11)]
        public string CoreVersion { get; set; }
    }

    /// <summary>Project lock state reported in <see cref="StatusMessage"/>.</summary>
    public sealed class LockState
    {
        [JsonProperty("held", Order = 0)]
        public bool Held { get; set; }

        [JsonProperty("owner_pid", Order = 1)]
        public int? OwnerPid { get; set; }

        [JsonProperty("port", Order = 2)]
        public int? Port { get; set; }
    }

    /// <summary>Bridge -> core. The successful reply to <see cref="HelloMessage"/>.</summary>
    public sealed class StatusMessage : Envelope
    {
        public StatusMessage() : base("status")
        {
        }

        [JsonProperty("bridge_version", Order = 10)]
        public string BridgeVersion { get; set; }

        [JsonProperty("unity_version", Order = 11)]
        public string UnityVersion { get; set; }

        [JsonProperty("project", Order = 12)]
        public string Project { get; set; }

        [JsonProperty("input_backends", Order = 13)]
        public List<string> InputBackends { get; set; } = new List<string>();

        [JsonProperty("clock_control", Order = 14)]
        public bool ClockControl { get; set; }

        [JsonProperty("capture", Order = 15)]
        public bool Capture { get; set; }

        [JsonProperty("capture_self_test", Order = 16)]
        public string CaptureSelfTest { get; set; } = "unknown";

        [JsonProperty("lock", Order = 17)]
        public LockState Lock { get; set; } = new LockState();
    }

    /// <summary>Bridge -> core error. <see cref="InReplyTo"/> echoes the request seq when known.</summary>
    public sealed class BridgeErrorMessage : Envelope
    {
        public BridgeErrorMessage() : base("error")
        {
        }

        [JsonProperty("code", Order = 10)]
        public string Code { get; set; }

        [JsonProperty("message", Order = 11)]
        public string Message { get; set; }

        [JsonProperty("in_reply_to", Order = 12)]
        public long? InReplyTo { get; set; }
    }
}
