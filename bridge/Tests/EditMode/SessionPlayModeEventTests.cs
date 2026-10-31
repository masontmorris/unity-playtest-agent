// SPDX-License-Identifier: Apache-2.0
using System;
using System.IO;
using NUnit.Framework;
using Playtest.Bridge.Editor.Session;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The play-mode callback after a domain reload. Unity can raise the transition a ticket is waiting
    /// for before the first Editor update, after the static constructor has run but before any Tick; the
    /// controller restores the persisted machine first, then applies the event through these rules.
    /// Each case uses the real machine and the real persisted state, so it measures the order and the
    /// timestamps rather than the text of the callback.
    /// </summary>
    [TestFixture]
    public sealed class SessionPlayModeEventTests
    {
        private static readonly string ControlRoot =
            Path.GetFullPath(Path.Combine(Path.GetTempPath(), "upa-session-control"));

        [Test]
        public void AnEnteredEditModeBeforeTheFirstUpdatePreparesTheRestoredTicket()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = ReloadedRestart(out config);
            DateTime observed = SessionTestSupport.T0.AddSeconds(2);

            SessionEventEffect effect = PlayModeSessionEvents.Apply(
                machine, SessionPlayModeChange.EnteredEditMode, observed);

            Assert.That(effect.Persist, Is.True, "the observed transition is written before anything else");
            Assert.That(effect.Publish, Is.True);
            Assert.That(effect.RequestExit, Is.False);
            Assert.That(machine.Ticket.Phase, Is.EqualTo(RestartPhase.Starting));
            Assert.That(machine.Ticket.EnteredEditModeUtc, Is.EqualTo(observed));
            Assert.That(machine.Ticket.EnteredPlayModeUtc, Is.Null,
                "play mode was not observed yet and is never inferred");
            Assert.That(machine.HostPhase, Is.EqualTo(PlayModeSessionPhase.Starting));
        }

        [Test]
        public void AnEnteredPlayModeBeforeTheFirstUpdateIsAppliedAfterTheEditModeObservation()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = ReloadedRestart(out config);
            DateTime editMode = SessionTestSupport.T0.AddSeconds(2);
            DateTime playMode = SessionTestSupport.T0.AddSeconds(3);

            PlayModeSessionEvents.Apply(machine, SessionPlayModeChange.EnteredEditMode, editMode);
            SessionEventEffect effect = PlayModeSessionEvents.Apply(
                machine, SessionPlayModeChange.EnteredPlayMode, playMode);

            Assert.That(effect.Persist, Is.True);
            Assert.That(effect.Publish, Is.True);
            Assert.That(machine.Ticket.EnteredEditModeUtc, Is.EqualTo(editMode));
            Assert.That(machine.Ticket.EnteredPlayModeUtc, Is.EqualTo(playMode));
            Assert.That(machine.Ticket.Phase, Is.EqualTo(RestartPhase.Starting));
            Assert.That(machine.Ticket.ReadyUtc, Is.Null, "readiness still needs the scene and the bridge");
            Assert.That(machine.Ticket.ToGeneration, Is.Null);
        }

        [Test]
        public void ADomainReloadBetweenTheTwoObservationsKeepsTheFirstTimestamp()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = ReloadedRestart(out config);
            DateTime editMode = SessionTestSupport.T0.AddSeconds(2);
            PlayModeSessionEvents.Apply(machine, SessionPlayModeChange.EnteredEditMode, editMode);

            // A second reload must not lose or repeat the observation that was already made.
            PlayModeSessionMachine restored = SessionPersistence.Restore(
                SessionPersistence.Save(machine, ControlRoot), config, ControlRoot);
            DateTime playMode = SessionTestSupport.T0.AddSeconds(3);
            PlayModeSessionEvents.Apply(restored, SessionPlayModeChange.EnteredPlayMode, playMode);

            Assert.That(restored.Ticket.EnteredEditModeUtc, Is.EqualTo(editMode));
            Assert.That(restored.Ticket.EnteredPlayModeUtc, Is.EqualTo(playMode));
            Assert.That(restored.Ticket.Phase, Is.EqualTo(RestartPhase.Starting));
        }

        [Test]
        public void ARepeatedEnteredPlayModeNeverMovesTheObservedInstant()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = ReloadedRestart(out config);
            PlayModeSessionEvents.Apply(
                machine, SessionPlayModeChange.EnteredEditMode, SessionTestSupport.T0.AddSeconds(2));
            DateTime first = SessionTestSupport.T0.AddSeconds(3);
            PlayModeSessionEvents.Apply(machine, SessionPlayModeChange.EnteredPlayMode, first);
            int revision = machine.Revision;

            SessionEventEffect effect = PlayModeSessionEvents.Apply(
                machine, SessionPlayModeChange.EnteredPlayMode, SessionTestSupport.T0.AddSeconds(9));

            Assert.That(machine.Ticket.EnteredPlayModeUtc, Is.EqualTo(first));
            Assert.That(machine.Revision, Is.EqualTo(revision), "a duplicate event is not a new transition");
            Assert.That(effect.RequestExit, Is.False);
        }

        [Test]
        public void AnEnteredPlayModeWithoutItsEditModeObservationIsRejected()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = ReloadedRestart(out config);
            int revision = machine.Revision;

            PlayModeSessionEvents.Apply(
                machine, SessionPlayModeChange.EnteredPlayMode, SessionTestSupport.T0.AddSeconds(3));

            Assert.That(machine.Ticket.EnteredPlayModeUtc, Is.Null, "order is enforced, not repaired");
            Assert.That(machine.Ticket.Phase, Is.EqualTo(RestartPhase.Stopped));
            Assert.That(machine.Revision, Is.EqualTo(revision));
            Assert.That(machine.LastRejectedTransition, Does.Contain("before edit mode"));
        }

        [Test]
        public void ARepeatedEnteredEditModeDoesNotStartASecondInstance()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = ReloadedRestart(out config);
            DateTime observed = SessionTestSupport.T0.AddSeconds(2);
            PlayModeSessionEvents.Apply(machine, SessionPlayModeChange.EnteredEditMode, observed);
            int revision = machine.Revision;

            SessionEventEffect effect = PlayModeSessionEvents.Apply(
                machine, SessionPlayModeChange.EnteredEditMode, SessionTestSupport.T0.AddSeconds(4));

            Assert.That(machine.Ticket.EnteredEditModeUtc, Is.EqualTo(observed));
            Assert.That(machine.Ticket.Phase, Is.EqualTo(RestartPhase.Starting));
            Assert.That(machine.Revision, Is.EqualTo(revision), "no duplicate stop/start transition");
            Assert.That(effect.RequestExit, Is.False);
        }

        [Test]
        public void AnUnexpectedEditModeWhileReadyFailsClosed()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = SessionTestSupport.ReadyMachine(out config);

            SessionEventEffect effect = PlayModeSessionEvents.Apply(
                machine, SessionPlayModeChange.EnteredEditMode, SessionTestSupport.T0.AddSeconds(5));

            Assert.That(effect.RequestExit, Is.True);
            Assert.That(effect.ExitFailed, Is.True);
            Assert.That(machine.HostPhase, Is.EqualTo(PlayModeSessionPhase.Failed));
            Assert.That(machine.Error, Is.EqualTo(PlayModeSessionEvents.UnexpectedPlayModeExitReason));
        }

        [Test]
        public void AnUnexpectedEditModeDuringTheInitialStartFailsClosed()
        {
            SessionConfig config = SessionTestSupport.Config();
            var machine = new PlayModeSessionMachine(config);

            SessionEventEffect effect = PlayModeSessionEvents.Apply(
                machine, SessionPlayModeChange.EnteredEditMode, SessionTestSupport.T0);

            Assert.That(effect.RequestExit, Is.True);
            Assert.That(effect.ExitFailed, Is.True);
            Assert.That(machine.HostPhase, Is.EqualTo(PlayModeSessionPhase.Failed));
        }

        [Test]
        public void AnExitingPlayModeOnlyPersistsTheCurrentState()
        {
            SessionConfig config;
            PlayModeSessionMachine machine = SessionTestSupport.ReadyMachine(out config);
            string generation = machine.Generation;
            int revision = machine.Revision;

            SessionEventEffect effect = PlayModeSessionEvents.Apply(
                machine, SessionPlayModeChange.ExitingPlayMode, SessionTestSupport.T0.AddSeconds(1));

            Assert.That(effect.Persist, Is.True);
            Assert.That(effect.Publish, Is.False, "the transition itself is not a mailbox state change");
            Assert.That(effect.RequestExit, Is.False);
            Assert.That(machine.Revision, Is.EqualTo(revision));
            Assert.That(machine.Generation, Is.EqualTo(generation));
        }

        [Test]
        public void AnEnteredPlayModeWithNoTicketIsNotARestart()
        {
            SessionConfig config = SessionTestSupport.Config();
            var machine = new PlayModeSessionMachine(config);

            SessionEventEffect effect = PlayModeSessionEvents.Apply(
                machine, SessionPlayModeChange.EnteredPlayMode, SessionTestSupport.T0);

            Assert.That(effect.Persist, Is.False);
            Assert.That(effect.Publish, Is.False);
            Assert.That(effect.RequestExit, Is.False);
            Assert.That(machine.Ticket, Is.Null);
        }

        /// <summary>
        /// The state a restarted ticket has when the Editor leaves Play Mode: persisted and then read
        /// back, as the controller does after a domain reload. This is the state the event can arrive in
        /// before the first update.
        /// </summary>
        private static PlayModeSessionMachine ReloadedRestart(out SessionConfig config)
        {
            PlayModeSessionMachine ready = SessionTestSupport.ReadyMachine(out config);
            RestartRequestRecord request = SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: ready.Generation);
            string reason;
            Assert.That(
                ready.TryAccept(request, SessionTestSupport.T0, out reason),
                Is.EqualTo(RestartAcceptance.Accepted),
                reason);
            Assert.That(ready.MarkBridgeStopped(SessionTestSupport.T0.AddSeconds(1)), Is.True,
                ready.LastRejectedTransition);

            string saved = SessionPersistence.Save(ready, ControlRoot);
            return SessionPersistence.Restore(saved, config, ControlRoot);
        }
    }
}
