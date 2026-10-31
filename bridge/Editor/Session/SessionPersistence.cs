// SPDX-License-Identifier: Apache-2.0
using Newtonsoft.Json.Linq;

namespace Playtest.Bridge.Editor.Session
{
    /// <summary>
    /// The session's resumable state, kept in <c>SessionState</c> across a domain reload. It is written
    /// before every play-mode transition, so a reload can continue the same ticket instead of minting a
    /// second generation or replaying an operation. It is editor-session memory only: nothing here is
    /// written into the project or the control directory, and it carries no token or key.
    /// </summary>
    public static class SessionPersistence
    {
        public const int CurrentVersion = 1;

        private static readonly string[] Keys =
        {
            "version", "control_root", "run_id", "host_phase", "generation", "scene_path", "scene_handle",
            "ready_utc", "error", "last_completed_ticket_id", "ticket",
        };

        private static readonly string[] TicketKeys =
        {
            "ticket_id", "from_generation", "request_deadline_utc", "phase", "entered_edit_mode_utc",
            "entered_play_mode_utc", "ready_utc", "to_generation", "scene_path", "scene_handle",
            "failure_reason",
        };

        public static string Save(PlayModeSessionMachine machine, string controlRoot)
        {
            JToken ticket = JValue.CreateNull();
            if (machine.Ticket != null)
            {
                RestartTicket value = machine.Ticket;
                ticket = new JObject
                {
                    ["ticket_id"] = value.TicketId,
                    ["from_generation"] = value.FromGeneration,
                    ["request_deadline_utc"] = SessionRecordJson.Timestamp(value.RequestDeadlineUtc),
                    ["phase"] = RestartResultRecord.WireName(value.Phase),
                    ["entered_edit_mode_utc"] = SessionRecordJson.Nullable(value.EnteredEditModeUtc),
                    ["entered_play_mode_utc"] = SessionRecordJson.Nullable(value.EnteredPlayModeUtc),
                    ["ready_utc"] = SessionRecordJson.Nullable(value.ReadyUtc),
                    ["to_generation"] = SessionRecordJson.Nullable(value.ToGeneration),
                    ["scene_path"] = SessionRecordJson.Nullable(value.ScenePath),
                    ["scene_handle"] = SessionRecordJson.Nullable(value.SceneHandle),
                    ["failure_reason"] = SessionRecordJson.Nullable(value.FailureReason),
                };
            }

            return SessionRecordJson.Serialize(new JObject
            {
                ["version"] = CurrentVersion,
                ["control_root"] = controlRoot,
                ["run_id"] = machine.Config.RunId,
                ["host_phase"] = HostStateRecord.WireName(machine.HostPhase),
                ["generation"] = SessionRecordJson.Nullable(machine.Generation),
                ["scene_path"] = SessionRecordJson.Nullable(machine.ScenePath),
                ["scene_handle"] = SessionRecordJson.Nullable(machine.SceneHandle),
                ["ready_utc"] = SessionRecordJson.Nullable(machine.ReadyUtc),
                ["error"] = SessionRecordJson.Nullable(machine.Error),
                ["last_completed_ticket_id"] = SessionRecordJson.Nullable(machine.LastCompletedTicketId),
                ["ticket"] = ticket,
            });
        }

        public static PlayModeSessionMachine Restore(string json, SessionConfig config, string controlRoot)
        {
            JObject obj = SessionRecordJson.ParseObject(json, "session state");
            SessionRecordJson.RequireExactKeys(obj, "session state", Keys);
            SessionRecordJson.RequireVersion(obj, "session state");

            string savedRoot = SessionRecordJson.RequireString(obj, "control_root", "session state");
            if (!string.Equals(savedRoot, controlRoot, System.StringComparison.OrdinalIgnoreCase))
            {
                throw new SessionRecordException(
                    "the saved session belongs to '" + savedRoot + "', not '" + controlRoot + "'");
            }

            string runId = SessionRecordJson.RequireString(obj, "run_id", "session state");
            if (!string.Equals(runId, config.RunId, System.StringComparison.Ordinal))
            {
                throw new SessionRecordException(
                    "the saved session belongs to run '" + runId + "', not '" + config.RunId + "'");
            }

            var machine = new PlayModeSessionMachine(config)
            {
                HostPhase = HostStateRecord.ParsePhase(
                    SessionRecordJson.RequireString(obj, "host_phase", "session state")),
                Generation = SessionRecordJson.OptionalUuid(obj, "generation", "session state"),
                ScenePath = SessionRecordJson.OptionalString(obj, "scene_path", "session state"),
                SceneHandle = SessionRecordJson.OptionalInteger(obj, "scene_handle", "session state"),
                ReadyUtc = SessionRecordJson.OptionalTimestampUtc(obj, "ready_utc", "session state"),
                Error = SessionRecordJson.OptionalString(obj, "error", "session state"),
            };

            JToken ticketToken = obj["ticket"];
            if (ticketToken != null && ticketToken.Type != JTokenType.Null)
            {
                if (ticketToken.Type != JTokenType.Object)
                {
                    throw new SessionRecordException("session state.ticket must be an object or null");
                }

                JObject ticket = (JObject)ticketToken;
                SessionRecordJson.RequireExactKeys(ticket, "session state.ticket", TicketKeys);
                var restored = new RestartTicket(
                    SessionRecordJson.RequireString(ticket, "ticket_id", "session state.ticket"),
                    SessionRecordJson.RequireUuid(ticket, "from_generation", "session state.ticket"),
                    SessionRecordJson.RequireTimestampUtc(ticket, "request_deadline_utc", "session state.ticket"))
                {
                    Phase = RestartResultRecord.ParsePhase(
                        SessionRecordJson.RequireString(ticket, "phase", "session state.ticket")),
                    EnteredEditModeUtc = SessionRecordJson.OptionalTimestampUtc(
                        ticket, "entered_edit_mode_utc", "session state.ticket"),
                    EnteredPlayModeUtc = SessionRecordJson.OptionalTimestampUtc(
                        ticket, "entered_play_mode_utc", "session state.ticket"),
                    ReadyUtc = SessionRecordJson.OptionalTimestampUtc(ticket, "ready_utc", "session state.ticket"),
                    ToGeneration = SessionRecordJson.OptionalUuid(ticket, "to_generation", "session state.ticket"),
                    ScenePath = SessionRecordJson.OptionalString(ticket, "scene_path", "session state.ticket"),
                    SceneHandle = SessionRecordJson.OptionalInteger(ticket, "scene_handle", "session state.ticket"),
                    FailureReason = SessionRecordJson.OptionalString(ticket, "failure_reason", "session state.ticket"),
                };
                restored.ToResult(config.RunId); // refuses to restore a ready ticket that is not fully measured
                machine.Ticket = restored;
            }

            machine.LastCompletedTicketId = SessionRecordJson.OptionalString(
                obj, "last_completed_ticket_id", "session state");
            return machine;
        }
    }
}
