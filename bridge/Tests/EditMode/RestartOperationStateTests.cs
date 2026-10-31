// SPDX-License-Identifier: Apache-2.0
using System;
using NUnit.Framework;
using Playtest.Bridge.Editor.Session;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The host's transition rules. S1 selects a restart; this machine only executes the one ticket it
    /// was given. These tests pin the refusals as hard as the success: no retry, no inferred readiness,
    /// no late transition turning a cancelled or failed ticket into a success, and one mailbox-visible
    /// revision per transition so a poll that changes nothing publishes nothing.
    /// </summary>
    [TestFixture]
    public sealed class RestartOperationStateTests
    {
        private SessionConfig _config;
        private PlayModeSessionMachine _ready;
        private string _generation;

        [SetUp]
        public void SetUp()
        {
            _ready = SessionTestSupport.ReadyMachine(out _config);
            _generation = _ready.Generation;
            Assert.That(_generation, Is.Not.Null);
        }

        [Test]
        public void TheInitialReadyNeedsAMeasuredGenerationAndScene()
        {
            var machine = new PlayModeSessionMachine(_config);
            Assert.That(machine.HostPhase, Is.EqualTo(PlayModeSessionPhase.Starting));
            Assert.That(machine.Revision, Is.EqualTo(0));

            Assert.That(machine.MarkInitialReady(SessionTestSupport.T0), Is.False,
                "ready without a measured scene must be refused");
            Assert.That(machine.LastRejectedTransition, Does.Contain("scene"));

            machine.MarkInitialSceneLoaded(SessionTestSupport.ScenePath, 1);
            Assert.That(machine.MarkInitialReady(SessionTestSupport.T0), Is.False,
                "ready without a measured generation must be refused");

            machine.MintGenerationForPlayInstance();
            Assert.That(machine.MarkInitialReady(SessionTestSupport.T0), Is.True,
                machine.LastRejectedTransition);
        }

        [Test]
        public void TheInitialReadyIsOneTransitionAndMintsOneGeneration()
        {
            var machine = new PlayModeSessionMachine(_config);
            string first = machine.MintGenerationForPlayInstance();
            string second = machine.MintGenerationForPlayInstance();

            Assert.That(second, Is.EqualTo(first), "a play instance has exactly one generation");
            Assert.That(machine.Revision, Is.EqualTo(0), "minting is not a mailbox-visible change");
            Assert.That(machine.MarkInitialSceneLoaded(SessionTestSupport.ScenePath, 3), Is.True);
            Assert.That(machine.Revision, Is.EqualTo(0), "the initial scene is not published on its own");
            Assert.That(machine.MarkInitialReady(SessionTestSupport.T0), Is.True);
            Assert.That(machine.Revision, Is.EqualTo(1));
            Assert.That(machine.HostPhase, Is.EqualTo(PlayModeSessionPhase.Ready));
            Assert.That(machine.ReadyUtc, Is.EqualTo(SessionTestSupport.T0));
        }

        [Test]
        public void TheInitialReadyFailsClosedAtTheHostDeadline()
        {
            SessionConfig config = SessionTestSupport.Config(SessionTestSupport.T0.AddSeconds(10));
            var machine = new PlayModeSessionMachine(config);
            machine.MintGenerationForPlayInstance();
            machine.MarkInitialSceneLoaded(SessionTestSupport.ScenePath, 1);

            Assert.That(machine.MarkInitialReady(SessionTestSupport.T0.AddSeconds(11)), Is.False);
            Assert.That(machine.HostPhase, Is.EqualTo(PlayModeSessionPhase.Failed));
            Assert.That(machine.Error, Is.EqualTo(PlayModeSessionMachine.ReasonHostDeadline));
        }

        [Test]
        public void OneTicketGoesFromAcceptedToReadyThroughEveryObservedState()
        {
            RestartRequestRecord request = SessionTestSupport.ParseRequest(
                SessionTestSupport.Request(SessionTestSupport.T0, fromGeneration: _generation));

            int revision = _ready.Revision;
            Assert.That(_ready.TryAccept(request, SessionTestSupport.T0, out string error), Is.EqualTo(RestartAcceptance.Accepted), error);
            Assert.That(_ready.Revision, Is.EqualTo(revision + 1));
            Assert.That(_ready.HostPhase, Is.EqualTo(PlayModeSessionPhase.Starting));
            Assert.That(_ready.Generation, Is.Null, "the old play instance's generation ends with it");
            Assert.That(_ready.Ticket.Phase, Is.EqualTo(RestartPhase.Accepted));
            Assert.That(_ready.Ticket.ToResult(SessionTestSupport.RunId).Phase, Is.EqualTo(RestartPhase.Accepted));

            revision = _ready.Revision;
            Assert.That(_ready.MarkBridgeStopped(SessionTestSupport.T0.AddMilliseconds(100)), Is.True,
                _ready.LastRejectedTransition);
            Assert.That(_ready.Revision, Is.EqualTo(revision + 1));
            Assert.That(_ready.Ticket.ToResult(SessionTestSupport.RunId).Phase, Is.EqualTo(RestartPhase.Stopped));

            revision = _ready.Revision;
            Assert.That(_ready.MarkEditModeEntered(SessionTestSupport.T0.AddSeconds(1)),
                Is.EqualTo(EditModeOutcome.RestartPreparing));
            Assert.That(_ready.Revision, Is.EqualTo(revision + 1));
            RestartResultRecord starting = _ready.Ticket.ToResult(SessionTestSupport.RunId);
            Assert.That(starting.Phase, Is.EqualTo(RestartPhase.Starting));
            Assert.That(starting.EnteredEditModeUtc, Is.EqualTo(SessionTestSupport.T0.AddSeconds(1)));
            Assert.That(starting.EnteredPlayModeUtc, Is.Null);

            revision = _ready.Revision;
            Assert.That(_ready.MarkPlayModeEntered(SessionTestSupport.T0.AddSeconds(2)), Is.True,
                _ready.LastRejectedTransition);
            Assert.That(_ready.Revision, Is.EqualTo(revision + 1));

            revision = _ready.Revision;
            Assert.That(_ready.MarkSceneLoaded(SessionTestSupport.ScenePath, 11), Is.True,
                _ready.LastRejectedTransition);
            Assert.That(_ready.Revision, Is.EqualTo(revision), "the scene is published with readiness, not before it");

            string fresh = _ready.MintGenerationForPlayInstance();
            Assert.That(fresh, Is.Not.EqualTo(_generation), "the new play instance never reuses the old generation");
            Assert.That(_ready.Revision, Is.EqualTo(revision), "minting is not published on its own");

            revision = _ready.Revision;
            Assert.That(_ready.MarkBridgeStarted(fresh, SessionTestSupport.T0.AddSeconds(3)), Is.True,
                _ready.LastRejectedTransition);
            Assert.That(_ready.Revision, Is.EqualTo(revision + 1));
            Assert.That(_ready.HostPhase, Is.EqualTo(PlayModeSessionPhase.Ready));

            RestartResultRecord ready = _ready.Ticket.ToResult(SessionTestSupport.RunId);
            Assert.That(ready.Phase, Is.EqualTo(RestartPhase.Ready));
            Assert.That(ready.FromGeneration, Is.EqualTo(_generation));
            Assert.That(ready.ToGeneration, Is.EqualTo(fresh));
            Assert.That(ready.ScenePath, Is.EqualTo(SessionTestSupport.ScenePath));
            Assert.That(ready.SceneHandle, Is.EqualTo(11));
            Assert.That(ready.FailureReason, Is.Null);
            Assert.That(_ready.LastCompletedTicketId, Is.EqualTo(SessionTestSupport.TicketId));
        }

        [Test]
        public void ReadinessIsRefusedWithoutBothObservedTransitions()
        {
            AcceptTicket();
            _ready.MarkBridgeStopped(SessionTestSupport.T0);
            Assert.That(_ready.MarkBridgeStarted(SessionTestSupport.OtherGeneration, SessionTestSupport.T0), Is.False,
                "no bridge may report ready before the new play instance was observed");
            Assert.That(_ready.LastRejectedTransition, Is.Not.Null);

            _ready.MarkEditModeEntered(SessionTestSupport.T0);
            string fresh = _ready.MintGenerationForPlayInstance();
            _ready.MarkSceneLoaded(SessionTestSupport.ScenePath, 2);
            Assert.That(_ready.MarkBridgeStarted(fresh, SessionTestSupport.T0), Is.False,
                "ready without an observed EnteredPlayMode must be refused");
            Assert.That(_ready.Ticket.Phase, Is.Not.EqualTo(RestartPhase.Ready));
        }

        [Test]
        public void ReadinessIsRefusedWithoutALoadedScene()
        {
            AcceptTicket();
            _ready.MarkBridgeStopped(SessionTestSupport.T0);
            _ready.MarkEditModeEntered(SessionTestSupport.T0);
            _ready.MarkPlayModeEntered(SessionTestSupport.T0);

            string fresh = _ready.MintGenerationForPlayInstance();
            Assert.That(_ready.MarkBridgeStarted(fresh, SessionTestSupport.T0), Is.False,
                "a bridge alone is not a new play instance");
            Assert.That(_ready.LastRejectedTransition, Does.Contain("scene"));
        }

        [Test]
        public void ReadinessIsRefusedForTheWrongOrReusedGeneration()
        {
            AcceptTicket();
            _ready.MarkBridgeStopped(SessionTestSupport.T0);
            _ready.MarkEditModeEntered(SessionTestSupport.T0);
            _ready.MarkPlayModeEntered(SessionTestSupport.T0);
            _ready.MarkSceneLoaded(SessionTestSupport.ScenePath, 4);

            Assert.That(_ready.MarkBridgeStarted("not-a-uuid", SessionTestSupport.T0), Is.False);
            Assert.That(_ready.LastRejectedTransition, Does.Contain("non-canonical"));

            Assert.That(_ready.MarkBridgeStarted(SessionTestSupport.OtherGeneration, SessionTestSupport.T0), Is.False,
                "a generation this instance did not mint cannot make it ready");
            Assert.That(_ready.LastRejectedTransition, Does.Contain("not this instance"));
            Assert.That(_ready.Ticket.Phase, Is.EqualTo(RestartPhase.Starting),
                "the refused bridge never published readiness");
        }

        [Test]
        public void AnEventOutOfOrderIsRejectedAndDoesNotPublish()
        {
            AcceptTicket();
            int revision = _ready.Revision;

            Assert.That(_ready.MarkEditModeEntered(SessionTestSupport.T0), Is.EqualTo(EditModeOutcome.Ignored),
                "edit mode before the bridge released is not this ticket's transition");
            Assert.That(_ready.Revision, Is.EqualTo(revision));
            Assert.That(_ready.Ticket.Phase, Is.EqualTo(RestartPhase.Accepted));

            Assert.That(_ready.MarkPlayModeEntered(SessionTestSupport.T0), Is.False,
                "play mode before edit mode is not a restart");
            Assert.That(_ready.Revision, Is.EqualTo(revision));
            Assert.That(_ready.MarkSceneLoaded(SessionTestSupport.ScenePath, 1), Is.False,
                "a scene before play mode is not this ticket's fresh scene");
            Assert.That(_ready.Revision, Is.EqualTo(revision));
        }

        [Test]
        public void ASecondTicketWhileOneIsInFlightIsBusy()
        {
            AcceptTicket();
            int revision = _ready.Revision;

            RestartRequestRecord second = SessionTestSupport.Request(
                SessionTestSupport.T0, ticketId: "ticket-2", fromGeneration: SessionTestSupport.OtherGeneration);
            Assert.That(_ready.TryAccept(second, SessionTestSupport.T0, out string error),
                Is.EqualTo(RestartAcceptance.Busy), error);
            Assert.That(_ready.Revision, Is.EqualTo(revision));
            Assert.That(_ready.Ticket.TicketId, Is.EqualTo(SessionTestSupport.TicketId));
        }

        [Test]
        public void ARepeatedTicketIsRefusedInFlightAndAfterCompletion()
        {
            AcceptTicket();
            RestartRequestRecord same = SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: _generation);

            Assert.That(_ready.TryAccept(same, SessionTestSupport.T0, out string error),
                Is.EqualTo(RestartAcceptance.Duplicate), error);

            CompleteTicket();

            Assert.That(_ready.TryAccept(same, SessionTestSupport.T0.AddSeconds(5), out error),
                Is.EqualTo(RestartAcceptance.Duplicate), error);
            Assert.That(_ready.HostPhase, Is.EqualTo(PlayModeSessionPhase.Ready),
                "a duplicate never disturbs the session it cannot join");
        }

        [Test]
        public void ForeignStaleExpiredAndEarlyRequestsAreRefusedBeforeDispatch()
        {
            RestartRequestRecord foreign = SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: _generation, runId: "another-run");
            Assert.That(_ready.TryAccept(foreign, SessionTestSupport.T0, out string error),
                Is.EqualTo(RestartAcceptance.ForeignRun), error);

            RestartRequestRecord stale = SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: SessionTestSupport.OtherGeneration);
            Assert.That(_ready.TryAccept(stale, SessionTestSupport.T0, out error),
                Is.EqualTo(RestartAcceptance.StaleGeneration), error);
            Assert.That(error, Does.Contain(_generation));

            RestartRequestRecord expired = SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: _generation, deadline: TimeSpan.FromSeconds(-1));
            Assert.That(_ready.TryAccept(expired, SessionTestSupport.T0, out error),
                Is.EqualTo(RestartAcceptance.DeadlineExpired), error);

            var starting = new PlayModeSessionMachine(_config);
            starting.MintGenerationForPlayInstance();
            RestartRequestRecord early = SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: starting.Generation);
            Assert.That(starting.TryAccept(early, SessionTestSupport.T0, out error),
                Is.EqualTo(RestartAcceptance.NotReady), error);

            Assert.That(_ready.TryAccept(null, SessionTestSupport.T0, out error),
                Is.EqualTo(RestartAcceptance.NotReady));
            Assert.That(_ready.Ticket, Is.Null, "a refused request never becomes a ticket");
            Assert.That(_ready.Revision, Is.EqualTo(1), "no refusal publishes a transition");
        }

        [Test]
        public void TheTicketDeadlineIsTheSmallerOfTheOperationAndTheHost()
        {
            AcceptTicket(deadline: TimeSpan.FromSeconds(20));
            Assert.That(_ready.DeadlineForTicket, Is.EqualTo(SessionTestSupport.T0.AddSeconds(20)),
                "a short operation deadline wins over the host's later one");

            var later = SessionTestSupport.ReadyMachine(out SessionConfig config, SessionTestSupport.T0);
            RestartRequestRecord longRequest = SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: later.Generation, deadline: TimeSpan.FromMinutes(30));
            Assert.That(later.TryAccept(longRequest, SessionTestSupport.T0, out string error),
                Is.EqualTo(RestartAcceptance.Accepted), error);
            Assert.That(later.DeadlineForTicket, Is.EqualTo(config.DeadlineUtc));
        }

        [Test]
        public void AnOperationDeadlineFailsTheTicketClosed()
        {
            AcceptTicket(deadline: TimeSpan.FromSeconds(10));
            int revision = _ready.Revision;

            Assert.That(_ready.MarkBridgeStopped(SessionTestSupport.T0.AddSeconds(11)), Is.False);
            Assert.That(_ready.Ticket.Phase, Is.EqualTo(RestartPhase.Failed));
            Assert.That(_ready.Ticket.FailureReason, Is.EqualTo(PlayModeSessionMachine.ReasonOperationDeadline));
            Assert.That(_ready.HostPhase, Is.EqualTo(PlayModeSessionPhase.Failed));
            Assert.That(_ready.Revision, Is.EqualTo(revision + 1), "a timeout publishes once");

            RestartResultRecord result = _ready.Ticket.ToResult(SessionTestSupport.RunId);
            Assert.That(result.Phase, Is.EqualTo(RestartPhase.Failed));
            Assert.That(result.FailureReason, Is.EqualTo(PlayModeSessionMachine.ReasonOperationDeadline));
            Assert.That(result.ToGeneration, Is.Null);
        }

        [Test]
        public void AHostDeadlineFailsTheSessionClosed()
        {
            SessionConfig config = SessionTestSupport.Config(SessionTestSupport.T0.AddSeconds(10));
            var machine = new PlayModeSessionMachine(config);
            machine.MintGenerationForPlayInstance();
            machine.MarkInitialSceneLoaded(SessionTestSupport.ScenePath, 1);
            machine.MarkInitialReady(SessionTestSupport.T0);

            RestartRequestRecord request = SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: machine.Generation, deadline: TimeSpan.FromMinutes(5));
            Assert.That(machine.TryAccept(request, SessionTestSupport.T0, out string error),
                Is.EqualTo(RestartAcceptance.Accepted), error);

            Assert.That(machine.ExpireIfNeeded(SessionTestSupport.T0.AddSeconds(11)), Is.True);
            Assert.That(machine.Ticket.FailureReason, Is.EqualTo(PlayModeSessionMachine.ReasonHostDeadline));
            Assert.That(machine.HostPhase, Is.EqualTo(PlayModeSessionPhase.Failed));
        }

        [Test]
        public void AnInitialStartupPastTheHostDeadlineFailsWithoutATicket()
        {
            SessionConfig config = SessionTestSupport.Config(SessionTestSupport.T0.AddSeconds(10));
            var machine = new PlayModeSessionMachine(config);

            Assert.That(machine.ExpireIfNeeded(SessionTestSupport.T0.AddSeconds(11)), Is.True);
            Assert.That(machine.HostPhase, Is.EqualTo(PlayModeSessionPhase.Failed));
            Assert.That(machine.Error, Is.EqualTo(PlayModeSessionMachine.ReasonHostDeadline));
            Assert.That(machine.Ticket, Is.Null);
        }

        [Test]
        public void AFailedSessionNeverAcceptsAnotherTicket()
        {
            AcceptTicket(deadline: TimeSpan.FromSeconds(10));
            _ready.MarkBridgeStopped(SessionTestSupport.T0.AddSeconds(11));
            int revision = _ready.Revision;

            RestartRequestRecord later = SessionTestSupport.Request(
                SessionTestSupport.T0.AddSeconds(12), ticketId: "ticket-2", fromGeneration: _generation);
            Assert.That(_ready.TryAccept(later, SessionTestSupport.T0.AddSeconds(12), out string error),
                Is.EqualTo(RestartAcceptance.NotReady), error);
            Assert.That(_ready.Revision, Is.EqualTo(revision));
            Assert.That(_ready.IsClosed, Is.True);
            Assert.That(_ready.CanAccept, Is.False);
        }

        [Test]
        public void ACancelledTicketCannotBeCompletedByALateTransition()
        {
            AcceptTicket();
            _ready.MarkBridgeStopped(SessionTestSupport.T0);
            _ready.MarkEditModeEntered(SessionTestSupport.T0.AddSeconds(1));

            Assert.That(_ready.Cancel(SessionTestSupport.T0.AddSeconds(2)), Is.True);
            Assert.That(_ready.Ticket.Phase, Is.EqualTo(RestartPhase.Cancelled));
            Assert.That(_ready.Ticket.FailureReason, Is.EqualTo(PlayModeSessionMachine.ReasonCancelled));
            Assert.That(_ready.HostPhase, Is.EqualTo(PlayModeSessionPhase.Failed));

            long ignored = _ready.IgnoredLateTransitions;
            Assert.That(_ready.MarkPlayModeEntered(SessionTestSupport.T0.AddSeconds(3)), Is.False);
            Assert.That(_ready.MarkSceneLoaded(SessionTestSupport.ScenePath, 9), Is.False);
            Assert.That(_ready.MarkBridgeStarted(SessionTestSupport.OtherGeneration, SessionTestSupport.T0.AddSeconds(3)),
                Is.False);
            Assert.That(_ready.IgnoredLateTransitions, Is.GreaterThan(ignored));

            RestartResultRecord result = _ready.Ticket.ToResult(SessionTestSupport.RunId);
            Assert.That(result.Phase, Is.EqualTo(RestartPhase.Cancelled));
            Assert.That(result.FailureReason, Is.EqualTo(PlayModeSessionMachine.ReasonCancelled));
            Assert.That(result.ToGeneration, Is.Null);
            Assert.That(result.ReadyUtc, Is.Null);
        }

        [Test]
        public void ACancelNeedsATicketInFlight()
        {
            Assert.That(_ready.Cancel(SessionTestSupport.T0), Is.False);
            Assert.That(_ready.HostPhase, Is.EqualTo(PlayModeSessionPhase.Ready),
                "cancelling nothing is not a reason to fail the session");
        }

        [Test]
        public void AnUnexpectedEditModeEndsTheInstanceAndDropsItsGeneration()
        {
            Assert.That(_ready.MarkEditModeEntered(SessionTestSupport.T0), Is.EqualTo(EditModeOutcome.InstanceCleared));
            Assert.That(_ready.Generation, Is.Null);
            Assert.That(_ready.ScenePath, Is.Null);
            Assert.That(_ready.SceneHandle, Is.Null);
            Assert.That(_ready.ReadyUtc, Is.Null);

            string fresh = _ready.MintGenerationForPlayInstance();
            Assert.That(fresh, Is.Not.EqualTo(_generation), "the same play instance is never served twice");
        }

        [Test]
        public void ASupervisedShutdownIsACleanStop()
        {
            _ready.BeginStop();
            Assert.That(_ready.HostPhase, Is.EqualTo(PlayModeSessionPhase.Stopping));
            Assert.That(_ready.IsClosed, Is.False, "stopping is still a host that may report its stop");

            _ready.MarkStopped();
            Assert.That(_ready.HostPhase, Is.EqualTo(PlayModeSessionPhase.Stopped));
            Assert.That(_ready.IsClosed, Is.True);
            Assert.That(_ready.Error, Is.Null, "a clean stop is not a failure");
            Assert.That(_ready.CanAccept, Is.False);
        }

        private void AcceptTicket(TimeSpan? deadline = null)
        {
            RestartRequestRecord request = SessionTestSupport.Request(
                SessionTestSupport.T0, fromGeneration: _generation, deadline: deadline);
            Assert.That(_ready.TryAccept(request, SessionTestSupport.T0, out string error),
                Is.EqualTo(RestartAcceptance.Accepted), error);
        }

        private void CompleteTicket()
        {
            _ready.MarkBridgeStopped(SessionTestSupport.T0);
            _ready.MarkEditModeEntered(SessionTestSupport.T0.AddSeconds(1));
            _ready.MarkPlayModeEntered(SessionTestSupport.T0.AddSeconds(2));
            _ready.MarkSceneLoaded(SessionTestSupport.ScenePath, 5);
            string fresh = _ready.MintGenerationForPlayInstance();
            Assert.That(_ready.MarkBridgeStarted(fresh, SessionTestSupport.T0.AddSeconds(3)), Is.True,
                _ready.LastRejectedTransition);
        }
    }
}
