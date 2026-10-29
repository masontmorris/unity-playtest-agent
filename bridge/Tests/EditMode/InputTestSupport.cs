// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Tests
{
    /// <summary>A wall clock the test moves by hand, so measured durations are exact.</summary>
    internal sealed class TestClock
    {
        private long _ticks = new DateTime(2026, 10, 28, 12, 0, 0, DateTimeKind.Utc).Ticks;

        public DateTime Now
        {
            get { return new DateTime(Interlocked.Read(ref _ticks), DateTimeKind.Utc); }
        }

        public void Advance(double seconds)
        {
            Interlocked.Add(ref _ticks, TimeSpan.FromSeconds(seconds).Ticks);
        }

        public Func<DateTime> AsFunc()
        {
            return () => Now;
        }
    }

    /// <summary>
    /// A simulated keyboard, mouse and gamepad. Queued events only reach the device state on the
    /// next <see cref="Tick"/>, exactly like the Input System: a mouse delta resets at the start of
    /// the next update, and every state is replaced as a whole. Tests flip the switches below to
    /// simulate a device that ignores input or never releases it.
    /// </summary>
    internal sealed class FakeInputSurface : IBridgeInputSurface
    {
        private readonly object _gate = new object();
        private readonly List<Action> _queued = new List<Action>();
        private readonly List<string> _log = new List<string>();
        private readonly HashSet<string> _keys = new HashSet<string>(StringComparer.Ordinal);
        private readonly HashSet<string> _mouseButtons = new HashSet<string>(StringComparer.Ordinal);
        private readonly HashSet<string> _gamepadButtons = new HashSet<string>(StringComparer.Ordinal);
        private readonly Dictionary<string, double[]> _gamepadValues = new Dictionary<string, double[]>(StringComparer.Ordinal);
        private double _deltaX;
        private double _deltaY;
        private long _updateCount;
        private int _inUpdate;
        private int _readsOutsideUpdate;
        private int _flushes;

        /// <summary>Raised on each update, like the Input System's after-update callback.</summary>
        public Action UpdateObserved;

        /// <summary>The device consumes queued events without changing state, like a disabled device.</summary>
        public volatile bool DropQueuedEvents;

        /// <summary>Neutral events are ignored, like a stuck input.</summary>
        public volatile bool IgnoreNeutral;

        /// <summary>TryQueue refuses for this device, like a device that is missing.</summary>
        public volatile string FailQueueDevice;

        /// <summary>Neutral events for this device are refused by the surface.</summary>
        public volatile string FailNeutralDevice;

        /// <summary>The mouse reports this multiple of the queued delta.</summary>
        public double MouseDeltaScale = 1;

        /// <summary>A keyboard key name this surface rejects when validating.</summary>
        public string InvalidKey;

        public bool IsAvailable
        {
            get { return true; }
        }

        public long UpdateCount
        {
            get { return Interlocked.Read(ref _updateCount); }
        }

        public int ReadsOutsideUpdate
        {
            get { return Volatile.Read(ref _readsOutsideUpdate); }
        }

        public int Flushes
        {
            get { return Volatile.Read(ref _flushes); }
        }

        /// <summary>Events in the order they were queued: "queue Keyboard w+a", "neutral Mouse".</summary>
        public IReadOnlyList<string> Log
        {
            get
            {
                lock (_gate)
                {
                    return _log.Where(entry => entry.StartsWith("queue ", StringComparison.Ordinal) || entry.StartsWith("neutral ", StringComparison.Ordinal)).ToList();
                }
            }
        }

        public IReadOnlyCollection<string> Keys
        {
            get { lock (_gate) { return _keys.OrderBy(k => k, StringComparer.Ordinal).ToList(); } }
        }

        public IReadOnlyCollection<string> MouseButtons
        {
            get { lock (_gate) { return _mouseButtons.OrderBy(k => k, StringComparer.Ordinal).ToList(); } }
        }

        public IReadOnlyCollection<string> GamepadButtons
        {
            get { lock (_gate) { return _gamepadButtons.OrderBy(k => k, StringComparer.Ordinal).ToList(); } }
        }

        public double[] GamepadValue(string control)
        {
            lock (_gate)
            {
                return _gamepadValues.TryGetValue(control, out double[] value) ? (double[])value.Clone() : new[] { 0.0, 0.0 };
            }
        }

        public double[] MouseDelta
        {
            get { lock (_gate) { return new[] { _deltaX, _deltaY }; } }
        }

        /// <summary>Releases everything directly, like a person letting go.</summary>
        public void ForceNeutral()
        {
            lock (_gate)
            {
                ClearAll();
            }
        }

        public bool TryValidateControl(BridgeControlInfo info, out string error)
        {
            error = null;
            if (info.Device == BridgeInputVocabulary.Keyboard
                && InvalidKey != null
                && string.Equals(info.Control, InvalidKey, StringComparison.OrdinalIgnoreCase))
            {
                error = "unknown key '" + info.Control + "'";
                return false;
            }

            return true;
        }

        public bool TryQueue(string device, IReadOnlyList<BridgeInputBinding> inputs, out string error)
        {
            error = null;
            if (FailQueueDevice != null && string.Equals(FailQueueDevice, device, StringComparison.Ordinal))
            {
                error = "the fake " + device + " refused the input";
                return false;
            }

            var copy = inputs.ToList();
            lock (_gate)
            {
                _log.Add("queue " + device + " " + string.Join("+", copy.Select(i => i.Control)));
                _queued.Add(() => ApplyState(device, copy));
            }

            return true;
        }

        public bool TryQueueNeutral(string device, IReadOnlyList<BridgeInputBinding> inputs, out string error)
        {
            error = null;
            if (FailNeutralDevice != null && string.Equals(FailNeutralDevice, device, StringComparison.Ordinal))
            {
                error = "the fake " + device + " refused neutral";
                lock (_gate) _log.Add("neutral " + device);
                return false;
            }

            lock (_gate)
            {
                _log.Add("neutral " + device);
                _queued.Add(() =>
                {
                    if (IgnoreNeutral)
                    {
                        return;
                    }

                    ReleaseDevice(device);
                });
            }

            return true;
        }

        public bool TryRead(BridgeInputBinding input, out BridgeInputReading reading, out string error)
        {
            error = null;
            reading = default(BridgeInputReading);
            if (Volatile.Read(ref _inUpdate) == 0)
            {
                Interlocked.Increment(ref _readsOutsideUpdate);
            }

            if (!BridgeInputVocabulary.TryResolve(input.Device, input.Control, out BridgeControlInfo info, out error))
            {
                return false;
            }

            lock (_gate)
            {
                if (info.Device == BridgeInputVocabulary.Keyboard)
                {
                    reading.Pressed = _keys.Contains(input.Control.ToLowerInvariant());
                    return true;
                }

                if (info.Device == BridgeInputVocabulary.Mouse)
                {
                    if (info.Kind == BridgeControlKind.Impulse)
                    {
                        reading.X = _deltaX;
                        reading.Y = _deltaY;
                    }
                    else
                    {
                        reading.Pressed = _mouseButtons.Contains(info.Control);
                    }

                    return true;
                }

                if (info.Kind == BridgeControlKind.Button)
                {
                    reading.Pressed = _gamepadButtons.Contains(info.Control);
                }
                else if (info.Kind == BridgeControlKind.Dpad)
                {
                    reading.X = (_gamepadButtons.Contains("dpadRight") ? 1 : 0) - (_gamepadButtons.Contains("dpadLeft") ? 1 : 0);
                    reading.Y = (_gamepadButtons.Contains("dpadUp") ? 1 : 0) - (_gamepadButtons.Contains("dpadDown") ? 1 : 0);
                }
                else if (_gamepadValues.TryGetValue(info.Control, out double[] value))
                {
                    reading.X = value[0];
                    reading.Y = value[1];
                }

                return true;
            }
        }

        public void Flush()
        {
            Interlocked.Increment(ref _flushes);
            Tick();
        }

        /// <summary>One input update: the mouse delta resets, then queued events apply.</summary>
        public void Tick()
        {
            List<Action> events;
            lock (_gate)
            {
                _deltaX = 0;
                _deltaY = 0;
                events = new List<Action>(_queued);
                _queued.Clear();
                if (!DropQueuedEvents)
                {
                    foreach (Action apply in events)
                    {
                        apply();
                    }
                }
            }

            Interlocked.Increment(ref _updateCount);
            Interlocked.Exchange(ref _inUpdate, 1);
            try
            {
                UpdateObserved?.Invoke();
            }
            finally
            {
                Interlocked.Exchange(ref _inUpdate, 0);
            }
        }

        private void ApplyState(string device, List<BridgeInputBinding> inputs)
        {
            if (device == BridgeInputVocabulary.Keyboard)
            {
                _keys.Clear();
                foreach (BridgeInputBinding input in inputs)
                {
                    _keys.Add(input.Control.ToLowerInvariant());
                }

                return;
            }

            if (device == BridgeInputVocabulary.Mouse)
            {
                _mouseButtons.Clear();
                foreach (BridgeInputBinding input in inputs)
                {
                    BridgeInputVocabulary.TryResolve(input.Device, input.Control, out BridgeControlInfo info, out _);
                    if (info.Kind == BridgeControlKind.Impulse)
                    {
                        _deltaX += input.X.Value * BridgeInputVocabulary.MouseDeltaPixelsPerUnit * MouseDeltaScale;
                        _deltaY += input.Y.Value * BridgeInputVocabulary.MouseDeltaPixelsPerUnit * MouseDeltaScale;
                    }
                    else
                    {
                        _mouseButtons.Add(info.Control);
                    }
                }

                return;
            }

            _gamepadButtons.Clear();
            _gamepadValues.Clear();
            foreach (BridgeInputBinding input in inputs)
            {
                BridgeInputVocabulary.TryResolve(input.Device, input.Control, out BridgeControlInfo info, out _);
                switch (info.Kind)
                {
                    case BridgeControlKind.Button:
                        _gamepadButtons.Add(info.Control);
                        break;
                    case BridgeControlKind.Dpad:
                        if (input.Y.Value > 0) _gamepadButtons.Add("dpadUp");
                        if (input.Y.Value < 0) _gamepadButtons.Add("dpadDown");
                        if (input.X.Value < 0) _gamepadButtons.Add("dpadLeft");
                        if (input.X.Value > 0) _gamepadButtons.Add("dpadRight");
                        break;
                    default:
                        _gamepadValues[info.Control] = new[] { input.X.Value, info.Kind == BridgeControlKind.Axis ? 0 : input.Y.Value };
                        break;
                }
            }
        }

        private void ReleaseDevice(string device)
        {
            if (device == BridgeInputVocabulary.Keyboard)
            {
                _keys.Clear();
            }
            else if (device == BridgeInputVocabulary.Mouse)
            {
                _mouseButtons.Clear();
            }
            else
            {
                _gamepadButtons.Clear();
                _gamepadValues.Clear();
            }
        }

        private void ClearAll()
        {
            _keys.Clear();
            _mouseButtons.Clear();
            _gamepadButtons.Clear();
            _gamepadValues.Clear();
            _deltaX = 0;
            _deltaY = 0;
        }
    }

    /// <summary>
    /// A thread standing in for the Unity main thread. Each pass it runs invocations, pumps the
    /// executor and ticks the simulated device, like the Editor update followed by the game loop.
    /// </summary>
    internal sealed class FakeMainLoop : IDisposable
    {
        private readonly Thread _thread;
        private readonly BlockingCollection<Action> _invocations = new BlockingCollection<Action>();
        private readonly ManualResetEventSlim _started = new ManualResetEventSlim(false);
        private volatile bool _stop;
        private int _threadId;

        public FakeMainLoop()
        {
            _thread = new Thread(Run) { IsBackground = true, Name = "fake-main-thread" };
            _thread.Start();
            _started.Wait();
        }

        public int ThreadId
        {
            get { return _threadId; }
        }

        public BridgeInputExecutor Executor;

        public FakeInputSurface Surface;

        /// <summary>While true the loop only runs invocations: a stalled main thread.</summary>
        public volatile bool Paused;

        /// <summary>Tick the device each pass. When false only a manual flush updates it.</summary>
        public volatile bool AutoTick = true;

        public T Invoke<T>(Func<T> work)
        {
            T result = default(T);
            Exception failure = null;
            using (var done = new ManualResetEventSlim(false))
            {
                _invocations.Add(() =>
                {
                    try
                    {
                        result = work();
                    }
                    catch (Exception ex)
                    {
                        failure = ex;
                    }
                    finally
                    {
                        done.Set();
                    }
                });
                if (!done.Wait(10000))
                {
                    throw new TimeoutException("the fake main thread did not run the invocation: it is blocked");
                }
            }

            if (failure != null)
            {
                throw new InvalidOperationException("the invocation failed on the fake main thread", failure);
            }

            return result;
        }

        public void Invoke(Action work)
        {
            Invoke<bool>(() =>
            {
                work();
                return true;
            });
        }

        public void Dispose()
        {
            _stop = true;
            _invocations.CompleteAdding();
            _thread.Join(2000);
        }

        private void Run()
        {
            _threadId = Thread.CurrentThread.ManagedThreadId;
            _started.Set();
            while (!_stop)
            {
                while (_invocations.TryTake(out Action work))
                {
                    work();
                }

                if (!Paused)
                {
                    Executor?.Pump();
                    if (AutoTick)
                    {
                        Surface?.Tick();
                    }
                }

                Thread.Sleep(1);
            }
        }
    }

    /// <summary>A scheduler the test fires by hand. A disposed timer never fires.</summary>
    internal sealed class ManualScheduler : IBridgeScheduler
    {
        private readonly object _gate = new object();
        private readonly List<Item> _items = new List<Item>();

        public IDisposable Schedule(TimeSpan delay, Action callback)
        {
            var item = new Item { Delay = delay, Callback = callback };
            lock (_gate)
            {
                _items.Add(item);
            }

            return item;
        }

        public IReadOnlyList<TimeSpan> PendingDelays
        {
            get
            {
                lock (_gate)
                {
                    return _items.Where(i => !i.Done).Select(i => i.Delay).ToList();
                }
            }
        }

        public int Pending
        {
            get { return PendingDelays.Count; }
        }

        /// <summary>Every scheduled callback, fired or not, so a test can fire a stale one.</summary>
        public IReadOnlyList<Action> AllCallbacks
        {
            get
            {
                lock (_gate)
                {
                    return _items.Select(i => i.Callback).ToList();
                }
            }
        }

        /// <summary>Fires the earliest scheduled live timer. Returns false when none is pending.</summary>
        public bool FireNext()
        {
            return FireWhere(_ => true);
        }

        /// <summary>Fires the first live timer whose delay satisfies the predicate.</summary>
        public bool FireWhere(Func<TimeSpan, bool> predicate)
        {
            Item chosen = null;
            lock (_gate)
            {
                chosen = _items.FirstOrDefault(i => !i.Done && predicate(i.Delay));
                if (chosen != null)
                {
                    chosen.Done = true;
                }
            }

            if (chosen == null)
            {
                return false;
            }

            chosen.Callback();
            return true;
        }

        private sealed class Item : IDisposable
        {
            public TimeSpan Delay;

            public Action Callback;

            public bool Done;

            public void Dispose()
            {
                Done = true;
            }
        }
    }

    /// <summary>Everything a lease test needs: a simulated device behind a real executor and lease.</summary>
    internal sealed class InputRig : IDisposable
    {
        public readonly TestClock Clock = new TestClock();
        public readonly FakeInputSurface Surface = new FakeInputSurface();
        public readonly FakeMainLoop Loop = new FakeMainLoop();
        public readonly ManualScheduler Scheduler = new ManualScheduler();
        public readonly List<ExecutionRecordMessage> Records = new List<ExecutionRecordMessage>();
        public readonly List<string> Logs = new List<string>();
        public readonly BridgeInputExecutor Executor;
        public readonly BridgeActionLease Lease;

        public InputRig(
            int operationTimeoutMs = 3000,
            bool realScheduler = false,
            Action<ExecutionRecordMessage> onRecord = null,
            bool realClock = false)
        {
            Func<DateTime> now = realClock ? (Func<DateTime>)(() => DateTime.UtcNow) : Clock.AsFunc();
            Executor = new BridgeInputExecutor(
                Surface,
                Loop.ThreadId,
                now,
                new BridgeInputExecutorOptions { OperationTimeoutMs = operationTimeoutMs, VerifyTimeoutMs = 300 });
            Surface.UpdateObserved = Executor.OnGameInputUpdate;
            Loop.Executor = Executor;
            Loop.Surface = Surface;
            Lease = new BridgeActionLease(
                Executor,
                record =>
                {
                    lock (Records)
                    {
                        Records.Add(record);
                    }

                    onRecord?.Invoke(record);
                },
                message =>
                {
                    lock (Logs)
                    {
                        Logs.Add(message);
                    }
                },
                now,
                realScheduler ? null : Scheduler);
        }

        public List<ExecutionRecordMessage> Snapshot()
        {
            lock (Records)
            {
                return new List<ExecutionRecordMessage>(Records);
            }
        }

        public ExecutionRecordMessage Last(string status)
        {
            return Snapshot().LastOrDefault(r => r.Status == status);
        }

        public bool WaitFor(Func<bool> condition, int timeoutMs = 5000)
        {
            DateTime deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
            while (DateTime.UtcNow < deadline)
            {
                if (condition())
                {
                    return true;
                }

                Thread.Sleep(5);
            }

            return condition();
        }

        public void Dispose()
        {
            Lease.Dispose();
            Loop.Dispose();
        }

        public static ActRequestMessage Request(string id, double? leaseS = null)
        {
            return new ActRequestMessage { ActionId = id, LeaseS = leaseS };
        }

        public static BridgeActionDefinition Action(string name, double leaseS, params BridgeInputBinding[] inputs)
        {
            var definition = new BridgeActionDefinition { Name = name, DefaultLeaseS = leaseS };
            definition.Inputs.AddRange(inputs);
            return definition;
        }

        public static BridgeActionDefinition Skill(string name, double maxS, params BridgeActionStep[] steps)
        {
            var definition = new BridgeActionDefinition { Name = name, MaxS = maxS };
            definition.Steps.AddRange(steps);
            return definition;
        }

        public static BridgeInputBinding Key(string key)
        {
            return new BridgeInputBinding("Keyboard", key);
        }

        public static string Describe(BridgeInputReceipt receipt)
        {
            return receipt.StepIndex.ToString(CultureInfo.InvariantCulture) + ":" + receipt.Device + "." + receipt.Control;
        }
    }
}
