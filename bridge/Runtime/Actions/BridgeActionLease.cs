using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Threading;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Actions
{
    /// <summary>Runs a callback once after a delay. The lease uses it for hold ends, waits and deadlines.</summary>
    public interface IBridgeScheduler
    {
        IDisposable Schedule(TimeSpan delay, Action callback);
    }

    /// <summary>The scheduler used outside tests: one thread pool timer per callback.</summary>
    public sealed class BridgeThreadingScheduler : IBridgeScheduler
    {
        public IDisposable Schedule(TimeSpan delay, Action callback)
        {
            return new Timer(_ => callback(), null, delay, Timeout.InfiniteTimeSpan);
        }
    }

    /// <summary>
    /// The second half of a two-phase start. <see cref="BridgeActionLease.TryBegin"/> validates and
    /// registers the act without waiting on the device, so a caller can hold its own gate for it.
    /// <see cref="Execute"/> then supersedes the previous action and applies the inputs, which waits
    /// for the Unity main thread, so it must run outside any lock the main thread needs.
    /// </summary>
    public sealed class BridgeLeaseStart
    {
        private readonly BridgeActionLease _lease;
        private readonly object _run;
        private int _executed;

        internal BridgeLeaseStart(BridgeActionLease lease, object run)
        {
            _lease = lease;
            _run = run;
        }

        /// <summary>
        /// Returns false with an error, and without reporting anything for the act, only when no
        /// input reached a device: the caller then rejects the act. Returns true once the act is
        /// running, when a stop or newer act already took it, and when an input that may have reached
        /// a device failed to apply, in which case the lease released it and reported the execution
        /// failed with the receipts it has.
        /// </summary>
        public bool Execute(out string error)
        {
            if (Interlocked.Exchange(ref _executed, 1) != 0)
            {
                error = "this start was already executed";
                return false;
            }

            return _lease.Execute(_run, out error);
        }
    }

    /// <summary>
    /// Owns the one running action or skill. A new act releases the previous one, reports it
    /// cancelled, applies its own inputs and reports <c>running</c>; the run then ends as
    /// <c>completed</c>, or is cancelled by a newer act or a stop, or fails. An action holds its
    /// inputs for the lease. A skill holds each step's inputs for its hold, releases and verifies
    /// them, waits, and only then applies the next step, so steps never overlap and none is skipped.
    ///
    /// The lease never chooses an action: it executes the one it is given and reports measured
    /// timing and per-input receipts. The first failed apply or unverified release stops the run
    /// with its inputs released; a stopped, superseded or expired run is never resumed or replayed.
    /// No lock is held while waiting on the input backend, because the backend's work runs on the
    /// Unity main thread and the main thread may itself be calling <see cref="Stop"/>.
    /// </summary>
    public sealed class BridgeActionLease : IDisposable
    {
        public const double MaxLeaseSeconds = BridgeInputVocabulary.MaxHoldSeconds;

        private const int FinalizeWaitMs = 3000;

        private readonly IBridgeInputBackend _backend;
        private readonly Action<ExecutionRecordMessage> _emit;
        private readonly Func<DateTime> _utcNow;
        private readonly Action<string> _log;
        private readonly IBridgeScheduler _scheduler;
        private readonly object _state = new object();
        private Run _active;
        private Run _finalizing;
        private long _epoch;
        private bool _disposed;

        public BridgeActionLease(
            IBridgeInputBackend backend,
            Action<ExecutionRecordMessage> emit,
            Action<string> log = null,
            Func<DateTime> utcNow = null,
            IBridgeScheduler scheduler = null)
        {
            _backend = backend ?? throw new ArgumentNullException(nameof(backend));
            _emit = emit ?? throw new ArgumentNullException(nameof(emit));
            _log = log;
            _utcNow = utcNow ?? (() => DateTime.UtcNow);
            _scheduler = scheduler ?? new BridgeThreadingScheduler();
        }

        public bool IsActive
        {
            get
            {
                lock (_state)
                {
                    return _active != null;
                }
            }
        }

        public string ActiveActionId
        {
            get
            {
                lock (_state)
                {
                    return _active?.Request.ActionId;
                }
            }
        }

        /// <summary>
        /// Starts a validated action or skill: <see cref="TryBegin"/> then <see cref="BridgeLeaseStart.Execute"/>.
        /// Any current run is released and reported cancelled first; a backend failure after that
        /// leaves no input held. Freshness is the caller's responsibility.
        /// </summary>
        public bool TryStart(
            ActRequestMessage request,
            BridgeActionDefinition action,
            long validatedAgainstSeq,
            out string error)
        {
            if (!TryBegin(request, action, validatedAgainstSeq, out BridgeLeaseStart start, out error))
            {
                return false;
            }

            return start.Execute(out error);
        }

        /// <summary>
        /// Validates the act and registers it as the active run, without touching a device or the
        /// running action. Every check that can fail without state, including the act's params,
        /// its lease and the backend's validation of every input of every step, happens here, so a
        /// rejected act leaves a running action undisturbed. Does not wait on the main thread.
        /// </summary>
        public bool TryBegin(
            ActRequestMessage request,
            BridgeActionDefinition action,
            long validatedAgainstSeq,
            out BridgeLeaseStart start,
            out string error)
        {
            start = null;
            error = null;
            if (request == null)
            {
                error = "missing act request";
                return false;
            }

            if (action == null)
            {
                error = "missing action definition";
                return false;
            }

            if (request.Params != null && request.Params.Count > 0)
            {
                error = "act params are not supported: an action runs exactly its declared catalog values";
                return false;
            }

            error = BridgeActionCatalog.Validate(action, checkVocabulary: false);
            if (error != null)
            {
                return false;
            }

            var steps = new List<PlannedStep>();
            double limitS;
            if (action.IsSkill)
            {
                limitS = action.MaxS.Value;
                if (request.LeaseS.HasValue && request.LeaseS.Value != limitS)
                {
                    error = "lease_s must be absent or exactly max_s ("
                        + limitS.ToString("R", CultureInfo.InvariantCulture) + " s) for a skill";
                    return false;
                }

                List<BridgeActionStep> declared = action.Steps;
                for (int i = 0; i < declared.Count; i++)
                {
                    steps.Add(new PlannedStep(declared[i].Inputs, declared[i].HoldS, declared[i].WaitS));
                }
            }
            else
            {
                limitS = request.LeaseS ?? action.DefaultLeaseS;
                if (limitS <= 0 || double.IsNaN(limitS) || double.IsInfinity(limitS))
                {
                    error = "lease_s must be positive";
                    return false;
                }

                if (limitS > MaxLeaseSeconds)
                {
                    error = "lease_s must be at most " + MaxLeaseSeconds.ToString("0") + " s";
                    return false;
                }

                steps.Add(new PlannedStep(action.InputSets()[0], limitS, 0));
            }

            if (!_backend.IsAvailable)
            {
                error = "the " + _backend.Name + " input backend is unavailable";
                return false;
            }

            if (!_backend.TryValidate(action, out error))
            {
                return false;
            }

            Run run;
            lock (_state)
            {
                if (_disposed)
                {
                    error = "the input backend is disposed";
                    return false;
                }

                run = new Run(++_epoch, request, action, validatedAgainstSeq, steps, action.IsSkill, limitS);

                // A run that is still being released, because its hold just expired, has not
                // handed the device back yet: this act waits for it instead of being refused.
                run.Previous = _active ?? _finalizing;
                _active = run;
            }

            start = new BridgeLeaseStart(this, run);
            return true;
        }

        /// <summary>
        /// Releases the current run and reports it cancelled. When <paramref name="emitCancelled"/>
        /// is false, a disconnected client gets no record. Returns whether a run was stopped.
        /// </summary>
        public bool Stop(string reason, bool emitCancelled = true)
        {
            Run active;
            lock (_state)
            {
                active = _active;
            }

            if (active == null)
            {
                return false;
            }

            FinalizeChain(active, BridgeExecStatus.Cancelled, reason, emitCancelled, waitIfLost: false);
            return true;
        }

        public void Dispose()
        {
            lock (_state)
            {
                if (_disposed)
                {
                    return;
                }

                _disposed = true;
            }

            Stop("dispose", emitCancelled: false);
            _backend.Dispose();
        }

        internal bool Execute(object token, out string error)
        {
            error = null;
            var run = (Run)token;

            // The previous run is released and reported before any input of this one is applied,
            // so the two never hold inputs together.
            Run previous = run.Previous;
            run.Previous = null;
            FinalizeChain(previous, BridgeExecStatus.Cancelled, "superseded", emit: true, waitIfLost: true);
            if (run.IsClaimed)
            {
                return true;
            }

            if (!ApplyStep(run, 0, out BridgeInputAck ack, out string applyError))
            {
                // TryBegin already completed all side-effect-free checks. From this point on, a
                // backend failure is an execution failure, even if the device could not provide a
                // receipt. Rejecting it would falsely claim that execution never started.
                EndRun(run, BridgeExecStatus.Failed, "step 0 was not applied: " + applyError, emit: true);
                return true;
            }

            BeginRunning(run, ack);
            return true;
        }

        private bool ApplyStep(Run run, int index, out BridgeInputAck ack, out string error)
        {
            ack = null;
            if (run.IsClaimed)
            {
                error = "the action was stopped";
                return false;
            }

            try
            {
                return _backend.TryApplyInputs(run.Input, index, run.Steps[index].Inputs, out ack, out error);
            }
            catch (Exception ex)
            {
                ack = null;
                error = Describe(ex);
                return false;
            }
        }

        private void BeginRunning(Run run, BridgeInputAck ack)
        {
            // The hold starts when the device confirmed the input applied, not after the running
            // record was built and sent: cold serialization or a slow write consumes the lease
            // instead of adding another full one. Scheduling uses the monotonic clock; the wall
            // times still measure the release below.
            DateTime start = ack != null && ack.AppliedUtc != default(DateTime) ? ack.AppliedUtc : _utcNow();
            long startTimestamp = ack != null && ack.Timestamp != 0 ? ack.Timestamp : Stopwatch.GetTimestamp();
            lock (_state)
            {
                if (run.IsClaimed)
                {
                    return;
                }

                run.Started = true;
                run.StartUtc = start;
                run.StartTimestamp = startTimestamp;
                run.Phase = Phase.Holding;
                run.StepIndex = 0;
            }

            EmitRunning(run);
            if (run.IsSkill)
            {
                ScheduleDeadline(run);
            }

            ScheduleHold(run, 0, startTimestamp);
        }

        private void ScheduleHold(Run run, int index, long stepStartTimestamp)
        {
            double remainingS = run.Steps[index].HoldS - ElapsedSeconds(stepStartTimestamp);
            if (remainingS <= 0)
            {
                OnHoldEnd(run, index);
                return;
            }

            SetStepTimer(run, remainingS, () => OnHoldEnd(run, index));
        }

        private void ScheduleWait(Run run, int index, long releasedTimestamp)
        {
            double remainingS = run.Steps[index].WaitS - ElapsedSeconds(releasedTimestamp);
            if (remainingS <= 0)
            {
                OnWaitEnd(run, index);
                return;
            }

            SetStepTimer(run, remainingS, () => OnWaitEnd(run, index));
        }

        private void ScheduleDeadline(Run run)
        {
            double remainingS = run.LimitS - ElapsedSeconds(run.StartTimestamp);
            if (remainingS <= 0)
            {
                OnDeadline(run);
                return;
            }

            IDisposable timer = _scheduler.Schedule(TimeSpan.FromSeconds(remainingS), () => OnDeadline(run));
            bool keep;
            lock (_state)
            {
                keep = !run.IsClaimed;
                if (keep)
                {
                    run.DeadlineTimer = timer;
                }
            }

            if (!keep)
            {
                timer.Dispose();
            }
        }

        private void SetStepTimer(Run run, double delayS, Action callback)
        {
            IDisposable timer = _scheduler.Schedule(TimeSpan.FromSeconds(delayS), callback);
            IDisposable replaced;
            lock (_state)
            {
                if (run.IsClaimed)
                {
                    replaced = timer;
                }
                else
                {
                    replaced = run.StepTimer;
                    run.StepTimer = timer;
                }
            }

            if (replaced != null)
            {
                replaced.Dispose();
            }
        }

        private void OnHoldEnd(Run run, int index)
        {
            try
            {
                if (!Enter(run, Phase.Holding, index))
                {
                    return;
                }

                bool last = index == run.Steps.Count - 1;
                if (last && run.Steps[index].WaitS <= 0)
                {
                    // The final release is also the run's terminal release.
                    FinalizeRun(run, BridgeExecStatus.Completed, null, emit: true);
                    return;
                }

                if (!ReleaseStep(run, out string error))
                {
                    FinalizeRun(
                        run,
                        BridgeExecStatus.Failed,
                        "step " + index.ToString(CultureInfo.InvariantCulture) + " was not released: " + error,
                        emit: true);
                    return;
                }

                long releasedTimestamp = Stopwatch.GetTimestamp();
                lock (_state)
                {
                    if (run.IsClaimed)
                    {
                        return;
                    }

                    run.Phase = Phase.Waiting;
                }

                ScheduleWait(run, index, releasedTimestamp);
            }
            catch (Exception ex)
            {
                FailRun(run, ex);
            }
        }

        private void OnWaitEnd(Run run, int index)
        {
            try
            {
                if (!Enter(run, Phase.Waiting, index))
                {
                    return;
                }

                if (index == run.Steps.Count - 1)
                {
                    FinalizeRun(run, BridgeExecStatus.Completed, null, emit: true);
                    return;
                }

                int next = index + 1;
                if (!ApplyStep(run, next, out BridgeInputAck ack, out string error))
                {
                    FinalizeRun(
                        run,
                        BridgeExecStatus.Failed,
                        "step " + next.ToString(CultureInfo.InvariantCulture) + " was not applied: " + error,
                        emit: true);
                    return;
                }

                long startTimestamp = ack != null && ack.Timestamp != 0 ? ack.Timestamp : Stopwatch.GetTimestamp();
                lock (_state)
                {
                    if (run.IsClaimed)
                    {
                        return;
                    }

                    run.Phase = Phase.Holding;
                    run.StepIndex = next;
                }

                ScheduleHold(run, next, startTimestamp);
            }
            catch (Exception ex)
            {
                FailRun(run, ex);
            }
        }

        private void OnDeadline(Run run)
        {
            try
            {
                if (run.IsClaimed)
                {
                    return;
                }

                FinalizeRun(run, BridgeExecStatus.TimedOut, "max_s elapsed before the skill finished", emit: true);
            }
            catch (Exception ex)
            {
                FailRun(run, ex);
            }
        }

        private void FailRun(Run run, Exception ex)
        {
            _log?.Invoke("action " + run.Request.ActionId + " failed: " + Describe(ex));
            try
            {
                FinalizeRun(run, BridgeExecStatus.Failed, Describe(ex), emit: true);
            }
            catch (Exception)
            {
            }
        }

        private bool Enter(Run run, Phase expected, int index)
        {
            lock (_state)
            {
                if (run.IsClaimed
                    || !ReferenceEquals(_active, run)
                    || run.Phase != expected
                    || run.StepIndex != index)
                {
                    return false;
                }

                run.Phase = Phase.Transitioning;
                return true;
            }
        }

        private bool ReleaseStep(Run run, out string error)
        {
            bool released = Release(run, out bool verified, out error);
            if (released && verified)
            {
                error = null;
                return true;
            }

            error = error ?? "the release was not verified";
            return false;
        }

        private bool Release(Run run, out bool verified, out string error)
        {
            verified = false;
            try
            {
                bool released = _backend.TryReleaseInputs(run.Input, out verified, out error);
                if (!released)
                {
                    verified = false;
                }

                return released;
            }
            catch (Exception ex)
            {
                verified = false;
                error = Describe(ex);
                return false;
            }
        }

        private void FinalizeChain(Run head, string intended, string reason, bool emit, bool waitIfLost)
        {
            if (head == null)
            {
                return;
            }

            // Oldest first, so a superseded run is reported before the run that replaced it.
            var chain = new List<Run>();
            for (Run run = head; run != null; run = run.Previous)
            {
                chain.Add(run);
            }

            for (int i = chain.Count - 1; i >= 0; i--)
            {
                if (!FinalizeRun(chain[i], intended, reason, emit) && waitIfLost)
                {
                    chain[i].FinalizedDone.Wait(FinalizeWaitMs);
                }
            }
        }

        /// <summary>
        /// Ends the run exactly once. The first caller cancels the run, so no queued apply can reach
        /// the device afterwards, releases every input it applied, checks the device state, and
        /// reports the outcome. Any other caller returns false without waiting.
        /// </summary>
        private bool FinalizeRun(Run run, string intended, string reason, bool emit)
        {
            return EndRun(run, intended, reason, emit) != RunEnd.Lost;
        }

        /// <summary>
        /// The one place a run ends. The first caller claims it, cancels its input run, releases and
        /// verifies what it applied from device reads and reports one terminal record. Every
        /// accepted run gets a terminal record, including a backend failure with no receipt.
        /// </summary>
        private RunEnd EndRun(Run run, string intended, string reason, bool emit)
        {
            if (!run.TryClaim())
            {
                return RunEnd.Lost;
            }

            try
            {
                lock (_state)
                {
                    if (ReferenceEquals(_active, run))
                    {
                        _active = null;
                    }

                    _finalizing = run;
                    DisposeTimers(run);
                }

                // The moment the program stopped: its completion, or the cancel, failure or
                // deadline that cut it short. The inputs may be released later than this.
                DateTime stoppedUtc = _utcNow();
                run.Input.Cancel();
                bool released = Release(run, out bool verified, out string releaseError);
                IReadOnlyList<BridgeInputReceipt> receipts = run.Input.Snapshot();
                if (!released || !verified)
                {
                    _log?.Invoke("releasing input failed: " + releaseError);
                }

                for (int i = 0; i < receipts.Count; i++)
                {
                    if (receipts[i].ReleaseVerified == false)
                    {
                        verified = false;
                    }
                }

                string status = released && verified ? intended : BridgeExecStatus.Failed;
                if (status == BridgeExecStatus.Failed && reason != null)
                {
                    _log?.Invoke("action " + run.Request.ActionId + " failed: " + reason);
                }

                // The program's own clock starts at the first confirmed apply and ends when the
                // program does, so a final wait or a cancel during a wait is part of the total. The
                // last observed release can only push the end later (inputs held until then).
                // Per-input windows stay in the receipts.
                DateTime? start;
                lock (_state)
                {
                    start = run.Started ? run.StartUtc : (DateTime?)null;
                }

                if (!start.HasValue)
                {
                    start = EarliestStart(receipts);
                }

                DateTime? lastRelease = LatestEnd(receipts);
                DateTime end = lastRelease.HasValue && lastRelease.Value > stoppedUtc ? lastRelease.Value : stoppedUtc;
                if (emit)
                {
                    // A release verified for a run that never handed anything to a device is not a
                    // claim; one for an input that was handed over is a measurement.
                    ExecutionRecordMessage record = BuildRecord(
                        run,
                        status,
                        start,
                        end,
                        start.HasValue || receipts.Count > 0 ? verified : (bool?)null,
                        receipts);
                    lock (run.EmitGate)
                    {
                        run.TerminalEmitted = true;
                        _emit(record);
                    }
                }
            }
            finally
            {
                EndFinalizing(run);
            }

            return RunEnd.Reported;
        }

        private void EndFinalizing(Run run)
        {
            run.FinalizedDone.Set();
            lock (_state)
            {
                if (ReferenceEquals(_finalizing, run))
                {
                    _finalizing = null;
                }
            }
        }

        private void EmitRunning(Run run)
        {
            DateTime start;
            lock (_state)
            {
                start = run.StartUtc;
            }

            ExecutionRecordMessage record = BuildRecord(
                run,
                BridgeExecStatus.Running,
                start,
                null,
                null,
                run.Input.Snapshot());
            lock (run.EmitGate)
            {
                if (run.TerminalEmitted)
                {
                    return;
                }

                _emit(record);
            }
        }

        private ExecutionRecordMessage BuildRecord(
            Run run,
            string status,
            DateTime? start,
            DateTime? end,
            bool? releaseVerified,
            IReadOnlyList<BridgeInputReceipt> receipts)
        {
            double? duration = null;
            if (start.HasValue && end.HasValue)
            {
                double seconds = (end.Value - start.Value).TotalSeconds;
                duration = seconds < 0 ? 0 : seconds;
            }

            var record = new ExecutionRecordMessage
            {
                Session = string.Empty,
                Seq = 0,
                WallUtc = _utcNow(),
                ActionId = run.Request.ActionId,
                Status = status,
                ValidatedAgainstSeq = run.ValidatedAgainstSeq,
                InputStartWallS = start.HasValue ? ToUnixSeconds(start.Value) : (double?)null,
                InputEndWallS = start.HasValue && end.HasValue ? ToUnixSeconds(end.Value) : (double?)null,
                AppliedDurationS = duration,
                ReleaseVerified = releaseVerified,
                ExpectedEffect = new List<BridgeEffect>(),
                ObservedEffect = new List<BridgeEffect>(),
            };
            for (int i = 0; i < receipts.Count; i++)
            {
                record.AppliedInputs.Add(ToWire(receipts[i]));
            }

            return record;
        }

        private static AppliedInputReceipt ToWire(BridgeInputReceipt receipt)
        {
            return new AppliedInputReceipt
            {
                StepIndex = receipt.StepIndex,
                Device = receipt.Device,
                Control = receipt.Control,
                X = receipt.X,
                Y = receipt.Y,
                InputStartWallS = receipt.StartUtc.HasValue ? ToUnixSeconds(receipt.StartUtc.Value) : (double?)null,
                InputEndWallS = receipt.EndUtc.HasValue ? ToUnixSeconds(receipt.EndUtc.Value) : (double?)null,
                AppliedDurationS = receipt.AppliedDurationS,
                AppliedDeltaX = receipt.AppliedDeltaX,
                AppliedDeltaY = receipt.AppliedDeltaY,
                ReleaseVerified = receipt.ReleaseVerified,
            };
        }

        private static DateTime? LatestEnd(IReadOnlyList<BridgeInputReceipt> receipts)
        {
            DateTime? latest = null;
            for (int i = 0; i < receipts.Count; i++)
            {
                DateTime? end = receipts[i].EndUtc;
                if (end.HasValue && (!latest.HasValue || end.Value > latest.Value))
                {
                    latest = end;
                }
            }

            return latest;
        }

        private static DateTime? EarliestStart(IReadOnlyList<BridgeInputReceipt> receipts)
        {
            DateTime? earliest = null;
            for (int i = 0; i < receipts.Count; i++)
            {
                DateTime? start = receipts[i].StartUtc;
                if (start.HasValue && (!earliest.HasValue || start.Value < earliest.Value))
                {
                    earliest = start;
                }
            }

            return earliest;
        }

        private static double ToUnixSeconds(DateTime value)
        {
            DateTime utc = value.Kind == DateTimeKind.Utc ? value : value.ToUniversalTime();
            return (utc - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
        }

        private static double ElapsedSeconds(long startTimestamp)
        {
            return (Stopwatch.GetTimestamp() - startTimestamp) / (double)Stopwatch.Frequency;
        }

        private static string Describe(Exception ex)
        {
            return ex.GetType().Name + ": " + ex.Message;
        }

        private static void DisposeTimers(Run run)
        {
            IDisposable step = run.StepTimer;
            IDisposable deadline = run.DeadlineTimer;
            run.StepTimer = null;
            run.DeadlineTimer = null;
            if (step != null)
            {
                step.Dispose();
            }

            if (deadline != null)
            {
                deadline.Dispose();
            }
        }

        private enum RunEnd
        {
            /// <summary>Another caller owns the end of this run and reports it.</summary>
            Lost,

            /// <summary>This caller ended the run and reported its terminal record.</summary>
            Reported,

        }

        private enum Phase
        {
            Starting,
            Holding,
            Waiting,
            Transitioning,
        }

        private readonly struct PlannedStep
        {
            public PlannedStep(IReadOnlyList<BridgeInputBinding> inputs, double holdS, double waitS)
            {
                Inputs = inputs;
                HoldS = holdS;
                WaitS = waitS;
            }

            public IReadOnlyList<BridgeInputBinding> Inputs { get; }

            public double HoldS { get; }

            public double WaitS { get; }
        }

        private sealed class Run
        {
            private int _claimed;

            public Run(
                long epoch,
                ActRequestMessage request,
                BridgeActionDefinition action,
                long validatedAgainstSeq,
                List<PlannedStep> steps,
                bool isSkill,
                double limitS)
            {
                Epoch = epoch;
                Request = request;
                Action = action;
                ValidatedAgainstSeq = validatedAgainstSeq;
                Steps = steps;
                IsSkill = isSkill;
                LimitS = limitS;
                Input = new BridgeInputRun(action.Name);
            }

            public long Epoch { get; }

            public ActRequestMessage Request { get; }

            public BridgeActionDefinition Action { get; }

            public long ValidatedAgainstSeq { get; }

            public List<PlannedStep> Steps { get; }

            public bool IsSkill { get; }

            /// <summary>An action's hold, or a skill's max_s.</summary>
            public double LimitS { get; }

            public BridgeInputRun Input { get; }

            public object EmitGate { get; } = new object();

            public ManualResetEventSlim FinalizedDone { get; } = new ManualResetEventSlim(false);

            public volatile Run Previous;

            // Guarded by the lease's state lock.
            public Phase Phase = Phase.Starting;

            public int StepIndex;

            public bool Started;

            public DateTime StartUtc;

            public long StartTimestamp;

            public IDisposable StepTimer;

            public IDisposable DeadlineTimer;

            // Guarded by EmitGate.
            public bool TerminalEmitted;

            public bool IsClaimed
            {
                get { return Volatile.Read(ref _claimed) != 0; }
            }

            public bool TryClaim()
            {
                return Interlocked.Exchange(ref _claimed, 1) == 0;
            }
        }
    }
}
