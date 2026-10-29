using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;

namespace Playtest.Bridge.Actions
{
    /// <summary>
    /// Measured evidence for one input applied by an action or skill step. The backend writes it
    /// from device-state reads; a field it could not measure stays null.
    /// </summary>
    public sealed class BridgeInputReceipt
    {
        public int StepIndex { get; set; }

        public string Device { get; set; }

        public string Control { get; set; }

        /// <summary>The declared x, copied from the input.</summary>
        public double? X { get; set; }

        /// <summary>The declared y, copied from the input.</summary>
        public double? Y { get; set; }

        /// <summary>When a device read confirmed the input applied.</summary>
        public DateTime? StartUtc { get; set; }

        /// <summary>When a device read confirmed the input released.</summary>
        public DateTime? EndUtc { get; set; }

        /// <summary>Confirmed apply to confirmed release. Null for a mouse impulse and when unmeasured.</summary>
        public double? AppliedDurationS { get; set; }

        /// <summary>The pixel delta the device read back for a mouse impulse.</summary>
        public double? AppliedDeltaX { get; set; }

        public double? AppliedDeltaY { get; set; }

        /// <summary>True when a device read showed the input neutral, false when it showed it held.</summary>
        public bool? ReleaseVerified { get; set; }

        /// <summary>The input's position in its step's declaration, so receipts report in declared order.</summary>
        internal int Order { get; set; }

        public BridgeInputReceipt Clone()
        {
            return (BridgeInputReceipt)MemberwiseClone();
        }
    }

    /// <summary>The instant a backend confirmed a step's inputs applied.</summary>
    public sealed class BridgeInputAck
    {
        public BridgeInputAck(DateTime appliedUtc, long timestamp)
        {
            AppliedUtc = appliedUtc;
            Timestamp = timestamp;
        }

        /// <summary>Wall clock of the confirming device read.</summary>
        public DateTime AppliedUtc { get; }

        /// <summary><see cref="System.Diagnostics.Stopwatch.GetTimestamp"/> at the same instant.</summary>
        public long Timestamp { get; }
    }

    /// <summary>
    /// One action or skill execution as the input backend sees it. The lease owns the run: it
    /// cancels the run before releasing, and a cancelled run can no longer apply input, so a queued
    /// apply that reaches the device late does nothing. The backend writes a receipt for every input
    /// it hands to a device, before it hands it over; receipts are only read through
    /// <see cref="Snapshot"/>. A run with no receipt therefore never reached a device.
    /// </summary>
    public sealed class BridgeInputRun
    {
        private readonly List<BridgeInputReceipt> _receipts = new List<BridgeInputReceipt>();
        private int _cancelled;
        private int _outstanding;
        private int _inFlight;

        public BridgeInputRun(string actionName)
        {
            ActionName = actionName;
        }

        public string ActionName { get; }

        /// <summary>Guards the receipts. Backends take it while changing a receipt.</summary>
        public object SyncRoot { get; } = new object();

        public bool IsCancelled
        {
            get { return Volatile.Read(ref _cancelled) != 0; }
        }

        /// <summary>Inputs this run applied that are not yet confirmed neutral.</summary>
        public int Outstanding
        {
            get { return Volatile.Read(ref _outstanding); }
        }

        /// <summary>Operations submitted for this run that have not finished.</summary>
        public int InFlight
        {
            get { return Volatile.Read(ref _inFlight); }
        }

        /// <summary>True once the backend registered an input of this run, so a device may hold it.</summary>
        public bool HasReceipts
        {
            get
            {
                lock (SyncRoot)
                {
                    return _receipts.Count > 0;
                }
            }
        }

        public void Cancel()
        {
            Interlocked.Exchange(ref _cancelled, 1);
        }

        /// <summary>
        /// Adds a receipt for an input about to be handed to a device. Backend use. <paramref name="order"/>
        /// is the input's position in its step, which keeps the receipts in declared order even when
        /// the backend hands inputs over one device at a time.
        /// </summary>
        public BridgeInputReceipt AddReceipt(int stepIndex, BridgeInputBinding input, int order = 0)
        {
            var receipt = new BridgeInputReceipt
            {
                StepIndex = stepIndex,
                Device = BridgeInputVocabulary.TryCanonicalDevice(input.Device, out string canonical) ? canonical : input.Device,
                Control = input.Control,
                X = input.X,
                Y = input.Y,
                Order = order,
            };
            lock (SyncRoot)
            {
                _receipts.Add(receipt);
            }

            return receipt;
        }

        /// <summary>
        /// Takes back a receipt for an input the device layer reported it never queued. Backend use;
        /// an input that may have reached a device keeps its receipt.
        /// </summary>
        public void RemoveReceipt(BridgeInputReceipt receipt)
        {
            lock (SyncRoot)
            {
                _receipts.Remove(receipt);
            }
        }

        /// <summary>A copy of the receipts: step by step, each step in its declared input order.</summary>
        public IReadOnlyList<BridgeInputReceipt> Snapshot()
        {
            lock (SyncRoot)
            {
                return _receipts
                    .OrderBy(receipt => receipt.StepIndex)
                    .ThenBy(receipt => receipt.Order)
                    .Select(receipt => receipt.Clone())
                    .ToList();
            }
        }

        internal void AddOutstanding(int count)
        {
            Interlocked.Add(ref _outstanding, count);
        }

        internal void AddInFlight(int count)
        {
            Interlocked.Add(ref _inFlight, count);
        }
    }
}
