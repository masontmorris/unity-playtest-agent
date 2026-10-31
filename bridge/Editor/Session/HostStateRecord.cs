// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;

namespace Playtest.Bridge.Editor.Session
{
    /// <summary>The host's own lifecycle. Wire names are fixed by the agreed mailbox.</summary>
    public enum PlayModeSessionPhase
    {
        Starting,
        Ready,
        Stopping,
        Stopped,
        Failed,
    }

    /// <summary>
    /// <c>host-state.json</c>: what the host is doing right now. A ready state carries the generation,
    /// scene and readiness instant it actually measured; an unmeasured value stays null. It carries no
    /// bridge port, token or key, so the transport authority remains the project lock file.
    /// </summary>
    public sealed class HostStateRecord
    {
        public const int CurrentVersion = 1;
        public const string FileName = "host-state.json";

        /// <summary>The only capability this host advertises to S1 in this version.</summary>
        public const string RestartCapability = "restart_play_mode";

        private static readonly string[] Keys =
        {
            "version", "run_id", "phase", "generation", "scene_path", "scene_handle", "ready_utc", "error",
            "capabilities",
        };

        public int Version { get; set; } = CurrentVersion;

        public string RunId { get; set; }

        public PlayModeSessionPhase Phase { get; set; } = PlayModeSessionPhase.Starting;

        public string Generation { get; set; }

        public string ScenePath { get; set; }

        public int? SceneHandle { get; set; }

        public DateTime? ReadyUtc { get; set; }

        public string Error { get; set; }

        public IReadOnlyList<string> Capabilities { get; set; } = new[] { RestartCapability };

        public static string WireName(PlayModeSessionPhase phase)
        {
            switch (phase)
            {
                case PlayModeSessionPhase.Starting: return "starting";
                case PlayModeSessionPhase.Ready: return "ready";
                case PlayModeSessionPhase.Stopping: return "stopping";
                case PlayModeSessionPhase.Stopped: return "stopped";
                case PlayModeSessionPhase.Failed: return "failed";
                default: throw new ArgumentOutOfRangeException(nameof(phase), phase, "unknown host phase");
            }
        }

        public static PlayModeSessionPhase ParsePhase(string value)
        {
            switch (value)
            {
                case "starting": return PlayModeSessionPhase.Starting;
                case "ready": return PlayModeSessionPhase.Ready;
                case "stopping": return PlayModeSessionPhase.Stopping;
                case "stopped": return PlayModeSessionPhase.Stopped;
                case "failed": return PlayModeSessionPhase.Failed;
                default: throw new SessionRecordException("host-state.json.phase is not a known phase: '" + value + "'");
            }
        }

        public static HostStateRecord Parse(string json)
        {
            JObject obj = SessionRecordJson.ParseObject(json, "host-state.json");
            SessionRecordJson.RequireExactKeys(obj, "host-state.json", Keys);
            SessionRecordJson.RequireVersion(obj, "host-state.json");
            IReadOnlyList<string> capabilities = SessionRecordJson.RequireStringArray(
                obj, "capabilities", "host-state.json");
            var state = new HostStateRecord
            {
                RunId = SessionRecordJson.RequireString(obj, "run_id", "host-state.json"),
                Phase = ParsePhase(SessionRecordJson.RequireString(obj, "phase", "host-state.json")),
                Generation = SessionRecordJson.OptionalUuid(obj, "generation", "host-state.json"),
                ScenePath = SessionRecordJson.OptionalString(obj, "scene_path", "host-state.json"),
                SceneHandle = SessionRecordJson.OptionalInteger(obj, "scene_handle", "host-state.json"),
                ReadyUtc = SessionRecordJson.OptionalTimestampUtc(obj, "ready_utc", "host-state.json"),
                Error = SessionRecordJson.OptionalString(obj, "error", "host-state.json"),
                Capabilities = capabilities,
            };
            if (state.Phase == PlayModeSessionPhase.Ready)
            {
                if (state.Generation == null || state.ScenePath == null || state.SceneHandle == null
                    || state.ReadyUtc == null)
                {
                    throw new SessionRecordException(
                        "host-state.json is ready without a measured generation, scene handle, scene path and ready time");
                }

                if (state.Error != null)
                {
                    throw new SessionRecordException("host-state.json is ready and carries an error");
                }
            }

            if (state.Phase == PlayModeSessionPhase.Failed && string.IsNullOrWhiteSpace(state.Error))
            {
                throw new SessionRecordException("host-state.json is failed without an error");
            }

            return state;
        }

        public string ToJson()
        {
            var capabilities = new JArray();
            if (Capabilities != null)
            {
                foreach (string capability in Capabilities)
                {
                    capabilities.Add(capability);
                }
            }

            // Every key is written, with null for a value that was never measured.
            return SessionRecordJson.Serialize(new JObject
            {
                ["version"] = CurrentVersion,
                ["run_id"] = RunId,
                ["phase"] = WireName(Phase),
                ["generation"] = SessionRecordJson.Nullable(Generation),
                ["scene_path"] = SessionRecordJson.Nullable(ScenePath),
                ["scene_handle"] = SessionRecordJson.Nullable(SceneHandle),
                ["ready_utc"] = SessionRecordJson.Nullable(ReadyUtc),
                ["error"] = SessionRecordJson.Nullable(Error),
                ["capabilities"] = capabilities,
            });
        }
    }
}
