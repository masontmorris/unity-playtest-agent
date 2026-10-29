using System;
using System.Threading;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Tests
{
    [TestFixture]
    public sealed class BridgeActionLeaseTests
    {
        [TestCase(false)]
        [TestCase(true)]
        public void Expiry_CountsRunningRecordWorkInsideTheRequestedHold(bool explicitLease)
        {
            const double requestedS = 0.8;
            var input = new FakeInputBackend();
            var scheduler = new RecordingScheduler();
            ExecutionRecordMessage terminal = null;
            using (var completed = new ManualResetEventSlim())
            using (var lease = new BridgeActionLease(input, record =>
            {
                if (record.Status == BridgeExecStatus.Running)
                {
                    // Amplify cold record construction/serialization without depending on JIT
                    // speed. The wide margin catches an extra full lease, not scheduler jitter.
                    Thread.Sleep(350);
                }
                else
                {
                    terminal = record;
                    completed.Set();
                }
            }, scheduler: scheduler))
            {
                Assert.That(lease.TryStart(
                    new ActRequestMessage { ActionId = "lease-test", LeaseS = explicitLease ? requestedS : (double?)null },
                    new BridgeActionDefinition { Name = "binding", Control = "w", DefaultLeaseS = explicitLease ? 2 : requestedS },
                    7, out string error), Is.True, error);
                Assert.That(completed.Wait(5000), Is.True, "the lease never completed");
                TestContext.WriteLine("requested_s={0:R} applied_s={1:R} overshoot_ms={2:F4}",
                    requestedS, terminal.AppliedDurationS, (terminal.AppliedDurationS - requestedS) * 1000);
                Assert.That(terminal.Status, Is.EqualTo(BridgeExecStatus.Completed));
                Assert.That(terminal.AppliedDurationS, Is.GreaterThanOrEqualTo(requestedS - 0.02));
                if (scheduler.DelayS.HasValue)
                    Assert.That(scheduler.DelayS.Value, Is.InRange(0, requestedS - 0.3),
                        "the scheduled delay must deduct the 350 ms record work, regardless of timer dispatch jitter");
                Assert.That(terminal.InputEndWallS - terminal.InputStartWallS,
                    Is.EqualTo(terminal.AppliedDurationS.Value).Within(0.000001),
                    "the reported duration must still measure start through release");
                Assert.That(input.Releases, Is.EqualTo(1));
            }
        }

        private sealed class RecordingScheduler : IBridgeScheduler
        {
            public double? DelayS;

            public IDisposable Schedule(TimeSpan delay, Action callback)
            {
                DelayS = delay.TotalSeconds;
                return new BridgeThreadingScheduler().Schedule(delay, callback);
            }
        }

        [Test]
        public void Expiry_AlreadyDueAfterRunningRecord_ReleasesBeforeStartReturns()
        {
            var input = new FakeInputBackend();
            ExecutionRecordMessage terminal = null;
            using (var lease = new BridgeActionLease(input, record =>
            {
                if (record.Status == BridgeExecStatus.Running)
                {
                    Thread.Sleep(150);
                }
                else
                {
                    terminal = record;
                }
            }))
            {
                Assert.That(lease.TryStart(
                    new ActRequestMessage { ActionId = "already-due", LeaseS = 0.05 },
                    new BridgeActionDefinition { Name = "binding", Control = "w" },
                    8, out string error), Is.True, error);
                Assert.That(lease.IsActive, Is.False, "an elapsed lease must not get another full timer");
                Assert.That(input.Releases, Is.EqualTo(1));
                Assert.That(terminal.Status, Is.EqualTo(BridgeExecStatus.Completed));
                Assert.That(terminal.AppliedDurationS, Is.GreaterThan(0.1),
                    "an unavoidable overrun must be reported, not clamped to the lease");
            }
        }

        [Test]
        public void Stop_ReportsMeasuredHoldInsteadOfTheConfiguredLease()
        {
            DateTime now = new DateTime(2026, 10, 28, 0, 0, 0, DateTimeKind.Utc);
            ExecutionRecordMessage terminal = null;
            using (var lease = new BridgeActionLease(new FakeInputBackend(), record => terminal = record, utcNow: () => now))
            {
                Assert.That(lease.TryStart(
                    new ActRequestMessage { ActionId = "cancelled", LeaseS = 10 },
                    new BridgeActionDefinition { Name = "binding", Control = "w" },
                    9, out string error), Is.True, error);
                now = now.AddSeconds(0.123);
                lease.Stop("test");
                Assert.That(terminal.Status, Is.EqualTo(BridgeExecStatus.Cancelled));
                Assert.That(terminal.AppliedDurationS, Is.EqualTo(0.123).Within(0.0000001));
            }
        }
    }
}
