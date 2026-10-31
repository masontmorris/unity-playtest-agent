// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using NUnit.Framework;
using Playtest.Bridge.Editor.Session;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The mailbox's wire contract on the C# side: the same names, the same closed sets, no unknown
    /// field silently accepted and no value invented. The Python contracts own the fixtures; these
    /// tests keep the host's DTOs from drifting away from the agreed `<c>interface.md</c>`.
    /// </summary>
    [TestFixture]
    public sealed class SessionProtocolTests
    {
        private static readonly string[] ConfigKeys =
        {
            "version", "run_id", "project_path", "catalog_path", "catalog_sha256", "deadline_utc",
        };

        private static readonly string[] HostStateKeys =
        {
            "version", "run_id", "phase", "generation", "scene_path", "scene_handle", "ready_utc", "error",
            "capabilities",
        };

        private static readonly string[] RequestKeys =
        {
            "version", "run_id", "ticket_id", "operation", "from_generation", "deadline_utc",
        };

        private static readonly string[] CancelKeys = { "version", "run_id", "ticket_id", "operation" };

        private static readonly string[] ResultKeys =
        {
            "version", "run_id", "ticket_id", "phase", "from_generation", "to_generation",
            "entered_edit_mode_utc", "entered_play_mode_utc", "ready_utc", "scene_path", "scene_handle",
            "failure_reason",
        };

        [Test]
        public void Config_RoundTripsThroughItsWireForm()
        {
            SessionConfig config = SessionTestSupport.Config();
            SessionConfig parsed = SessionConfig.Parse(config.ToJson());

            Assert.That(parsed.Version, Is.EqualTo(1));
            Assert.That(parsed.RunId, Is.EqualTo(config.RunId));
            Assert.That(parsed.ProjectPath, Is.EqualTo(config.ProjectPath));
            Assert.That(parsed.CatalogPath, Is.EqualTo(config.CatalogPath));
            Assert.That(parsed.CatalogSha256, Is.EqualTo(config.CatalogSha256));
            Assert.That(parsed.DeadlineUtc, Is.EqualTo(SessionTestSupport.T0.AddMinutes(30)));
            Assert.That(parsed.DeadlineUtc.Kind, Is.EqualTo(DateTimeKind.Utc));
        }

        [Test]
        public void EveryRecordWritesExactlyTheAgreedKeys()
        {
            SessionConfig config = SessionTestSupport.Config();
            Assert.That(Keys(config.ToJson()), Is.EquivalentTo(ConfigKeys));

            Assert.That(Keys(StartingState().ToJson()), Is.EquivalentTo(HostStateKeys));
            Assert.That(Keys(ReadyState().ToJson()), Is.EquivalentTo(HostStateKeys));

            Assert.That(Keys(SessionTestSupport.Json(Request())), Is.EquivalentTo(RequestKeys));

            var cancel = new JObject
            {
                ["version"] = 1,
                ["run_id"] = SessionTestSupport.RunId,
                ["ticket_id"] = SessionTestSupport.TicketId,
                ["operation"] = RestartCancelRecord.CancelOperation,
            };
            Assert.That(Keys(cancel.ToString()), Is.EquivalentTo(CancelKeys));

            Assert.That(Keys(SessionTestSupport.ReadyResult().ToJson()), Is.EquivalentTo(ResultKeys));
        }

        [Test]
        public void Config_RejectsUnknownMissingAndMistypedValues()
        {
            string valid = SessionTestSupport.Config().ToJson();

            AssertRejected(() => SessionConfig.Parse(SessionTestSupport.Mutate(
                valid, o => o["extra"] = true)), "unknown");

            foreach (string key in ConfigKeys)
            {
                AssertRejected(() => SessionConfig.Parse(SessionTestSupport.Mutate(
                    valid, o => o.Remove(key))), "missing");
            }

            AssertRejected(() => SessionConfig.Parse(SessionTestSupport.Mutate(
                valid, o => o["version"] = "1")), "version");
            AssertRejected(() => SessionConfig.Parse(SessionTestSupport.Mutate(
                valid, o => o["version"] = 2)), "version");
            AssertRejected(() => SessionConfig.Parse(SessionTestSupport.Mutate(
                valid, o => o["run_id"] = "")), "run_id");
            AssertRejected(() => SessionConfig.Parse(SessionTestSupport.Mutate(
                valid, o => o["project_path"] = "relative/project")), "project_path");
            AssertRejected(() => SessionConfig.Parse(SessionTestSupport.Mutate(
                valid, o => o["catalog_sha256"] = "ABC")), "catalog_sha256");
            AssertRejected(() => SessionConfig.Parse(SessionTestSupport.Mutate(
                valid, o => o["deadline_utc"] = "2026-01-02T03:04:05")), "timezone");
        }

        [Test]
        public void HostState_RoundTripsStartingAndReady()
        {
            HostStateRecord starting = HostStateRecord.Parse(StartingState().ToJson());
            Assert.That(starting.Phase, Is.EqualTo(PlayModeSessionPhase.Starting));
            Assert.That(starting.Generation, Is.Null);
            Assert.That(starting.SceneHandle, Is.Null);
            Assert.That(starting.Capabilities, Is.EqualTo(new[] { HostStateRecord.RestartCapability }));

            HostStateRecord ready = HostStateRecord.Parse(ReadyState().ToJson());
            Assert.That(ready.Phase, Is.EqualTo(PlayModeSessionPhase.Ready));
            Assert.That(ready.Generation, Is.EqualTo(SessionTestSupport.Generation));
            Assert.That(ready.ScenePath, Is.EqualTo(SessionTestSupport.ScenePath));
            Assert.That(ready.SceneHandle, Is.EqualTo(7));
            Assert.That(ready.ReadyUtc, Is.EqualTo(SessionTestSupport.T0));
        }

        [Test]
        public void HostState_RefusesReadyWithoutEveryMeasuredValue()
        {
            string ready = ReadyState().ToJson();
            foreach (string key in new[] { "generation", "scene_path", "scene_handle", "ready_utc" })
            {
                // One message covers the four: a ready state is refused without the whole measurement.
                AssertRejected(() => HostStateRecord.Parse(SessionTestSupport.Mutate(ready, o => o[key] = null)));
            }

            AssertRejected(() => HostStateRecord.Parse(SessionTestSupport.Mutate(
                ready, o => o["error"] = "something")), "error");
            AssertRejected(() => HostStateRecord.Parse(SessionTestSupport.Mutate(
                StartingState().ToJson(), o => o["phase"] = "unknown")), "phase");
            AssertRejected(() => HostStateRecord.Parse(SessionTestSupport.Mutate(
                StartingState().ToJson(), o => o["phase"] = "failed")), "failed");
            AssertRejected(() => HostStateRecord.Parse(SessionTestSupport.Mutate(
                StartingState().ToJson(), o => o["capabilities"] = "restart_play_mode")), "capabilities");
            AssertRejected(() => HostStateRecord.Parse(SessionTestSupport.Mutate(
                StartingState().ToJson(), o => o["generation"] = "NOT-A-UUID")), "generation");
        }

        [Test]
        public void Request_AndCancel_RoundTripThroughTheirWireForms()
        {
            RestartRequestRecord request = SessionTestSupport.ParseRequest(
                SessionTestSupport.Request(SessionTestSupport.T0, fromGeneration: SessionTestSupport.Generation));
            Assert.That(request.RunId, Is.EqualTo(SessionTestSupport.RunId));
            Assert.That(request.TicketId, Is.EqualTo(SessionTestSupport.TicketId));
            Assert.That(request.Operation, Is.EqualTo(RestartRequestRecord.RestartOperation));
            Assert.That(request.FromGeneration, Is.EqualTo(SessionTestSupport.Generation));
            Assert.That(request.DeadlineUtc, Is.EqualTo(SessionTestSupport.T0.AddSeconds(60)));

            RestartCancelRecord cancel = SessionTestSupport.Cancel();
            Assert.That(cancel.Operation, Is.EqualTo(RestartCancelRecord.CancelOperation));
            Assert.That(cancel.TicketId, Is.EqualTo(SessionTestSupport.TicketId));
        }

        [Test]
        public void Request_RefusesAnotherOperationAndANonCanonicalGeneration()
        {
            AssertRejected(() => RestartRequestRecord.Parse(SessionTestSupport.Mutate(
                SessionTestSupport.Json(SessionTestSupport.Request(
                    SessionTestSupport.T0, fromGeneration: SessionTestSupport.Generation)),
                o => o["operation"] = "stop_play_mode")), "operation");

            foreach (string bad in new[]
                     {
                         SessionTestSupport.Generation.ToUpperInvariant(),
                         SessionTestSupport.Generation.Replace("-", string.Empty),
                         "{" + SessionTestSupport.Generation + "}",
                         "../" + SessionTestSupport.Generation,
                         SessionTestSupport.Generation + "/../x",
                         string.Empty,
                     })
            {
                AssertRejected(() => RestartRequestRecord.Parse(SessionTestSupport.Mutate(
                    SessionTestSupport.Json(SessionTestSupport.Request(
                        SessionTestSupport.T0, fromGeneration: SessionTestSupport.Generation)),
                    o => o["from_generation"] = bad)), "from_generation");
            }
        }

        [Test]
        public void Records_RefuseDuplicateKeysAndTrailingContent()
        {
            string valid = SessionTestSupport.Config().ToJson();
            string duplicated = valid.Replace(
                "{\"version\":1,",
                "{\"version\":1,\"run_id\":\"second\",");

            AssertRejected(() => SessionConfig.Parse(duplicated), "already exists");
            AssertRejected(() => SessionConfig.Parse(valid + "{}"), "additional text");
            AssertRejected(() => SessionConfig.Parse(valid + " trailing"), "additional text");
            AssertRejected(() => SessionConfig.Parse("[]"), "not an object");
            AssertRejected(() => SessionConfig.Parse(string.Empty), "empty");
            AssertRejected(() => RestartCancelRecord.Parse(valid), "missing");
        }

        [Test]
        public void Timestamps_RequireAnExplicitOffsetAndResolveToUtc()
        {
            string explicitZ = SessionTestSupport.Mutate(
                SessionTestSupport.Config().ToJson(), o => o["deadline_utc"] = "2026-01-02T03:04:05.0000000Z");
            Assert.That(SessionConfig.Parse(explicitZ).DeadlineUtc.Kind, Is.EqualTo(DateTimeKind.Utc));
            Assert.That(SessionConfig.Parse(explicitZ).DeadlineUtc,
                Is.EqualTo(new DateTime(2026, 1, 2, 3, 4, 5, DateTimeKind.Utc)));

            string withOffset = SessionTestSupport.Mutate(
                SessionTestSupport.Config().ToJson(), o => o["deadline_utc"] = "2026-01-02T05:04:05+02:00");
            Assert.That(SessionConfig.Parse(withOffset).DeadlineUtc,
                Is.EqualTo(new DateTime(2026, 1, 2, 3, 4, 5, DateTimeKind.Utc)));

            string local = SessionTestSupport.Mutate(
                SessionTestSupport.Config().ToJson(), o => o["deadline_utc"] = "2026-01-02T03:04:05");
            AssertRejected(() => SessionConfig.Parse(local), "timezone");
        }

        [Test]
        public void Result_ReadyRoundTripsWithEveryObservedValue()
        {
            RestartResultRecord ready = SessionTestSupport.ReadyResult();
            RestartResultRecord parsed = RestartResultRecord.Parse(ready.ToJson());

            Assert.That(parsed.Phase, Is.EqualTo(RestartPhase.Ready));
            Assert.That(parsed.FromGeneration, Is.EqualTo(SessionTestSupport.Generation));
            Assert.That(parsed.ToGeneration, Is.EqualTo(SessionTestSupport.OtherGeneration));
            Assert.That(parsed.EnteredEditModeUtc, Is.EqualTo(SessionTestSupport.T0));
            Assert.That(parsed.EnteredPlayModeUtc, Is.EqualTo(SessionTestSupport.T0.AddSeconds(1)));
            Assert.That(parsed.ReadyUtc, Is.EqualTo(SessionTestSupport.T0.AddSeconds(2)));
            Assert.That(parsed.ScenePath, Is.EqualTo(SessionTestSupport.ScenePath));
            Assert.That(parsed.SceneHandle, Is.EqualTo(7));
            Assert.That(parsed.FailureReason, Is.Null);
        }

        [Test]
        public void Result_ReadyIsRefusedWithoutAnyObservedValue()
        {
            string ready = SessionTestSupport.ReadyResult().ToJson();
            foreach (string key in new[]
                     {
                         "entered_edit_mode_utc", "entered_play_mode_utc", "ready_utc", "scene_path",
                         "scene_handle",
                     })
            {
                AssertRejected(() => RestartResultRecord.Parse(SessionTestSupport.Mutate(ready, o => o[key] = null)));
            }

            AssertRejected(() => RestartResultRecord.Parse(SessionTestSupport.Mutate(
                ready, o => o["to_generation"] = null)), "to_generation");
            AssertRejected(() => RestartResultRecord.Parse(SessionTestSupport.Mutate(
                ready, o => o["to_generation"] = SessionTestSupport.Generation)), "same generation");
            AssertRejected(() => RestartResultRecord.Parse(SessionTestSupport.Mutate(
                ready, o => o["failure_reason"] = "partial")), "failure_reason");
            AssertRejected(() => RestartResultRecord.Parse(SessionTestSupport.Mutate(
                ready, o => o["entered_play_mode_utc"] = SessionRecordJson.Timestamp(
                    SessionTestSupport.T0.AddSeconds(-1)))), "EnteredPlayMode before EnteredEditMode");
            AssertRejected(() => RestartResultRecord.Parse(SessionTestSupport.Mutate(
                ready, o => o["ready_utc"] = SessionRecordJson.Timestamp(
                    SessionTestSupport.T0.AddMilliseconds(500)))), "readiness before EnteredPlayMode");
        }

        [Test]
        public void Result_TerminalFailuresNeedAReasonAndNeverCarryAGeneration()
        {
            foreach (RestartPhase phase in new[] { RestartPhase.Failed, RestartPhase.Cancelled })
            {
                var result = new RestartResultRecord
                {
                    RunId = SessionTestSupport.RunId,
                    TicketId = SessionTestSupport.TicketId,
                    Phase = phase,
                    FromGeneration = SessionTestSupport.Generation,
                    FailureReason = "operation_deadline",
                };
                Assert.That(RestartResultRecord.Parse(result.ToJson()).Phase, Is.EqualTo(phase));

                result.FailureReason = null;
                Assert.Throws<SessionRecordException>(() => result.ToJson());

                result.FailureReason = "operation_deadline";
                result.ToGeneration = SessionTestSupport.OtherGeneration;
                Assert.Throws<SessionRecordException>(() => result.ToJson());
            }
        }

        [Test]
        public void Result_IntermediatePhasesCarryNoReasonAndNoGeneration()
        {
            foreach (RestartPhase phase in new[]
                     {
                         RestartPhase.Accepted, RestartPhase.Stopped, RestartPhase.Starting,
                     })
            {
                var result = new RestartResultRecord
                {
                    RunId = SessionTestSupport.RunId,
                    TicketId = SessionTestSupport.TicketId,
                    Phase = phase,
                    FromGeneration = SessionTestSupport.Generation,
                };
                Assert.That(RestartResultRecord.Parse(result.ToJson()).Phase, Is.EqualTo(phase));

                result.FailureReason = "not yet";
                Assert.Throws<SessionRecordException>(() => result.ToJson());

                result.FailureReason = null;
                result.ToGeneration = SessionTestSupport.OtherGeneration;
                Assert.Throws<SessionRecordException>(() => result.ToJson());
            }
        }

        [Test]
        public void Result_PhaseNamesAreAClosedSet()
        {
            var expected = new Dictionary<RestartPhase, string>
            {
                { RestartPhase.Accepted, "accepted" },
                { RestartPhase.Stopped, "stopped" },
                { RestartPhase.Starting, "starting" },
                { RestartPhase.Ready, "ready" },
                { RestartPhase.Failed, "failed" },
                { RestartPhase.Cancelled, "cancelled" },
            };
            foreach (KeyValuePair<RestartPhase, string> pair in expected)
            {
                Assert.That(RestartResultRecord.WireName(pair.Key), Is.EqualTo(pair.Value));
                Assert.That(RestartResultRecord.ParsePhase(pair.Value), Is.EqualTo(pair.Key));
            }

            Assert.Throws<SessionRecordException>(() => RestartResultRecord.ParsePhase("done"));
            Assert.Throws<ArgumentOutOfRangeException>(() => RestartResultRecord.WireName((RestartPhase)99));
        }

        [Test]
        public void ARejectedRequestPublishesADefiniteFailureAndNoTransition()
        {
            RestartRequestRecord request = SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: SessionTestSupport.Generation);
            RestartResultRecord rejected = RestartResultRecord.Rejected(
                SessionTestSupport.RunId, request, "stale generation");

            Assert.That(rejected.Phase, Is.EqualTo(RestartPhase.Failed));
            Assert.That(rejected.TicketId, Is.EqualTo(SessionTestSupport.TicketId));
            Assert.That(rejected.FromGeneration, Is.EqualTo(SessionTestSupport.Generation));
            Assert.That(rejected.ToGeneration, Is.Null);
            Assert.That(rejected.FailureReason, Is.EqualTo("stale generation"));
            Assert.That(rejected.EnteredEditModeUtc, Is.Null);
            Assert.That(rejected.ReadyUtc, Is.Null);

            // The record is publishable as-is: the host does not need to fill anything in.
            Assert.That(RestartResultRecord.Parse(rejected.ToJson()).Phase, Is.EqualTo(RestartPhase.Failed));
        }

        [Test]
        public void ControlRecords_CarryNoTokenPortOrGameplayInput()
        {
            var records = new[]
            {
                SessionTestSupport.Config().ToJson(),
                ReadyState().ToJson(),
                SessionTestSupport.Json(SessionTestSupport.Request(
                    SessionTestSupport.T0, fromGeneration: SessionTestSupport.Generation)),
                SessionTestSupport.ReadyResult().ToJson(),
            };

            foreach (string json in records)
            {
                JObject obj = JObject.Parse(json);
                var names = obj.Properties().Select(p => p.Name).ToList();
                Assert.That(names, Has.None.Matches<string>(n => n.Contains("token")), json);
                Assert.That(names, Has.None.Matches<string>(n => n.Contains("key")), json);
                Assert.That(names, Has.None.Matches<string>(n => n.Contains("port")), json);
                Assert.That(names, Has.None.Matches<string>(n => n.Contains("input")), json);
                Assert.That(names, Has.None.Matches<string>(n => n.Contains("pid")), json);

                // The generation is the only identity here; the transport authority stays the lock file.
                Assert.That(json, Does.Not.Contain("token"));
            }
        }

        private static HostStateRecord StartingState()
        {
            return new HostStateRecord
            {
                RunId = SessionTestSupport.RunId,
                Phase = PlayModeSessionPhase.Starting,
                Capabilities = new[] { HostStateRecord.RestartCapability },
            };
        }

        private static HostStateRecord ReadyState()
        {
            return new HostStateRecord
            {
                RunId = SessionTestSupport.RunId,
                Phase = PlayModeSessionPhase.Ready,
                Generation = SessionTestSupport.Generation,
                ScenePath = SessionTestSupport.ScenePath,
                SceneHandle = 7,
                ReadyUtc = SessionTestSupport.T0,
                Capabilities = new[] { HostStateRecord.RestartCapability },
            };
        }

        private static RestartRequestRecord Request()
        {
            return SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: SessionTestSupport.Generation);
        }

        private static HashSet<string> Keys(string json)
        {
            return new HashSet<string>(
                JObject.Parse(json).Properties().Select(p => p.Name), StringComparer.Ordinal);
        }

        private static void AssertRejected(TestDelegate action, string expectedInMessage = null)
        {
            SessionRecordException thrown = Assert.Throws<SessionRecordException>(action, expectedInMessage);
            Assert.That(thrown.Message, Is.Not.Empty);
            if (expectedInMessage != null)
            {
                Assert.That(
                    thrown.Message.ToLowerInvariant(),
                    Does.Contain(expectedInMessage.ToLowerInvariant()),
                    thrown.Message);
            }
        }
    }
}
