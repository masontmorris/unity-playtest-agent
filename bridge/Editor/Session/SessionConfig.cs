// SPDX-License-Identifier: Apache-2.0
using System;
using Newtonsoft.Json.Linq;

namespace Playtest.Bridge.Editor.Session
{
    /// <summary>
    /// <c>config.json</c>: what the wrapper already decided before Unity starts. Written once into a
    /// new per-run control directory outside the game project; it never carries a token, key or port.
    /// The deadline is the host's global wall deadline, so the host can exit on its own rather than
    /// waiting for a supervisor.
    /// </summary>
    public sealed class SessionConfig
    {
        public const int CurrentVersion = 1;
        public const string FileName = "config.json";

        private static readonly string[] Keys =
        {
            "version", "run_id", "project_path", "catalog_path", "catalog_sha256", "deadline_utc",
        };

        public int Version { get; set; } = CurrentVersion;

        /// <summary>Non-empty unique run identifier; every other record must carry the same value.</summary>
        public string RunId { get; set; }

        /// <summary>Absolute Unity project root the wrapper launched.</summary>
        public string ProjectPath { get; set; }

        /// <summary>Absolute path to the exported action catalog the host must serve.</summary>
        public string CatalogPath { get; set; }

        /// <summary>Lower-case 64-hex SHA-256 of the exported catalog's bytes.</summary>
        public string CatalogSha256 { get; set; }

        /// <summary>UTC instant after which the host fails closed and exits.</summary>
        public DateTime DeadlineUtc { get; set; }

        public static SessionConfig Parse(string json)
        {
            JObject obj = SessionRecordJson.ParseObject(json, "config.json");
            SessionRecordJson.RequireExactKeys(obj, "config.json", Keys);
            SessionRecordJson.RequireVersion(obj, "config.json");
            var config = new SessionConfig
            {
                RunId = SessionRecordJson.RequireString(obj, "run_id", "config.json"),
                ProjectPath = SessionRecordJson.RequireAbsolutePath(obj, "project_path", "config.json"),
                CatalogPath = SessionRecordJson.RequireAbsolutePath(obj, "catalog_path", "config.json"),
                CatalogSha256 = SessionRecordJson.RequireSha256(obj, "catalog_sha256", "config.json"),
                DeadlineUtc = SessionRecordJson.RequireTimestampUtc(obj, "deadline_utc", "config.json"),
            };
            return config;
        }

        public static bool TryParse(string json, out SessionConfig config, out string error)
        {
            try
            {
                config = Parse(json);
                error = null;
                return true;
            }
            catch (SessionRecordException ex)
            {
                config = null;
                error = ex.Message;
                return false;
            }
        }

        public string ToJson()
        {
            return SessionRecordJson.Serialize(new JObject
            {
                ["version"] = CurrentVersion,
                ["run_id"] = RunId,
                ["project_path"] = ProjectPath,
                ["catalog_path"] = CatalogPath,
                ["catalog_sha256"] = CatalogSha256,
                ["deadline_utc"] = SessionRecordJson.Timestamp(DeadlineUtc),
            });
        }
    }
}
