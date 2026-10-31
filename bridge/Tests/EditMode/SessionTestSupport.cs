// SPDX-License-Identifier: Apache-2.0
using System;
using System.IO;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Playtest.Bridge.Editor.Session;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Values shared by the session-host tests: one run, two canonical generations, one fixed instant
    /// and small builders. Every timestamp is supplied by the test, so nothing here reads the clock and
    /// no assertion depends on how long the suite takes.
    /// </summary>
    internal static class SessionTestSupport
    {
        public const string RunId = "run-session-1";
        public const string TicketId = "ticket-1";
        public const string ScenePath = "Assets/Scenes/Main.unity";

        /// <summary>Canonical lower-case hyphenated UUIDs, the only shape a generation may have.</summary>
        public const string Generation = "3f2f1e9c-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
        public const string OtherGeneration = "0b4c2d1e-9f8a-4b7c-9d6e-5f4a3b2c1d0e";

        public static readonly DateTime T0 = new DateTime(2026, 1, 2, 3, 4, 5, DateTimeKind.Utc);

        public const string CatalogSha256 =
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

        public static string TempDirectory()
        {
            return Path.Combine(Path.GetTempPath(), "upa-session-" + Guid.NewGuid().ToString("N"));
        }

        public static SessionConfig Config(DateTime? deadlineUtc = null, string runId = RunId)
        {
            string root = Path.GetFullPath(Path.Combine(Path.GetTempPath(), "upa-session-project"));
            var config = new SessionConfig
            {
                RunId = runId,
                ProjectPath = root,
                CatalogPath = Path.Combine(root, "catalog.json"),
                CatalogSha256 = CatalogSha256,
                DeadlineUtc = deadlineUtc ?? T0.AddMinutes(30),
            };
            return config;
        }

        /// <summary>A host that measured its initial generation, scene and bridge, in the ready phase.</summary>
        public static PlayModeSessionMachine ReadyMachine(out SessionConfig config, DateTime? nowUtc = null)
        {
            DateTime now = nowUtc ?? T0;
            config = Config(now.AddMinutes(30));
            var machine = new PlayModeSessionMachine(config);
            if (!machine.MarkInitialSceneLoaded(ScenePath, 1))
            {
                throw new InvalidOperationException("test setup: " + machine.LastRejectedTransition);
            }

            machine.MintGenerationForPlayInstance();
            if (!machine.MarkInitialReady(now))
            {
                throw new InvalidOperationException("test setup: " + machine.LastRejectedTransition);
            }

            return machine;
        }

        public static RestartRequestRecord Request(
            DateTime nowUtc,
            string ticketId = TicketId,
            string fromGeneration = null,
            string runId = RunId,
            TimeSpan? deadline = null)
        {
            return new RestartRequestRecord
            {
                RunId = runId,
                TicketId = ticketId,
                Operation = RestartRequestRecord.RestartOperation,
                FromGeneration = fromGeneration,
                DeadlineUtc = nowUtc + (deadline ?? TimeSpan.FromSeconds(60)),
            };
        }

        /// <summary>Parses a request through the real wire path, so a test cannot skip validation.</summary>
        public static RestartRequestRecord ParseRequest(RestartRequestRecord request)
        {
            return RestartRequestRecord.Parse(Json(request));
        }

        public static string Json(RestartRequestRecord request)
        {
            return new JObject
            {
                ["version"] = RestartRequestRecord.CurrentVersion,
                ["run_id"] = request.RunId,
                ["ticket_id"] = request.TicketId,
                ["operation"] = request.Operation,
                ["from_generation"] = request.FromGeneration,
                ["deadline_utc"] = SessionRecordJson.Timestamp(request.DeadlineUtc),
            }.ToString(Formatting.None);
        }

        public static RestartCancelRecord Cancel(string ticketId = TicketId, string runId = RunId)
        {
            return RestartCancelRecord.Parse(new JObject
            {
                ["version"] = RestartCancelRecord.CurrentVersion,
                ["run_id"] = runId,
                ["ticket_id"] = ticketId,
                ["operation"] = RestartCancelRecord.CancelOperation,
            }.ToString(Formatting.None));
        }

        public static RestartResultRecord ReadyResult()
        {
            return new RestartResultRecord
            {
                RunId = RunId,
                TicketId = TicketId,
                Phase = RestartPhase.Ready,
                FromGeneration = Generation,
                ToGeneration = OtherGeneration,
                EnteredEditModeUtc = T0,
                EnteredPlayModeUtc = T0.AddSeconds(1),
                ReadyUtc = T0.AddSeconds(2),
                ScenePath = ScenePath,
                SceneHandle = 7,
            };
        }

        /// <summary>Applies one edit to a serialized record and returns the new text.</summary>
        public static string Mutate(string json, Action<JObject> edit)
        {
            // Dates stay strings: a mutation must not silently rewrite an instant into another format.
            var settings = new JsonSerializerSettings { DateParseHandling = DateParseHandling.None };
            JObject obj = JsonConvert.DeserializeObject<JObject>(json, settings);
            edit(obj);
            return obj.ToString(Formatting.None);
        }
    }
}
