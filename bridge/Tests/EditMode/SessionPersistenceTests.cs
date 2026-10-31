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
    /// The host survives a domain reload without replaying an operation or minting a second generation.
    /// What it persists is the same closed record it publishes elsewhere: resuming a saved session that
    /// belongs to another run, another control directory, or claims readiness it did not measure is a
    /// failure, never a repair.
    /// </summary>
    [TestFixture]
    public sealed class SessionPersistenceTests
    {
        private static readonly string[] StateKeys =
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

        [Test]
        public void AReadySessionResumesWithTheSameMeasuredInstance()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = SessionTestSupport.ReadyMachine(out config);
            string root = SessionTestSupport.TempDirectory();

            string saved = SessionPersistence.Save(machine, root);
            PlayModeSessionMachine resumed = SessionPersistence.Restore(saved, config, root);

            Assert.That(resumed.HostPhase, Is.EqualTo(PlayModeSessionPhase.Ready));
            Assert.That(resumed.Generation, Is.EqualTo(machine.Generation));
            Assert.That(resumed.ScenePath, Is.EqualTo(SessionTestSupport.ScenePath));
            Assert.That(resumed.SceneHandle, Is.EqualTo(1));
            Assert.That(resumed.ReadyUtc, Is.EqualTo(SessionTestSupport.T0));
            Assert.That(resumed.Ticket, Is.Null);
            Assert.That(resumed.Revision, Is.EqualTo(0),
                "a resumed host republishes its current state instead of inventing a transition");
        }

        [Test]
        public void AnInFlightTicketResumesAtItsPhaseAndStillNeedsTheNewInstance()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = SessionTestSupport.ReadyMachine(out config);
            string root = SessionTestSupport.TempDirectory();

            RestartRequestRecord request = SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: machine.Generation);
            Assert.That(machine.TryAccept(request, SessionTestSupport.T0, out string error),
                Is.EqualTo(RestartAcceptance.Accepted), error);
            machine.MarkBridgeStopped(SessionTestSupport.T0);
            machine.MarkEditModeEntered(SessionTestSupport.T0.AddSeconds(1));

            PlayModeSessionMachine resumed = SessionPersistence.Restore(
                SessionPersistence.Save(machine, root), config, root);

            Assert.That(resumed.HostPhase, Is.EqualTo(PlayModeSessionPhase.Starting));
            Assert.That(resumed.Ticket, Is.Not.Null);
            Assert.That(resumed.Ticket.TicketId, Is.EqualTo(SessionTestSupport.TicketId));
            Assert.That(resumed.Ticket.Phase, Is.EqualTo(RestartPhase.Starting));
            Assert.That(resumed.Ticket.EnteredEditModeUtc, Is.EqualTo(SessionTestSupport.T0.AddSeconds(1)));
            Assert.That(resumed.Generation, Is.Null, "the old instance's generation does not survive it");

            Assert.That(resumed.MarkPlayModeEntered(SessionTestSupport.T0.AddSeconds(2)), Is.True,
                resumed.LastRejectedTransition);
            Assert.That(resumed.MarkSceneLoaded(SessionTestSupport.ScenePath, 12), Is.True);
            string fresh = resumed.MintGenerationForPlayInstance();
            Assert.That(fresh, Is.Not.EqualTo(request.FromGeneration));
            Assert.That(resumed.MarkBridgeStarted(fresh, SessionTestSupport.T0.AddSeconds(3)), Is.True,
                resumed.LastRejectedTransition);
            Assert.That(resumed.Ticket.ToResult(SessionTestSupport.RunId).Phase, Is.EqualTo(RestartPhase.Ready));
        }

        [Test]
        public void ACompletedTicketStaysCompletedAndDuplicatesStayRefused()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = SessionTestSupport.ReadyMachine(out config);
            string root = SessionTestSupport.TempDirectory();
            RestartRequestRecord request = SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: machine.Generation);
            machine.TryAccept(request, SessionTestSupport.T0, out _);
            machine.MarkBridgeStopped(SessionTestSupport.T0);
            machine.MarkEditModeEntered(SessionTestSupport.T0.AddSeconds(1));
            machine.MarkPlayModeEntered(SessionTestSupport.T0.AddSeconds(2));
            machine.MarkSceneLoaded(SessionTestSupport.ScenePath, 5);
            string fresh = machine.MintGenerationForPlayInstance();
            machine.MarkBridgeStarted(fresh, SessionTestSupport.T0.AddSeconds(3));

            PlayModeSessionMachine resumed = SessionPersistence.Restore(
                SessionPersistence.Save(machine, root), config, root);

            JObject savedState = JObject.Parse(SessionPersistence.Save(machine, root));
            Assert.That(
                ((JObject)savedState["ticket"]).Properties().Select(p => p.Name),
                Is.EquivalentTo(TicketKeys));

            Assert.That(resumed.HostPhase, Is.EqualTo(PlayModeSessionPhase.Ready));
            Assert.That(resumed.Ticket.Phase, Is.EqualTo(RestartPhase.Ready));
            Assert.That(resumed.LastCompletedTicketId, Is.EqualTo(SessionTestSupport.TicketId));
            Assert.That(resumed.TryAccept(request, SessionTestSupport.T0.AddSeconds(4), out string error),
                Is.EqualTo(RestartAcceptance.Duplicate), error);
            Assert.That(resumed.Generation, Is.EqualTo(fresh));
        }

        [Test]
        public void ASavedStateFromAnotherRunOrDirectoryIsRefused()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = SessionTestSupport.ReadyMachine(out config);
            string root = SessionTestSupport.TempDirectory();
            string saved = SessionPersistence.Save(machine, root);

            string otherRun = SessionTestSupport.Mutate(saved, o => o["run_id"] = "another-run");
            Assert.Throws<SessionRecordException>(() => SessionPersistence.Restore(otherRun, config, root));

            string otherRoot = SessionTestSupport.Mutate(saved, o => o["control_root"] = root + "-elsewhere");
            Assert.Throws<SessionRecordException>(() => SessionPersistence.Restore(otherRoot, config, root));
        }

        [Test]
        public void AResumedReadyTicketMustStillBeFullyMeasured()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = SessionTestSupport.ReadyMachine(out config);
            string root = SessionTestSupport.TempDirectory();
            RestartRequestRecord request = SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: machine.Generation);
            machine.TryAccept(request, SessionTestSupport.T0, out _);
            machine.MarkBridgeStopped(SessionTestSupport.T0);
            machine.MarkEditModeEntered(SessionTestSupport.T0.AddSeconds(1));
            machine.MarkPlayModeEntered(SessionTestSupport.T0.AddSeconds(2));
            machine.MarkSceneLoaded(SessionTestSupport.ScenePath, 5);
            machine.MarkBridgeStarted(machine.MintGenerationForPlayInstance(), SessionTestSupport.T0.AddSeconds(3));
            string saved = SessionPersistence.Save(machine, root);

            string withoutGeneration = SessionTestSupport.Mutate(
                saved, o => ((JObject)o["ticket"])["to_generation"] = null);
            Assert.Throws<SessionRecordException>(
                () => SessionPersistence.Restore(withoutGeneration, config, root));

            string withoutScene = SessionTestSupport.Mutate(
                saved, o => ((JObject)o["ticket"])["scene_handle"] = null);
            Assert.Throws<SessionRecordException>(() => SessionPersistence.Restore(withoutScene, config, root));
        }

        [Test]
        public void ASavedInstanceCanNeverPretendToBeAFreshOne()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = SessionTestSupport.ReadyMachine(out config);
            string root = SessionTestSupport.TempDirectory();
            RestartRequestRecord request = SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: machine.Generation);
            machine.TryAccept(request, SessionTestSupport.T0, out _);
            machine.MarkBridgeStopped(SessionTestSupport.T0);
            machine.MarkEditModeEntered(SessionTestSupport.T0.AddSeconds(1));
            machine.MarkPlayModeEntered(SessionTestSupport.T0.AddSeconds(2));
            machine.MarkSceneLoaded(SessionTestSupport.ScenePath, 6);

            // A corrupted or hand-written state that claims the new instance is the old one's generation.
            string tampered = SessionTestSupport.Mutate(SessionPersistence.Save(machine, root), o =>
            {
                o["generation"] = request.FromGeneration;
                o["scene_path"] = SessionTestSupport.ScenePath;
                o["scene_handle"] = 6;
            });

            PlayModeSessionMachine resumed = SessionPersistence.Restore(tampered, config, root);
            Assert.That(resumed.MarkBridgeStarted(resumed.Generation, SessionTestSupport.T0.AddSeconds(3)), Is.False);
            Assert.That(resumed.LastRejectedTransition, Does.Contain("reused"));
            Assert.That(resumed.Ticket.Phase, Is.Not.EqualTo(RestartPhase.Ready));
        }

        [Test]
        public void ASavedStateIsClosedAndCarriesNoToken()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = SessionTestSupport.ReadyMachine(out config);
            string saved = SessionPersistence.Save(machine, SessionTestSupport.TempDirectory());

            JObject obj = JObject.Parse(saved);
            Assert.That(obj.Properties().Select(p => p.Name), Is.EquivalentTo(StateKeys));
            Assert.That(saved, Does.Not.Contain("token"));
            Assert.That(saved, Does.Not.Contain("port"));
            Assert.That(obj["ticket"].Type, Is.EqualTo(JTokenType.Null),
                "an idle host persists an explicit null ticket");
        }

        [Test]
        public void ASavedStateRefusesUnknownKeysAndMalformedShapes()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = SessionTestSupport.ReadyMachine(out config);
            string root = SessionTestSupport.TempDirectory();
            string saved = SessionPersistence.Save(machine, root);

            Assert.Throws<SessionRecordException>(() => SessionPersistence.Restore(
                SessionTestSupport.Mutate(saved, o => o["extra"] = 1), config, root));
            Assert.Throws<SessionRecordException>(() => SessionPersistence.Restore(
                SessionTestSupport.Mutate(saved, o => o["version"] = 2), config, root));
            Assert.Throws<SessionRecordException>(() => SessionPersistence.Restore(
                SessionTestSupport.Mutate(saved, o => o["host_phase"] = "paused"), config, root));
            Assert.Throws<SessionRecordException>(() => SessionPersistence.Restore(
                SessionTestSupport.Mutate(saved, o => o["generation"] = "NOT-A-UUID"), config, root));

            string withTicket = SessionTestSupport.Mutate(saved, o => o["ticket"] = "not an object");
            Assert.Throws<SessionRecordException>(() => SessionPersistence.Restore(withTicket, config, root));

            string partialTicket = SessionTestSupport.Mutate(saved, o => o["ticket"] = new JObject
            {
                ["ticket_id"] = SessionTestSupport.TicketId,
            });
            Assert.Throws<SessionRecordException>(() => SessionPersistence.Restore(partialTicket, config, root));
        }
    }
}
