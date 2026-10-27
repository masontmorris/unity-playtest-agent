using System;
using System.Threading;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Actions
{
    /// <summary>
    /// Owns the one held input lease. A new action releases the previous lease, emits its
    /// terminal record, applies the new binding, emits a running record and completes or cancels
    /// it on expiry, supersede or stop. The lease never chooses an action; it executes the one it
    /// is given and reports measured timing.
    /// </summary>
    public sealed class BridgeActionLease : IDisposable
    {
        public const double MaxLeaseSeconds = 3600;

        private readonly IBridgeInputBackend _backend;
        private readonly Action<ExecutionRecordMessage> _emit;
        private readonly Func<DateTime> _utcNow;
        private readonly Action<string> _log;
        private readonly object _gate = new object();
        private Active _active;
        private bool _disposed;

        public BridgeActionLease(
            IBridgeInputBackend backend,
            Action<ExecutionRecordMessage> emit,
            Action<string> log = null,
            Func<DateTime> utcNow = null)
        {
            _backend = backend ?? throw new ArgumentNullException(nameof(backend));
            _emit = emit ?? throw new ArgumentNullException(nameof(emit));
            _log = log;
            _utcNow = utcNow ?? (() => DateTime.UtcNow);
        }

        public bool IsActive
        {
            get
            {
                lock (_gate)
                {
                    return _active != null;
                }
            }
        }

        public string ActiveActionId
        {
            get
            {
                lock (_gate)
                {
                    return _active?.Request.ActionId;
                }
            }
        }

        /// <summary>
        /// Starts a validated action. Any current lease is released and reported cancelled first;
        /// a backend failure after that leaves no input held. Returns false with an error for a
        /// backend rejection. Freshness is the caller's responsibility and must be checked before
        /// this method is called.
        /// </summary>
        public bool TryStart(
            ActRequestMessage request,
            BridgeActionDefinition action,
            long validatedAgainstSeq,
            out string error)
        {
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

            double leaseS = request.LeaseS ?? action.DefaultLeaseS;
            if (leaseS <= 0 || double.IsNaN(leaseS) || double.IsInfinity(leaseS))
            {
                error = "lease_s must be positive";
                return false;
            }

            if (leaseS > MaxLeaseSeconds)
            {
                error = "lease_s must be at most " + MaxLeaseSeconds.ToString("0") + " s";
                return false;
            }

            lock (_gate)
            {
                if (_disposed)
                {
                    error = "the input backend is disposed";
                    return false;
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

                EndActiveLocked(BridgeExecStatus.Cancelled);
                try
                {
                    if (!_backend.TryApply(action, out error))
                    {
                        return false;
                    }
                }
                catch (Exception ex)
                {
                    error = ex.GetType().Name + ": " + ex.Message;
                    return false;
                }

                DateTime start = _utcNow();
                var active = new Active
                {
                    Request = request,
                    Action = action,
                    StartUtc = start,
                    ValidatedAgainstSeq = validatedAgainstSeq,
                };
                _active = active;

                _emit(BuildTimingRecord(
                    active,
                    BridgeExecStatus.Running,
                    start,
                    null,
                    null));

                active.Timer = new Timer(OnExpiry, active, TimeSpan.FromSeconds(leaseS), Timeout.InfiniteTimeSpan);
                return true;
            }
        }

        /// <summary>
        /// Releases the current lease. When <paramref name="emitCancelled"/> is false, a
        /// disconnected client gets no record.
        /// </summary>
        public void Stop(string reason, bool emitCancelled = true)
        {
            lock (_gate)
            {
                if (_active == null)
                {
                    return;
                }

                Active active = _active;
                _active = null;
                DisposeTimer(active);
                bool released = Release(out bool verified, out string releaseError);
                if (!verified)
                {
                    _log?.Invoke("releasing input failed: " + releaseError);
                }

                if (emitCancelled)
                {
                    DateTime end = _utcNow();
                    _emit(BuildTimingRecord(
                        active,
                        (released && verified) ? BridgeExecStatus.Cancelled : BridgeExecStatus.Failed,
                        active.StartUtc,
                        end,
                        verified));
                }
            }
        }

        public void Dispose()
        {
            lock (_gate)
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

        private void OnExpiry(object state)
        {
            var active = state as Active;
            if (active == null)
            {
                return;
            }

            lock (_gate)
            {
                if (_disposed || !ReferenceEquals(_active, active))
                {
                    return;
                }

                _active = null;
                DisposeTimer(active);
                bool released = Release(out bool verified, out string releaseError);
                if (!verified)
                {
                    _log?.Invoke("releasing input failed: " + releaseError);
                }

                DateTime end = _utcNow();
                _emit(BuildTimingRecord(
                    active,
                    (released && verified) ? BridgeExecStatus.Completed : BridgeExecStatus.Failed,
                    active.StartUtc,
                    end,
                    verified));
            }
        }

        private void EndActiveLocked(string status)
        {
            if (_active == null)
            {
                return;
            }

            Active previous = _active;
            _active = null;
            DisposeTimer(previous);
            bool released = Release(out bool verified, out string releaseError);
            if (!verified)
            {
                _log?.Invoke("releasing superseded input failed: " + releaseError);
            }

            DateTime end = _utcNow();
            _emit(BuildTimingRecord(
                previous,
                (released && verified) ? status : BridgeExecStatus.Failed,
                previous.StartUtc,
                end,
                verified));
        }

        private bool Release(out bool verified, out string error)
        {
            verified = false;
            try
            {
                bool released = _backend.TryRelease(out verified, out error);
                if (!released)
                {
                    verified = false;
                }

                return released;
            }
            catch (Exception ex)
            {
                verified = false;
                error = ex.GetType().Name + ": " + ex.Message;
                return false;
            }
        }

        private ExecutionRecordMessage BuildTimingRecord(
            Active active,
            string status,
            DateTime start,
            DateTime? end,
            bool? releaseVerified)
        {
            double? duration = null;
            if (end.HasValue)
            {
                double seconds = (end.Value - start).TotalSeconds;
                duration = seconds < 0 ? 0 : seconds;
            }

            return new ExecutionRecordMessage
            {
                Session = string.Empty,
                Seq = 0,
                WallUtc = _utcNow(),
                ActionId = active.Request.ActionId,
                Status = status,
                ValidatedAgainstSeq = active.ValidatedAgainstSeq,
                InputStartWallS = ToUnixSeconds(start),
                InputEndWallS = end.HasValue ? ToUnixSeconds(end.Value) : (double?)null,
                AppliedDurationS = duration,
                ReleaseVerified = releaseVerified,
                ExpectedEffect = new System.Collections.Generic.List<BridgeEffect>(),
                ObservedEffect = new System.Collections.Generic.List<BridgeEffect>(),
            };
        }

        private static double ToUnixSeconds(DateTime value)
        {
            DateTime utc = value.Kind == DateTimeKind.Utc ? value : value.ToUniversalTime();
            return (utc - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
        }

        private void DisposeTimer(Active active)
        {
            Timer timer = active.Timer;
            active.Timer = null;
            if (timer != null)
            {
                timer.Dispose();
            }
        }

        private sealed class Active
        {
            public ActRequestMessage Request;

            public BridgeActionDefinition Action;

            public DateTime StartUtc;

            public long ValidatedAgainstSeq;

            public Timer Timer;
        }
    }
}
