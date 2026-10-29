// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Threading;

namespace Playtest.Bridge.Actions
{
    /// <summary>One control reading taken from a device while an input update was visible to the game.</summary>
    public struct BridgeInputReading
    {
        /// <summary>True when a button-like control reads as pressed.</summary>
        public bool Pressed;

        /// <summary>The control value, or the x component of a two dimensional control.</summary>
        public double X;

        /// <summary>The y component of a two dimensional control.</summary>
        public double Y;
    }

    /// <summary>
    /// The device layer the executor drives. Every member is called on the Unity main thread only,
    /// except <see cref="IsAvailable"/> and <see cref="TryValidateControl"/>, which must be pure.
    /// The Unity implementation queues Input System events and reads controls inside the input
    /// update; tests supply a simulated device.
    /// </summary>
    public interface IBridgeInputSurface
    {
        bool IsAvailable { get; }

        /// <summary>
        /// Count of input updates the game could observe. It increases before the executor is told
        /// about the update, and only for updates that processed the queued events.
        /// </summary>
        long UpdateCount { get; }

        /// <summary>Pure check of anything only the device layer knows, such as the key names that exist.</summary>
        bool TryValidateControl(BridgeControlInfo info, out string error);

        /// <summary>
        /// Queues the combined state of one device for these inputs so they apply in the same
        /// input update. Nothing is applied until the next input update. Returns false only when
        /// nothing was queued for the device (it is missing, a key does not exist); an exception
        /// means the device may hold a partial state, which the executor treats as outstanding.
        /// </summary>
        bool TryQueue(string device, IReadOnlyList<BridgeInputBinding> inputs, out string error);

        /// <summary>
        /// Queues the neutral state of one device for inputs this executor applied. It may be
        /// called again for the same inputs: a neutral that was lost or refused is re-queued until a
        /// read shows the control neutral.
        /// </summary>
        bool TryQueueNeutral(string device, IReadOnlyList<BridgeInputBinding> inputs, out string error);

        /// <summary>Reads the device control for a declared input.</summary>
        bool TryRead(BridgeInputBinding input, out BridgeInputReading reading, out string error);

        /// <summary>
        /// Processes queued events now with a manual input update. Only used when the caller is
        /// the main thread, which cannot wait for the next update to arrive.
        /// </summary>
        void Flush();
    }

    public sealed class BridgeInputExecutorOptions
    {
        /// <summary>How long an apply or release waits for the main thread and the next input update.</summary>
        public int OperationTimeoutMs { get; set; } = 3000;

        /// <summary>How long a verification of an earlier release waits.</summary>
        public int VerifyTimeoutMs { get; set; } = 200;

        /// <summary>Largest difference between a declared and a read stick, trigger or dpad value.</summary>
        public double ValueTolerance { get; set; } = 1e-4;

        /// <summary>Largest difference between a declared and a read mouse pixel delta.</summary>
        public double PixelTolerance { get; set; } = 1e-3;
    }

    /// <summary>
    /// Applies declared inputs to a device surface and verifies, from device reads, that they were
    /// applied and later released. Device effects happen on the main thread: a caller on another
    /// thread submits the work and waits for it holding no lock, and a caller on the main thread
    /// runs it inline with a manual input update, so the main thread never waits on itself.
    ///
    /// Inputs of one step apply together, per device in one queued state, and are confirmed by
    /// reading each control after the first input update that could have processed them. The time
    /// of that confirming read is the start of the input; the time a later read shows it neutral is
    /// the end. A mouse delta is a one-shot impulse: it is queued once and the delta the device
    /// read back is reported, never the declared one. Nothing here chooses an action; the
    /// executor only does what the lease hands it and reports what the device showed.
    ///
    /// An input is tracked as outstanding, with its receipt, before the device layer is asked to
    /// queue it, and stays outstanding until a read shows it neutral. So a later device refusing
    /// the same step, a neutral that could not be queued, or an apply that timed out never lets
    /// an input that may have reached a device go unaccounted for: a failed apply returns with
    /// those inputs still outstanding, new applies are refused, and a release or a later
    /// <see cref="TryVerifyRelease"/> queues the neutral again and verifies it.
    /// </summary>
    public sealed class BridgeInputExecutor : IBridgeInputBackend
    {
        private const int WaiterGraceMs = 250;

        private readonly IBridgeInputSurface _surface;
        private readonly int _mainThreadId;
        private readonly Func<DateTime> _utcNow;
        private readonly BridgeInputExecutorOptions _options;
        private readonly ConcurrentQueue<Operation> _inbox = new ConcurrentQueue<Operation>();
        private readonly List<Operation> _pending = new List<Operation>();
        private readonly List<HeldInput> _held = new List<HeldInput>();
        private readonly List<HeldInput> _awaiting = new List<HeldInput>();
        private readonly object _namesGate = new object();
        private readonly List<string> _releasedNames = new List<string>();
        private int _outstanding;
        private int _disposed;

        public BridgeInputExecutor(
            IBridgeInputSurface surface,
            int? mainThreadId = null,
            Func<DateTime> utcNow = null,
            BridgeInputExecutorOptions options = null)
        {
            _surface = surface ?? throw new ArgumentNullException(nameof(surface));
            _mainThreadId = mainThreadId ?? Thread.CurrentThread.ManagedThreadId;
            _utcNow = utcNow ?? (() => DateTime.UtcNow);
            _options = options ?? new BridgeInputExecutorOptions();
        }

        public string Name
        {
            get { return BridgeActionDefinition.InputSystemBackend; }
        }

        public bool IsAvailable
        {
            get { return Volatile.Read(ref _disposed) == 0 && _surface.IsAvailable; }
        }

        /// <summary>Inputs held or released but not yet confirmed neutral. Readable from any thread.</summary>
        public int OutstandingInputs
        {
            get { return Volatile.Read(ref _outstanding); }
        }

        public IReadOnlyList<string> ReleasedInputs
        {
            get
            {
                lock (_namesGate)
                {
                    return new List<string>(_releasedNames);
                }
            }
        }

        private bool OnMainThread
        {
            get { return Thread.CurrentThread.ManagedThreadId == _mainThreadId; }
        }

        public bool TryValidate(BridgeActionDefinition action, out string error)
        {
            error = null;
            if (action == null)
            {
                error = "missing action definition";
                return false;
            }

            if (!_surface.IsAvailable)
            {
                error = "the Input System backend is not enabled in this project";
                return false;
            }

            if (!string.Equals(action.Backend, BridgeActionDefinition.InputSystemBackend, StringComparison.OrdinalIgnoreCase))
            {
                error = "unsupported input backend '" + action.Backend + "'";
                return false;
            }

            IReadOnlyList<IReadOnlyList<BridgeInputBinding>> sets = action.InputSets();
            if (sets.Count == 0)
            {
                error = "the action declares no inputs";
                return false;
            }

            for (int i = 0; i < sets.Count; i++)
            {
                if (!TryValidateInputs(sets[i], out string setError))
                {
                    error = action.IsSkill ? "step " + i.ToString(CultureInfo.InvariantCulture) + ": " + setError : setError;
                    return false;
                }
            }

            return true;
        }

        public bool TryApplyInputs(
            BridgeInputRun run,
            int stepIndex,
            IReadOnlyList<BridgeInputBinding> inputs,
            out BridgeInputAck ack,
            out string error)
        {
            ack = null;
            if (run == null)
            {
                error = "missing run";
                return false;
            }

            if (stepIndex < 0)
            {
                error = "step index must not be negative";
                return false;
            }

            if (!TryValidateInputs(inputs, out error))
            {
                return false;
            }

            var operation = new ApplyOperation(run, stepIndex, new List<BridgeInputBinding>(inputs));
            bool applied = Execute(operation, _options.OperationTimeoutMs, out error);
            if (applied)
            {
                ack = operation.Ack;
            }

            return applied;
        }

        public bool TryReleaseInputs(BridgeInputRun run, out bool releaseVerified, out string error)
        {
            releaseVerified = false;
            error = null;
            if (run == null)
            {
                error = "missing run";
                return false;
            }

            // Nothing applied, unverified or still being applied for this run: no device read can
            // add anything, and no main-thread round trip is needed.
            if (run.Outstanding == 0 && run.InFlight == 0)
            {
                releaseVerified = true;
                return true;
            }

            var operation = new ReleaseOperation(run);
            bool released = Execute(operation, _options.OperationTimeoutMs, out error);
            releaseVerified = released && operation.Verified;
            if (!releaseVerified && error == null)
            {
                error = "release was not verified";
            }

            return released;
        }

        public bool TryVerifyRelease(out string error)
        {
            error = null;
            if (Volatile.Read(ref _outstanding) == 0)
            {
                ClearReleasedNames();
                return true;
            }

            var operation = new VerifyOperation();
            bool completed = Execute(operation, _options.VerifyTimeoutMs, out error);
            if (completed && operation.Verified)
            {
                ClearReleasedNames();
                return true;
            }

            if (error == null)
            {
                error = "release was not verified";
            }

            return false;
        }

        /// <summary>
        /// Runs work other threads submitted. Call it from the main thread on every Unity update
        /// (the Editor update or before-render). It never blocks.
        /// </summary>
        public void Pump()
        {
            if (!OnMainThread)
            {
                return;
            }

            PumpInbox();
            ExpirePending();
        }

        /// <summary>
        /// Call from the main thread after each input update the game could observe, once the
        /// surface counted it. Pending applies and releases read the devices here, where the state
        /// is the one the game sees.
        /// </summary>
        public void OnGameInputUpdate()
        {
            if (!OnMainThread)
            {
                return;
            }

            PollPending(updateObserved: true);
        }

        public void Dispose()
        {
            if (Interlocked.Exchange(ref _disposed, 1) != 0)
            {
                return;
            }

            if (OnMainThread)
            {
                ReleaseAllBestEffort();
                FailAll("the input backend is disposed");
            }
        }

        private bool TryValidateInputs(IReadOnlyList<BridgeInputBinding> inputs, out string error)
        {
            if (!BridgeInputVocabulary.TryValidateInputs(inputs, checkVocabulary: true, out error))
            {
                return false;
            }

            for (int i = 0; i < inputs.Count; i++)
            {
                BridgeInputVocabulary.TryResolve(inputs[i].Device, inputs[i].Control, out BridgeControlInfo info, out error);
                if (!_surface.TryValidateControl(info, out error))
                {
                    error = "input " + i.ToString(CultureInfo.InvariantCulture) + ": " + error;
                    return false;
                }
            }

            error = null;
            return true;
        }

        private bool Execute(Operation operation, int timeoutMs, out string error)
        {
            if (Volatile.Read(ref _disposed) != 0)
            {
                error = "the input backend is disposed";
                return false;
            }

            operation.Owner = this;
            operation.DeadlineTimestamp = Stopwatch.GetTimestamp() + ToTicks(timeoutMs);
            operation.Run?.AddInFlight(1);

            if (OnMainThread)
            {
                RunInline(operation);
            }
            else
            {
                _inbox.Enqueue(operation);

                // The operation expires itself at its deadline once the main thread polls it, and
                // says what the device showed. The waiter only gives up after a grace period, which
                // means the main thread never answered at all.
                if (!operation.Done.Wait(timeoutMs + WaiterGraceMs))
                {
                    operation.Abandoned = true;
                    error = "timed out after " + timeoutMs.ToString(CultureInfo.InvariantCulture)
                        + " ms waiting for the Unity main thread to process the input";
                    return false;
                }
            }

            error = operation.Error;
            return operation.Succeeded;
        }

        private void RunInline(Operation operation)
        {
            PumpInbox();
            if (operation.IsComplete)
            {
                return;
            }

            try
            {
                operation.Begin();
            }
            catch (Exception ex)
            {
                operation.Fail(Describe(ex));
                return;
            }

            if (operation.IsComplete)
            {
                return;
            }

            try
            {
                _surface.Flush();
            }
            catch (Exception ex)
            {
                operation.Fail(Describe(ex));
                return;
            }

            PollPending(updateObserved: true);
            if (!operation.IsComplete)
            {
                operation.Expire("the manual input update did not process the queued state");
                _pending.RemoveAll(IsComplete);
            }
        }

        private void PumpInbox()
        {
            while (_inbox.TryDequeue(out Operation operation))
            {
                if (Volatile.Read(ref _disposed) != 0)
                {
                    operation.Fail("the input backend is disposed");
                    continue;
                }

                if (operation.Abandoned && operation.SkipWhenAbandoned)
                {
                    operation.Fail("the caller stopped waiting");
                    continue;
                }

                try
                {
                    operation.Begin();
                }
                catch (Exception ex)
                {
                    operation.Fail(Describe(ex));
                }
            }
        }

        private void PollPending(bool updateObserved)
        {
            if (_pending.Count == 0)
            {
                return;
            }

            Operation[] snapshot = _pending.ToArray();
            for (int i = 0; i < snapshot.Length; i++)
            {
                if (snapshot[i].IsComplete)
                {
                    continue;
                }

                try
                {
                    snapshot[i].Poll(updateObserved);
                }
                catch (Exception ex)
                {
                    snapshot[i].Fail(Describe(ex));
                }
            }

            _pending.RemoveAll(IsComplete);
        }

        private void ExpirePending()
        {
            PollPending(updateObserved: false);
        }

        private static bool IsComplete(Operation operation)
        {
            return operation.IsComplete;
        }

        private void FailAll(string reason)
        {
            while (_inbox.TryDequeue(out Operation queued))
            {
                queued.Fail(reason);
            }

            for (int i = 0; i < _pending.Count; i++)
            {
                _pending[i].Fail(reason);
            }

            _pending.Clear();
        }

        private void ReleaseAllBestEffort()
        {
            if (_held.Count == 0 && _awaiting.Count == 0)
            {
                return;
            }

            try
            {
                var everything = new List<HeldInput>(_held);
                everything.AddRange(_awaiting);
                QueueNeutral(everything);
                _surface.Flush();
            }
            catch (Exception)
            {
            }

            _held.Clear();
            _awaiting.Clear();
            RefreshOutstanding();
        }

        private void RefreshOutstanding()
        {
            Volatile.Write(ref _outstanding, _held.Count + _awaiting.Count);
        }

        private void SetReleasedNames(List<HeldInput> released)
        {
            lock (_namesGate)
            {
                _releasedNames.Clear();
                for (int i = 0; i < released.Count; i++)
                {
                    _releasedNames.Add(BridgeInputVocabulary.ReleasedName(released[i].Input));
                }
            }
        }

        private void ClearReleasedNames()
        {
            lock (_namesGate)
            {
                _releasedNames.Clear();
            }
        }

        /// <summary>
        /// Queues the neutral state of every device these inputs are on, best effort: a refusal or an
        /// exception does not stop the other devices and does not forget the input, which stays
        /// outstanding until a read shows it neutral. Returns the first problem, or null.
        /// </summary>
        private string QueueNeutral(IEnumerable<HeldInput> entries)
        {
            var inputs = new List<HeldInput>(entries);
            string problem = null;
            foreach (KeyValuePair<string, List<BridgeInputBinding>> group in GroupByDevice(inputs))
            {
                try
                {
                    if (!_surface.TryQueueNeutral(group.Key, group.Value, out string error) && problem == null)
                    {
                        problem = "the neutral state of " + group.Key + " could not be queued: " + (error ?? "refused");
                    }
                }
                catch (Exception ex)
                {
                    if (problem == null)
                    {
                        problem = "the neutral state of " + group.Key + " could not be queued: " + Describe(ex);
                    }
                }
            }

            return problem;
        }

        /// <summary>Forgets inputs the device layer reported it never queued, so nothing waits for a read that cannot come.</summary>
        private void Untrack(BridgeInputRun run, List<HeldInput> entries)
        {
            for (int i = 0; i < entries.Count; i++)
            {
                _held.Remove(entries[i]);
                run.RemoveReceipt(entries[i].Receipt);
            }

            run.AddOutstanding(-entries.Count);
            RefreshOutstanding();
        }

        private static List<KeyValuePair<string, List<BridgeInputBinding>>> GroupByDevice(List<HeldInput> held)
        {
            var groups = new List<KeyValuePair<string, List<BridgeInputBinding>>>();
            for (int i = 0; i < held.Count; i++)
            {
                AddToGroup(groups, held[i].Input);
            }

            return groups;
        }

        private static void AddToGroup(List<KeyValuePair<string, List<BridgeInputBinding>>> groups, BridgeInputBinding input)
        {
            BridgeInputVocabulary.TryCanonicalDevice(input.Device, out string device);
            for (int i = 0; i < groups.Count; i++)
            {
                if (string.Equals(groups[i].Key, device, StringComparison.Ordinal))
                {
                    groups[i].Value.Add(input);
                    return;
                }
            }

            groups.Add(new KeyValuePair<string, List<BridgeInputBinding>>(device, new List<BridgeInputBinding> { input }));
        }

        /// <summary>
        /// Reads every awaiting input of <paramref name="run"/> (all runs when null) and finishes the
        /// receipt of each one that reads neutral. Returns the number still not neutral.
        /// </summary>
        private int VerifyAwaiting(BridgeInputRun run, out string problem)
        {
            problem = null;
            DateTime now = _utcNow();
            int remaining = 0;
            for (int i = _awaiting.Count - 1; i >= 0; i--)
            {
                HeldInput entry = _awaiting[i];
                if (run != null && !ReferenceEquals(entry.Run, run))
                {
                    continue;
                }

                string mismatch;
                if (!_surface.TryRead(entry.Input, out BridgeInputReading reading, out string readError))
                {
                    mismatch = readError ?? "the device could not be read";
                }
                else
                {
                    mismatch = NotNeutral(entry, reading);
                }

                if (mismatch != null)
                {
                    remaining++;
                    problem = mismatch;
                    continue;
                }

                lock (entry.Run.SyncRoot)
                {
                    entry.Receipt.EndUtc = now;
                    entry.Receipt.ReleaseVerified = true;
                    if (entry.Info.Kind != BridgeControlKind.Impulse && entry.Receipt.StartUtc.HasValue)
                    {
                        entry.Receipt.AppliedDurationS = Math.Max(0, (now - entry.Receipt.StartUtc.Value).TotalSeconds);
                    }
                }

                entry.Run.AddOutstanding(-1);
                _awaiting.RemoveAt(i);
            }

            RefreshOutstanding();
            return remaining;
        }

        private void MarkUnverified(BridgeInputRun run)
        {
            for (int i = 0; i < _awaiting.Count; i++)
            {
                HeldInput entry = _awaiting[i];
                if (run != null && !ReferenceEquals(entry.Run, run))
                {
                    continue;
                }

                lock (entry.Run.SyncRoot)
                {
                    entry.Receipt.ReleaseVerified = false;
                }
            }
        }

        private string NotApplied(HeldInput entry, BridgeInputReading reading)
        {
            BridgeInputBinding input = entry.Input;
            double tolerance = _options.ValueTolerance;
            switch (entry.Info.Kind)
            {
                case BridgeControlKind.Button:
                    return reading.Pressed ? null : input + " reads as not pressed";

                case BridgeControlKind.Axis:
                    return Math.Abs(reading.X - input.X.Value) <= tolerance
                        ? null
                        : input + " reads " + Format(reading.X);

                case BridgeControlKind.Impulse:
                {
                    double x = input.X.Value * BridgeInputVocabulary.MouseDeltaPixelsPerUnit;
                    double y = input.Y.Value * BridgeInputVocabulary.MouseDeltaPixelsPerUnit;
                    return Math.Abs(reading.X - x) <= _options.PixelTolerance && Math.Abs(reading.Y - y) <= _options.PixelTolerance
                        ? null
                        : input + " read a delta of (" + Format(reading.X) + ", " + Format(reading.Y)
                            + ") pixels, not the declared (" + Format(x) + ", " + Format(y) + ")";
                }

                default:
                    return Math.Abs(reading.X - input.X.Value) <= tolerance && Math.Abs(reading.Y - input.Y.Value) <= tolerance
                        ? null
                        : input + " reads (" + Format(reading.X) + ", " + Format(reading.Y) + ")";
            }
        }

        private string NotNeutral(HeldInput entry, BridgeInputReading reading)
        {
            BridgeInputBinding input = entry.Input;
            switch (entry.Info.Kind)
            {
                case BridgeControlKind.Button:
                    return reading.Pressed ? input + " still reads as pressed" : null;

                case BridgeControlKind.Axis:
                    return Math.Abs(reading.X) <= _options.ValueTolerance ? null : input + " still reads " + Format(reading.X);

                case BridgeControlKind.Impulse:
                    return Math.Abs(reading.X) <= _options.PixelTolerance && Math.Abs(reading.Y) <= _options.PixelTolerance
                        ? null
                        : input + " still reads a delta of (" + Format(reading.X) + ", " + Format(reading.Y) + ") pixels";

                default:
                    return Math.Abs(reading.X) <= _options.ValueTolerance && Math.Abs(reading.Y) <= _options.ValueTolerance
                        ? null
                        : input + " still reads (" + Format(reading.X) + ", " + Format(reading.Y) + ")";
            }
        }

        private static string Format(double value)
        {
            return value.ToString("R", CultureInfo.InvariantCulture);
        }

        private static string Describe(Exception ex)
        {
            return ex.GetType().Name + ": " + ex.Message;
        }

        private static long ToTicks(int milliseconds)
        {
            return (long)(Math.Max(0, milliseconds) / 1000.0 * Stopwatch.Frequency);
        }

        private sealed class HeldInput
        {
            public BridgeInputBinding Input;

            public BridgeControlInfo Info;

            public BridgeInputReceipt Receipt;

            public BridgeInputRun Run;
        }

        private abstract class Operation
        {
            private int _completed;

            public BridgeInputExecutor Owner;

            public BridgeInputRun Run;

            public readonly ManualResetEventSlim Done = new ManualResetEventSlim(false);

            public volatile bool Abandoned;

            public bool Succeeded;

            public bool Verified;

            public string Error;

            public BridgeInputAck Ack;

            public long DeadlineTimestamp;

            public long QueuedAtUpdate;

            public bool IsComplete
            {
                get { return Volatile.Read(ref _completed) != 0; }
            }

            /// <summary>An apply nobody waits for any more must not start; a release always runs.</summary>
            public virtual bool SkipWhenAbandoned
            {
                get { return false; }
            }

            /// <summary>Starts the work on the main thread. May finish or become pending.</summary>
            public abstract void Begin();

            /// <summary>Re-checks a pending operation, with <paramref name="updateObserved"/> after an input update.</summary>
            public abstract void Poll(bool updateObserved);

            /// <summary>The deadline passed or the caller left: finish with what the device showed.</summary>
            public virtual void Expire(string reason)
            {
                Fail(reason);
            }

            public void Fail(string error)
            {
                Finish(false, error);
            }

            public void Succeed()
            {
                Finish(true, null);
            }

            public void Finish(bool succeeded, string error)
            {
                if (Interlocked.Exchange(ref _completed, 1) != 0)
                {
                    return;
                }

                Succeeded = succeeded;
                Error = error;
                if (Run != null)
                {
                    Run.AddInFlight(-1);
                }

                Done.Set();
            }

            protected bool Due
            {
                get { return Abandoned || Stopwatch.GetTimestamp() >= DeadlineTimestamp; }
            }

            protected bool UpdateSinceQueued
            {
                get { return Owner._surface.UpdateCount > QueuedAtUpdate; }
            }
        }

        private sealed class ApplyOperation : Operation
        {
            private readonly int _stepIndex;
            private readonly List<BridgeInputBinding> _inputs;
            private readonly List<HeldInput> _entries = new List<HeldInput>();
            private string _lastProblem;

            public ApplyOperation(BridgeInputRun run, int stepIndex, List<BridgeInputBinding> inputs)
            {
                Run = run;
                _stepIndex = stepIndex;
                _inputs = inputs;
            }

            public override bool SkipWhenAbandoned
            {
                get { return true; }
            }

            public override void Begin()
            {
                BridgeInputExecutor owner = Owner;
                if (Run.IsCancelled)
                {
                    Fail("the action was cancelled before its inputs were applied");
                    return;
                }

                if (owner._held.Count > 0 || owner._awaiting.Count > 0)
                {
                    Fail("earlier inputs are still held or not yet confirmed released");
                    return;
                }

                var infos = new BridgeControlInfo[_inputs.Count];
                for (int i = 0; i < _inputs.Count; i++)
                {
                    if (!BridgeInputVocabulary.TryResolve(_inputs[i].Device, _inputs[i].Control, out infos[i], out string error))
                    {
                        Fail(error);
                        return;
                    }
                }

                // One queued state per device, in order of first appearance. A device's inputs are
                // tracked (outstanding, with receipts) before the device layer is asked to queue
                // them, so whatever may have reached a device is never forgotten, whichever later
                // step of this apply fails or whichever cleanup is refused.
                var devices = new List<string>();
                var members = new Dictionary<string, List<int>>(StringComparer.Ordinal);
                for (int i = 0; i < _inputs.Count; i++)
                {
                    string device = infos[i].Device;
                    if (!members.TryGetValue(device, out List<int> indexes))
                    {
                        indexes = new List<int>();
                        members.Add(device, indexes);
                        devices.Add(device);
                    }

                    indexes.Add(i);
                }

                var entries = new HeldInput[_inputs.Count];
                string failure = null;
                for (int d = 0; d < devices.Count && failure == null; d++)
                {
                    string device = devices[d];
                    List<int> indexes = members[device];
                    var tracked = new List<HeldInput>(indexes.Count);
                    var queuedInputs = new List<BridgeInputBinding>(indexes.Count);
                    for (int k = 0; k < indexes.Count; k++)
                    {
                        int index = indexes[k];
                        var entry = new HeldInput
                        {
                            Input = _inputs[index],
                            Info = infos[index],
                            Run = Run,
                            Receipt = Run.AddReceipt(_stepIndex, _inputs[index], index),
                        };
                        entries[index] = entry;
                        tracked.Add(entry);
                        queuedInputs.Add(_inputs[index]);
                        owner._held.Add(entry);
                    }

                    Run.AddOutstanding(tracked.Count);
                    owner.ClearReleasedNames();
                    owner.RefreshOutstanding();

                    bool queued;
                    string queueError;
                    try
                    {
                        queued = owner._surface.TryQueue(device, queuedInputs, out queueError);
                    }
                    catch (Exception ex)
                    {
                        queued = false;
                        queueError = Describe(ex);
                    }

                    if (queued)
                    {
                        continue;
                    }

                    failure = queueError ?? "the device refused the input";
                    // A failed queue can be reported after the device has accepted part of the
                    // state. Keep every attempted control and receipt outstanding until a device
                    // read verifies neutral; a failed cleanup must block later applies too.
                }

                if (failure != null)
                {
                    // Nothing may stay half applied: queue the neutral state of every device already
                    // handed an input. The inputs stay outstanding, so a refused or lost neutral is
                    // retried by the release that follows and is never reported as released.
                    string cleanup = owner.QueueNeutral(owner._held.FindAll(held => ReferenceEquals(held.Run, Run)));
                    Fail(cleanup == null ? failure : failure + "; " + cleanup);
                    return;
                }

                _entries.AddRange(entries);
                QueuedAtUpdate = owner._surface.UpdateCount;
                owner._pending.Add(this);
            }

            public override void Poll(bool updateObserved)
            {
                if (IsComplete)
                {
                    return;
                }

                if (Run.IsCancelled)
                {
                    // The lease cancelled the run and is releasing it: the apply will not be awaited.
                    Fail("the action was cancelled while its inputs were being applied");
                    return;
                }

                BridgeInputExecutor owner = Owner;
                if (updateObserved && UpdateSinceQueued)
                {
                    long timestamp = Stopwatch.GetTimestamp();
                    DateTime now = owner._utcNow();
                    bool applied = true;
                    string problem = null;
                    for (int i = 0; i < _entries.Count; i++)
                    {
                        HeldInput entry = _entries[i];
                        string mismatch;
                        if (!owner._surface.TryRead(entry.Input, out BridgeInputReading reading, out string readError))
                        {
                            mismatch = readError ?? "the device could not be read";
                        }
                        else
                        {
                            if (entry.Info.Kind == BridgeControlKind.Impulse)
                            {
                                lock (Run.SyncRoot)
                                {
                                    if (!entry.Receipt.AppliedDeltaX.HasValue)
                                    {
                                        entry.Receipt.AppliedDeltaX = reading.X;
                                        entry.Receipt.AppliedDeltaY = reading.Y;
                                    }
                                }
                            }

                            mismatch = owner.NotApplied(entry, reading);
                        }

                        if (mismatch != null)
                        {
                            applied = false;
                            problem = mismatch;
                        }
                    }

                    if (applied)
                    {
                        lock (Run.SyncRoot)
                        {
                            for (int i = 0; i < _entries.Count; i++)
                            {
                                _entries[i].Receipt.StartUtc = now;
                            }
                        }

                        Ack = new BridgeInputAck(now, timestamp);
                        Succeed();
                        return;
                    }

                    // The first update that could have processed the input is the informative
                    // one: a later update only shows an impulse already reset to zero.
                    if (_lastProblem == null)
                    {
                        _lastProblem = problem;
                    }
                }

                if (Due)
                {
                    Expire(_lastProblem);
                }
            }

            public override void Expire(string reason)
            {
                Fail((reason ?? "no input update processed the queued state") + "; the input was not confirmed applied");
            }
        }

        private sealed class ReleaseOperation : Operation
        {
            private string _lastProblem;

            public ReleaseOperation(BridgeInputRun run)
            {
                Run = run;
            }

            public override void Begin()
            {
                BridgeInputExecutor owner = Owner;
                var mine = new List<HeldInput>();
                for (int i = 0; i < owner._held.Count; i++)
                {
                    if (ReferenceEquals(owner._held[i].Run, Run))
                    {
                        mine.Add(owner._held[i]);
                    }
                }

                // Inputs of this run an earlier release already moved here but could not confirm
                // neutral may be stuck because their neutral state was refused or lost: ask again.
                var toNeutralize = new List<HeldInput>(mine);
                for (int i = 0; i < owner._awaiting.Count; i++)
                {
                    if (ReferenceEquals(owner._awaiting[i].Run, Run))
                    {
                        toNeutralize.Add(owner._awaiting[i]);
                    }
                }

                string queueError = owner.QueueNeutral(toNeutralize);
                bool queueFailed = queueError != null;

                if (mine.Count > 0)
                {
                    owner.SetReleasedNames(mine);
                    for (int i = 0; i < mine.Count; i++)
                    {
                        owner._held.Remove(mine[i]);
                        owner._awaiting.Add(mine[i]);
                    }
                }

                owner.RefreshOutstanding();
                if (queueFailed)
                {
                    owner.MarkUnverified(Run);
                    Fail("the release could not be queued: " + queueError);
                    return;
                }

                bool anyMine = false;
                for (int i = 0; i < owner._awaiting.Count; i++)
                {
                    if (ReferenceEquals(owner._awaiting[i].Run, Run))
                    {
                        anyMine = true;
                        break;
                    }
                }

                if (!anyMine)
                {
                    Verified = true;
                    Succeed();
                    return;
                }

                QueuedAtUpdate = owner._surface.UpdateCount;
                owner._pending.Add(this);
            }

            public override void Poll(bool updateObserved)
            {
                if (IsComplete)
                {
                    return;
                }

                BridgeInputExecutor owner = Owner;
                if (updateObserved && UpdateSinceQueued)
                {
                    int remaining = owner.VerifyAwaiting(Run, out string problem);
                    if (remaining == 0)
                    {
                        Verified = true;
                        Succeed();
                        return;
                    }

                    _lastProblem = problem;
                }

                if (Due)
                {
                    Expire(_lastProblem);
                }
            }

            public override void Expire(string reason)
            {
                Owner.MarkUnverified(Run);
                Verified = false;
                Finish(true, (reason ?? "no input update processed the release") + "; the release was not confirmed");
            }
        }

        private sealed class VerifyOperation : Operation
        {
            private string _lastProblem;
            private string _queueProblem;

            public override void Begin()
            {
                BridgeInputExecutor owner = Owner;
                if (owner._held.Count > 0)
                {
                    Fail("inputs are still held");
                    return;
                }

                if (owner._awaiting.Count == 0)
                {
                    Verified = true;
                    Succeed();
                    return;
                }

                // Something released earlier is still not confirmed neutral. The neutral state that
                // release queued may have been refused or lost, so a stop or any later verification
                // queues it again before it reads; it can only help a device that is stuck.
                _queueProblem = owner.QueueNeutral(owner._awaiting);
                owner._pending.Add(this);
            }

            public override bool SkipWhenAbandoned
            {
                get { return true; }
            }

            public override void Poll(bool updateObserved)
            {
                if (IsComplete)
                {
                    return;
                }

                BridgeInputExecutor owner = Owner;
                if (updateObserved)
                {
                    int remaining = owner.VerifyAwaiting(null, out string problem);
                    if (remaining == 0)
                    {
                        Verified = true;
                        Succeed();
                        return;
                    }

                    _lastProblem = problem;
                }

                if (Due)
                {
                    Expire(_lastProblem);
                }
            }

            public override void Expire(string reason)
            {
                Verified = false;
                string message = reason ?? "no input update was observed";
                Fail(_queueProblem == null ? message : message + "; " + _queueProblem);
            }
        }
    }
}
