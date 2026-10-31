// SPDX-License-Identifier: Apache-2.0
using System;
using Playtest.Bridge.Transport;

namespace Playtest.Bridge.Editor.Session
{
    /// <summary>Why a validated request was or was not dispatched. Only <see cref="Accepted"/> transitions.</summary>
    public enum RestartAcceptance
    {
        Accepted,
        ForeignRun,
        Duplicate,
        Busy,
        NotReady,
        StaleGeneration,
        DeadlineExpired,
    }

    /// <summary>What an observed edit-mode entry means for the machine.</summary>
    public enum EditModeOutcome
    {
        Ignored,
        RestartPreparing,
        InstanceCleared,
    }

    /// <summary>One explicit restart ticket, from acceptance to a terminal phase.</summary>
    public sealed class RestartTicket
    {
        public RestartTicket(string ticketId, string fromGeneration, DateTime requestDeadlineUtc)
        {
            TicketId = ticketId;
            FromGeneration = fromGeneration;
            RequestDeadlineUtc = requestDeadlineUtc;
        }

        public string TicketId { get; }

        public string FromGeneration { get; }

        public DateTime RequestDeadlineUtc { get; }

        public RestartPhase Phase { get; internal set; } = RestartPhase.Accepted;

        public DateTime? EnteredEditModeUtc { get; internal set; }

        public DateTime? EnteredPlayModeUtc { get; internal set; }

        public DateTime? ReadyUtc { get; internal set; }

        public string ToGeneration { get; internal set; }

        public string ScenePath { get; internal set; }

        public int? SceneHandle { get; internal set; }

        public string FailureReason { get; internal set; }

        public bool IsTerminal
        {
            get
            {
                return Phase == RestartPhase.Ready
                    || Phase == RestartPhase.Failed
                    || Phase == RestartPhase.Cancelled;
            }
        }

        /// <summary>The result body for this ticket. Ready is refused unless every value was observed.</summary>
        public RestartResultRecord ToResult(string runId)
        {
            var result = new RestartResultRecord
            {
                RunId = runId,
                TicketId = TicketId,
                Phase = Phase,
                FromGeneration = FromGeneration,
                ToGeneration = ToGeneration,
                EnteredEditModeUtc = EnteredEditModeUtc,
                EnteredPlayModeUtc = EnteredPlayModeUtc,
                ReadyUtc = ReadyUtc,
                ScenePath = ScenePath,
                SceneHandle = SceneHandle,
                FailureReason = FailureReason,
            };
            result.Validate();
            return result;
        }
    }

    /// <summary>
    /// The host's transition rules, with no Unity API and no clock of its own: every call supplies the
    /// observed instant. The controller drives it; this type decides only what the agreed mailbox
    /// permits. It never selects a restart, never retries, and never turns a late or partial transition
    /// into readiness.
    ///
    /// <see cref="Revision"/> advances exactly once per mailbox-visible change, so the controller
    /// publishes one record per transition rather than one per poll.
    /// </summary>
    public sealed class PlayModeSessionMachine
    {
        public const string ReasonOperationDeadline = "operation_deadline";
        public const string ReasonHostDeadline = "host_deadline";
        public const string ReasonCancelled = "cancelled";

        private int _revision;
        private long _ignoredLateTransitions;

        public PlayModeSessionMachine(SessionConfig config)
        {
            Config = config ?? throw new ArgumentNullException(nameof(config));
            HostPhase = PlayModeSessionPhase.Starting;
        }

        public SessionConfig Config { get; }

        public PlayModeSessionPhase HostPhase { get; internal set; }

        /// <summary>The current play instance's generation, or null between instances.</summary>
        public string Generation { get; internal set; }

        public string ScenePath { get; internal set; }

        public int? SceneHandle { get; internal set; }

        public DateTime? ReadyUtc { get; internal set; }

        public string Error { get; internal set; }

        public RestartTicket Ticket { get; internal set; }

        /// <summary>Advances once per mailbox-visible change; the controller publishes on a change only.</summary>
        public int Revision
        {
            get { return _revision; }
        }

        /// <summary>Late events for a cancelled, failed or already ready ticket; kept for tests and diagnostics.</summary>
        public long IgnoredLateTransitions
        {
            get { return _ignoredLateTransitions; }
        }

        public string LastRejectedTransition { get; private set; }

        /// <summary>The most recent ticket that reached a terminal phase; duplicate requests are refused.</summary>
        public string LastCompletedTicketId { get; internal set; }

        public bool IsClosed
        {
            get { return HostPhase == PlayModeSessionPhase.Failed || HostPhase == PlayModeSessionPhase.Stopped; }
        }

        public bool CanAccept
        {
            get
            {
                return !IsClosed
                    && HostPhase == PlayModeSessionPhase.Ready
                    && (Ticket == null || Ticket.IsTerminal);
            }
        }

        /// <summary>The smaller of the operation's own deadline and the host's global deadline.</summary>
        public DateTime DeadlineForTicket
        {
            get
            {
                if (Ticket == null)
                {
                    return Config.DeadlineUtc;
                }

                return Ticket.RequestDeadlineUtc < Config.DeadlineUtc
                    ? Ticket.RequestDeadlineUtc
                    : Config.DeadlineUtc;
            }
        }

        /// <summary>
        /// Validates one request against this run, the current generation and the ticket already in
        /// flight. It never repairs a stale request: a rejection is final and publishes no transition.
        /// </summary>
        public RestartAcceptance TryAccept(RestartRequestRecord request, DateTime nowUtc, out string error)
        {
            error = null;
            if (request == null)
            {
                error = "request is null";
                return RestartAcceptance.NotReady;
            }

            if (!string.Equals(request.RunId, Config.RunId, StringComparison.Ordinal))
            {
                error = "request belongs to run '" + request.RunId + "', not '" + Config.RunId + "'";
                return RestartAcceptance.ForeignRun;
            }

            if ((Ticket != null && string.Equals(Ticket.TicketId, request.TicketId, StringComparison.Ordinal))
                || string.Equals(LastCompletedTicketId, request.TicketId, StringComparison.Ordinal))
            {
                error = "ticket '" + request.TicketId + "' was already handled";
                return RestartAcceptance.Duplicate;
            }

            if (Ticket != null && !Ticket.IsTerminal)
            {
                error = "ticket '" + Ticket.TicketId + "' is still in flight";
                return RestartAcceptance.Busy;
            }

            if (!CanAccept)
            {
                error = "the host is not ready to accept a restart (phase " + HostStateRecord.WireName(HostPhase) + ")";
                return RestartAcceptance.NotReady;
            }

            if (!string.Equals(request.FromGeneration, Generation, StringComparison.Ordinal))
            {
                error = "request starts from '" + request.FromGeneration + "', not the current '"
                    + (Generation ?? "<none>") + "'";
                return RestartAcceptance.StaleGeneration;
            }

            if (nowUtc >= request.DeadlineUtc || nowUtc >= Config.DeadlineUtc)
            {
                error = "the request deadline has passed";
                return RestartAcceptance.DeadlineExpired;
            }

            Ticket = new RestartTicket(request.TicketId, request.FromGeneration, request.DeadlineUtc);
            HostPhase = PlayModeSessionPhase.Starting;
            ClearPlayInstance();
            Changed();
            return RestartAcceptance.Accepted;
        }

        /// <summary>
        /// The bridge (and its lock, token and capture) has been released for this ticket. The old play
        /// instance is gone; its generation must never be reused for the new one.
        /// </summary>
        public bool MarkBridgeStopped(DateTime nowUtc)
        {
            if (Ticket == null)
            {
                return Reject("no ticket");
            }

            if (Ticket.IsTerminal)
            {
                _ignoredLateTransitions++;
                return Reject("ticket is terminal");
            }

            if (Ticket.Phase != RestartPhase.Accepted)
            {
                return Reject("bridge stopped out of order");
            }

            if (ExpireIfNeeded(nowUtc))
            {
                return false;
            }

            Ticket.Phase = RestartPhase.Stopped;
            Changed();
            return true;
        }

        /// <summary>
        /// An observed EnteredEditMode. Valid only after the bridge release for this ticket; a late one
        /// for a terminal ticket is ignored and can never publish readiness.
        /// </summary>
        public EditModeOutcome MarkEditModeEntered(DateTime nowUtc)
        {
            if (Ticket == null || Ticket.IsTerminal)
            {
                if (Ticket != null)
                {
                    _ignoredLateTransitions++;
                }

                ClearPlayInstance();
                return EditModeOutcome.InstanceCleared;
            }

            if (Ticket.Phase != RestartPhase.Stopped)
            {
                _ignoredLateTransitions++;
                Reject("edit mode entered before the bridge released");
                return EditModeOutcome.Ignored;
            }

            Ticket.EnteredEditModeUtc = nowUtc;
            Ticket.Phase = RestartPhase.Starting;
            ClearPlayInstance();
            Changed();
            ExpireIfNeeded(nowUtc);
            return EditModeOutcome.RestartPreparing;
        }

        /// <summary>An observed EnteredPlayMode for this ticket. It must follow its edit-mode observation.</summary>
        public bool MarkPlayModeEntered(DateTime nowUtc)
        {
            if (Ticket == null)
            {
                return false;
            }

            if (Ticket.IsTerminal)
            {
                _ignoredLateTransitions++;
                return Reject("ticket is terminal");
            }

            if (Ticket.Phase != RestartPhase.Starting || Ticket.EnteredEditModeUtc == null)
            {
                return Reject("play mode entered before edit mode was observed");
            }

            if (Ticket.EnteredPlayModeUtc != null)
            {
                return Reject("play mode was already observed for this ticket");
            }

            Ticket.EnteredPlayModeUtc = nowUtc;
            Changed();
            ExpireIfNeeded(nowUtc);
            return true;
        }

        /// <summary>Records the fresh play-mode scene instance. Not mailbox-visible until ready.</summary>
        public bool MarkSceneLoaded(string scenePath, int sceneHandle)
        {
            if (Ticket == null || Ticket.IsTerminal)
            {
                return Reject("no active ticket");
            }

            if (Ticket.Phase != RestartPhase.Starting || Ticket.EnteredPlayModeUtc == null)
            {
                return Reject("scene loaded before play mode was observed");
            }

            if (string.IsNullOrWhiteSpace(scenePath) || sceneHandle < 0)
            {
                return Reject("scene path or handle is not measured");
            }

            ScenePath = scenePath;
            SceneHandle = sceneHandle;
            return true;
        }

        /// <summary>Records the same fresh scene for the initial start, where no ticket exists but the same rule holds.</summary>
        public bool MarkInitialSceneLoaded(string scenePath, int sceneHandle)
        {
            if (Ticket != null && !Ticket.IsTerminal)
            {
                return Reject("a restart ticket is in flight");
            }

            if (HostPhase != PlayModeSessionPhase.Starting)
            {
                return Reject("the host has already left its starting phase");
            }

            if (string.IsNullOrWhiteSpace(scenePath) || sceneHandle < 0)
            {
                return Reject("scene path or handle is not measured");
            }

            ScenePath = scenePath;
            SceneHandle = sceneHandle;
            return true;
        }

        /// <summary>
        /// Mints a generation once for the current play instance. The caller persists it before starting
        /// the bridge, so a domain reload can never mint a second one for the same instance.
        /// </summary>
        public string MintGenerationForPlayInstance()
        {
            if (Generation != null)
            {
                return Generation;
            }

            string candidate = BridgeGeneration.New();
            if (Ticket != null && string.Equals(candidate, Ticket.FromGeneration, StringComparison.Ordinal))
            {
                candidate = BridgeGeneration.New();
            }

            Generation = candidate;
            return Generation;
        }

        /// <summary>A new bridge is running with a fresh token and lock for this play instance.</summary>
        public bool MarkBridgeStarted(string generation, DateTime nowUtc)
        {
            if (Ticket == null || Ticket.IsTerminal)
            {
                return Reject("no active ticket");
            }

            if (Ticket.Phase != RestartPhase.Starting)
            {
                return Reject("bridge started out of order");
            }

            if (Ticket.EnteredEditModeUtc == null || Ticket.EnteredPlayModeUtc == null)
            {
                return Reject("ready without both observed play-mode transitions");
            }

            if (ScenePath == null || SceneHandle == null)
            {
                return Reject("ready without a loaded scene");
            }

            if (!BridgeGeneration.IsCanonical(generation))
            {
                return Reject("bridge started with a non-canonical generation");
            }

            if (!string.Equals(generation, Generation, StringComparison.Ordinal))
            {
                return Reject("bridge started with a generation that is not this instance's");
            }

            if (string.Equals(generation, Ticket.FromGeneration, StringComparison.Ordinal))
            {
                return Reject("the new play instance reused the previous generation");
            }

            if (nowUtc >= DeadlineForTicket)
            {
                ExpireIfNeeded(nowUtc);
                return false;
            }

            Ticket.ScenePath = ScenePath;
            Ticket.SceneHandle = SceneHandle;
            Ticket.ToGeneration = generation;
            Ticket.ReadyUtc = nowUtc;
            Ticket.Phase = RestartPhase.Ready;
            HostPhase = PlayModeSessionPhase.Ready;
            ReadyUtc = nowUtc;
            LastCompletedTicketId = Ticket.TicketId;
            Changed();
            return true;
        }

        /// <summary>The initial start's readiness, from a measured play instance, scene, catalog and bridge.</summary>
        public bool MarkInitialReady(DateTime nowUtc)
        {
            if (Ticket != null && !Ticket.IsTerminal)
            {
                return Reject("a restart ticket is in flight");
            }

            if (HostPhase != PlayModeSessionPhase.Starting)
            {
                return Reject("the host has already left its starting phase");
            }

            if (Generation == null || ScenePath == null || SceneHandle == null)
            {
                return Reject("ready without a measured generation and scene");
            }

            if (nowUtc >= Config.DeadlineUtc)
            {
                Fail(ReasonHostDeadline, nowUtc);
                return false;
            }

            HostPhase = PlayModeSessionPhase.Ready;
            ReadyUtc = nowUtc;
            Changed();
            return true;
        }

        /// <summary>Terminal failure. Once written, no later transition may publish readiness for this ticket.</summary>
        public void Fail(string reason, DateTime nowUtc)
        {
            if (string.IsNullOrWhiteSpace(reason))
            {
                reason = "unspecified failure";
            }

            if (Ticket != null && !Ticket.IsTerminal)
            {
                Ticket.Phase = RestartPhase.Failed;
                Ticket.FailureReason = reason;
                LastCompletedTicketId = Ticket.TicketId;
                HostPhase = PlayModeSessionPhase.Failed;
                Error = reason;
                Changed();
                return;
            }

            if (HostPhase == PlayModeSessionPhase.Failed)
            {
                return;
            }

            HostPhase = PlayModeSessionPhase.Failed;
            Error = reason;
            Changed();
        }

        /// <summary>An explicit client cancellation of the in-flight ticket. It is terminal, never a late success.</summary>
        public bool Cancel(DateTime nowUtc)
        {
            if (Ticket == null || Ticket.IsTerminal)
            {
                return Reject("no active ticket to cancel");
            }

            Ticket.Phase = RestartPhase.Cancelled;
            Ticket.FailureReason = ReasonCancelled;
            LastCompletedTicketId = Ticket.TicketId;
            HostPhase = PlayModeSessionPhase.Failed;
            Error = ReasonCancelled;
            Changed();
            return true;
        }

        /// <summary>Applies the smaller operation or host deadline. Returns true when it failed the session.</summary>
        public bool ExpireIfNeeded(DateTime nowUtc)
        {
            if (Ticket != null && !Ticket.IsTerminal)
            {
                if (nowUtc >= DeadlineForTicket)
                {
                    Fail(DeadlineReason(), nowUtc);
                    return true;
                }

                return false;
            }

            if (HostPhase == PlayModeSessionPhase.Starting && nowUtc >= Config.DeadlineUtc)
            {
                Fail(ReasonHostDeadline, nowUtc);
                return true;
            }

            return false;
        }

        private string DeadlineReason()
        {
            return Ticket != null && Ticket.RequestDeadlineUtc < Config.DeadlineUtc
                ? ReasonOperationDeadline
                : ReasonHostDeadline;
        }

        /// <summary>Supervisor shutdown: a clean stop, never a failure of an operation that had none.</summary>
        public void BeginStop()
        {
            if (HostPhase == PlayModeSessionPhase.Failed)
            {
                return;
            }

            HostPhase = PlayModeSessionPhase.Stopping;
            Error = null;
            Changed();
        }

        public void MarkStopped()
        {
            if (HostPhase == PlayModeSessionPhase.Failed)
            {
                return;
            }

            HostPhase = PlayModeSessionPhase.Stopped;
            Changed();
        }

        /// <summary>An observed edit mode ends the play instance; its generation is never reused.</summary>
        public void ClearPlayInstance()
        {
            Generation = null;
            ScenePath = null;
            SceneHandle = null;
            ReadyUtc = null;
        }

        private void Changed()
        {
            _revision++;
        }

        private bool Reject(string reason)
        {
            LastRejectedTransition = reason;
            return false;
        }
    }
}
