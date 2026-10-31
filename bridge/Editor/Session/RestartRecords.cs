// SPDX-License-Identifier: Apache-2.0
using System;
using Newtonsoft.Json.Linq;

namespace Playtest.Bridge.Editor.Session
{
    /// <summary>
    /// <c>request.json</c>: one explicit S1-selected restart. The host never writes this file and never
    /// invents a request from a fall, timer, threshold or anomaly; it only validates and executes one.
    /// </summary>
    public sealed class RestartRequestRecord
    {
        public const int CurrentVersion = 1;
        public const string FileName = "request.json";
        public const string RestartOperation = "restart_play_mode";

        private static readonly string[] Keys =
        {
            "version", "run_id", "ticket_id", "operation", "from_generation", "deadline_utc",
        };

        public string RunId { get; set; }

        public string TicketId { get; set; }

        public string Operation { get; set; } = RestartOperation;

        public string FromGeneration { get; set; }

        public DateTime DeadlineUtc { get; set; }

        public static RestartRequestRecord Parse(string json)
        {
            JObject obj = SessionRecordJson.ParseObject(json, "request.json");
            SessionRecordJson.RequireExactKeys(obj, "request.json", Keys);
            SessionRecordJson.RequireVersion(obj, "request.json");
            var request = new RestartRequestRecord
            {
                RunId = SessionRecordJson.RequireString(obj, "run_id", "request.json"),
                TicketId = SessionRecordJson.RequireString(obj, "ticket_id", "request.json"),
                Operation = SessionRecordJson.RequireString(obj, "operation", "request.json"),
                FromGeneration = SessionRecordJson.RequireUuid(obj, "from_generation", "request.json"),
                DeadlineUtc = SessionRecordJson.RequireTimestampUtc(obj, "deadline_utc", "request.json"),
            };
            if (request.Operation != RestartOperation)
            {
                throw new SessionRecordException(
                    "request.json.operation is '" + request.Operation + "', not '" + RestartOperation + "'");
            }

            return request;
        }

        public static bool TryParse(string json, out RestartRequestRecord request, out string error)
        {
            try
            {
                request = Parse(json);
                error = null;
                return true;
            }
            catch (SessionRecordException ex)
            {
                request = null;
                error = ex.Message;
                return false;
            }
        }
    }

    /// <summary>
    /// <c>cancel.json</c>: the client abandons one ticket. It cannot arrive as a late "success"; a
    /// cancelled ticket is terminal and no later play-mode transition may publish ready for it.
    /// </summary>
    public sealed class RestartCancelRecord
    {
        public const int CurrentVersion = 1;
        public const string FileName = "cancel.json";
        public const string CancelOperation = "cancel_restart";

        private static readonly string[] Keys = { "version", "run_id", "ticket_id", "operation" };

        public string RunId { get; set; }

        public string TicketId { get; set; }

        public string Operation { get; set; } = CancelOperation;

        public static RestartCancelRecord Parse(string json)
        {
            JObject obj = SessionRecordJson.ParseObject(json, "cancel.json");
            SessionRecordJson.RequireExactKeys(obj, "cancel.json", Keys);
            SessionRecordJson.RequireVersion(obj, "cancel.json");
            var cancel = new RestartCancelRecord
            {
                RunId = SessionRecordJson.RequireString(obj, "run_id", "cancel.json"),
                TicketId = SessionRecordJson.RequireString(obj, "ticket_id", "cancel.json"),
                Operation = SessionRecordJson.RequireString(obj, "operation", "cancel.json"),
            };
            if (cancel.Operation != CancelOperation)
            {
                throw new SessionRecordException(
                    "cancel.json.operation is '" + cancel.Operation + "', not '" + CancelOperation + "'");
            }

            return cancel;
        }
    }

    /// <summary>The wire phases of <c>result.json</c>. The set is closed.</summary>
    public enum RestartPhase
    {
        Accepted,
        Stopped,
        Starting,
        Ready,
        Failed,
        Cancelled,
    }

    /// <summary>
    /// <c>result.json</c>: exactly one ticket's latest state. Ready is only publishable from observed
    /// values: both play-mode transitions for this ticket, a loaded and active scene, and a distinct
    /// fresh generation after the new bridge started. Anything not measured stays null.
    /// </summary>
    public sealed class RestartResultRecord
    {
        public const int CurrentVersion = 1;
        public const string FileName = "result.json";

        private static readonly string[] Keys =
        {
            "version", "run_id", "ticket_id", "phase", "from_generation", "to_generation",
            "entered_edit_mode_utc", "entered_play_mode_utc", "ready_utc", "scene_path", "scene_handle",
            "failure_reason",
        };

        public int Version { get; set; } = CurrentVersion;

        public string RunId { get; set; }

        public string TicketId { get; set; }

        public RestartPhase Phase { get; set; } = RestartPhase.Accepted;

        public string FromGeneration { get; set; }

        public string ToGeneration { get; set; }

        public DateTime? EnteredEditModeUtc { get; set; }

        public DateTime? EnteredPlayModeUtc { get; set; }

        public DateTime? ReadyUtc { get; set; }

        public string ScenePath { get; set; }

        public int? SceneHandle { get; set; }

        public string FailureReason { get; set; }

        public static string WireName(RestartPhase phase)
        {
            switch (phase)
            {
                case RestartPhase.Accepted: return "accepted";
                case RestartPhase.Stopped: return "stopped";
                case RestartPhase.Starting: return "starting";
                case RestartPhase.Ready: return "ready";
                case RestartPhase.Failed: return "failed";
                case RestartPhase.Cancelled: return "cancelled";
                default: throw new ArgumentOutOfRangeException(nameof(phase), phase, "unknown result phase");
            }
        }

        public static RestartPhase ParsePhase(string value)
        {
            switch (value)
            {
                case "accepted": return RestartPhase.Accepted;
                case "stopped": return RestartPhase.Stopped;
                case "starting": return RestartPhase.Starting;
                case "ready": return RestartPhase.Ready;
                case "failed": return RestartPhase.Failed;
                case "cancelled": return RestartPhase.Cancelled;
                default:
                    throw new SessionRecordException("result.json.phase is not a known phase: '" + value + "'");
            }
        }

        /// <summary>
        /// The one place the mailbox's cross-field rules live. The exported schema cannot express
        /// them, so the host refuses to write and refuses to read a record that breaks them.
        /// </summary>
        public void Validate()
        {
            if (!Playtest.Bridge.Transport.BridgeGeneration.IsCanonical(FromGeneration))
            {
                throw new SessionRecordException(
                    "result.json.from_generation must be a canonical lower-case hyphenated UUID");
            }

            if (string.IsNullOrWhiteSpace(RunId))
            {
                throw new SessionRecordException("result.json.run_id must not be empty");
            }

            if (string.IsNullOrWhiteSpace(TicketId))
            {
                throw new SessionRecordException("result.json.ticket_id must not be empty");
            }

            if (ToGeneration != null && !Playtest.Bridge.Transport.BridgeGeneration.IsCanonical(ToGeneration))
            {
                throw new SessionRecordException(
                    "result.json.to_generation must be a canonical lower-case hyphenated UUID or null");
            }

            switch (Phase)
            {
                case RestartPhase.Ready:
                    RequireReady();
                    break;
                case RestartPhase.Failed:
                case RestartPhase.Cancelled:
                    if (string.IsNullOrWhiteSpace(FailureReason))
                    {
                        throw new SessionRecordException(
                            "result.json is " + WireName(Phase) + " without a failure_reason");
                    }

                    if (ToGeneration != null)
                    {
                        throw new SessionRecordException(
                            "result.json is " + WireName(Phase) + " but carries a to_generation; only ready does");
                    }

                    break;
                default:
                    if (FailureReason != null)
                    {
                        throw new SessionRecordException(
                            "result.json is " + WireName(Phase) + " but carries a failure_reason");
                    }

                    if (ToGeneration != null)
                    {
                        throw new SessionRecordException(
                            "result.json is " + WireName(Phase) + " but carries a to_generation; only ready does");
                    }

                    break;
            }
        }

        private void RequireReady()
        {
            if (EnteredEditModeUtc == null)
            {
                throw new SessionRecordException(
                    "result.json cannot be ready without an observed EnteredEditMode for this ticket");
            }

            if (EnteredPlayModeUtc == null)
            {
                throw new SessionRecordException(
                    "result.json cannot be ready without an observed EnteredPlayMode for this ticket");
            }

            if (ReadyUtc == null)
            {
                throw new SessionRecordException("result.json cannot be ready without a ready time");
            }

            if (ScenePath == null || ScenePath.Trim().Length == 0)
            {
                throw new SessionRecordException("result.json cannot be ready without the loaded scene path");
            }

            if (SceneHandle == null)
            {
                throw new SessionRecordException("result.json cannot be ready without the loaded scene handle");
            }

            if (ToGeneration == null)
            {
                throw new SessionRecordException("result.json cannot be ready without a fresh to_generation");
            }

            if (string.Equals(ToGeneration, FromGeneration, StringComparison.Ordinal))
            {
                throw new SessionRecordException(
                    "result.json cannot be ready with the same generation it started from: no new play instance was observed");
            }

            if (FailureReason != null)
            {
                throw new SessionRecordException("result.json cannot be ready and carry a failure_reason");
            }

            if (EnteredEditModeUtc.Value > EnteredPlayModeUtc.Value)
            {
                throw new SessionRecordException("result.json observed EnteredPlayMode before EnteredEditMode");
            }

            if (EnteredPlayModeUtc.Value > ReadyUtc.Value)
            {
                throw new SessionRecordException("result.json observed readiness before EnteredPlayMode");
            }
        }

        public static RestartResultRecord Parse(string json)
        {
            JObject obj = SessionRecordJson.ParseObject(json, "result.json");
            SessionRecordJson.RequireExactKeys(obj, "result.json", Keys);
            SessionRecordJson.RequireVersion(obj, "result.json");
            var result = new RestartResultRecord
            {
                RunId = SessionRecordJson.RequireString(obj, "run_id", "result.json"),
                TicketId = SessionRecordJson.RequireString(obj, "ticket_id", "result.json"),
                Phase = ParsePhase(SessionRecordJson.RequireString(obj, "phase", "result.json")),
                FromGeneration = SessionRecordJson.RequireUuid(obj, "from_generation", "result.json"),
                ToGeneration = SessionRecordJson.OptionalUuid(obj, "to_generation", "result.json"),
                EnteredEditModeUtc = SessionRecordJson.OptionalTimestampUtc(
                    obj, "entered_edit_mode_utc", "result.json"),
                EnteredPlayModeUtc = SessionRecordJson.OptionalTimestampUtc(
                    obj, "entered_play_mode_utc", "result.json"),
                ReadyUtc = SessionRecordJson.OptionalTimestampUtc(obj, "ready_utc", "result.json"),
                ScenePath = SessionRecordJson.OptionalString(obj, "scene_path", "result.json"),
                SceneHandle = SessionRecordJson.OptionalInteger(obj, "scene_handle", "result.json"),
                FailureReason = SessionRecordJson.OptionalString(obj, "failure_reason", "result.json"),
            };
            result.Validate();
            return result;
        }

        public string ToJson()
        {
            Validate();
            return SessionRecordJson.Serialize(new JObject
            {
                ["version"] = CurrentVersion,
                ["run_id"] = RunId,
                ["ticket_id"] = TicketId,
                ["phase"] = WireName(Phase),
                ["from_generation"] = FromGeneration,
                ["to_generation"] = SessionRecordJson.Nullable(ToGeneration),
                ["entered_edit_mode_utc"] = SessionRecordJson.Nullable(EnteredEditModeUtc),
                ["entered_play_mode_utc"] = SessionRecordJson.Nullable(EnteredPlayModeUtc),
                ["ready_utc"] = SessionRecordJson.Nullable(ReadyUtc),
                ["scene_path"] = SessionRecordJson.Nullable(ScenePath),
                ["scene_handle"] = SessionRecordJson.Nullable(SceneHandle),
                ["failure_reason"] = SessionRecordJson.Nullable(FailureReason),
            });
        }

        /// <summary>
        /// The record for a request the host refuses before dispatch: a definite failure for that
        /// ticket, with no phase transition and no invented readiness.
        /// </summary>
        public static RestartResultRecord Rejected(string runId, RestartRequestRecord request, string reason)
        {
            var result = new RestartResultRecord
            {
                RunId = runId,
                TicketId = request.TicketId,
                Phase = RestartPhase.Failed,
                FromGeneration = request.FromGeneration,
                FailureReason = reason,
            };
            result.Validate();
            return result;
        }
    }
}
