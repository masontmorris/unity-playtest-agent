// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The lease state machine for chords and bounded skills, run against the real executor and a
    /// simulated device. Timers are fired by hand, so ordering is exact and nothing sleeps for a
    /// hold; the threading tests at the end use real threads and a real scheduler.
    /// </summary>
    [TestFixture]
    public sealed class BridgeActionLeaseSkillTests
    {
        private InputRig _rig;

        [SetUp]
        public void SetUp()
        {
            _rig = new InputRig(operationTimeoutMs: 400);
        }

        [TearDown]
        public void TearDown()
        {
            _rig.Dispose();
        }

        [Test]
        public void Chord_AppliesBothKeysInOneStateAndReportsMeasuredReceiptsPerInput()
        {
            DateTime start = _rig.Clock.Now;
            BridgeActionDefinition chord = InputRig.Action("strafe", 0.4, InputRig.Key("w"), InputRig.Key("a"));

            Assert.That(_rig.Lease.TryStart(InputRig.Request("a1"), chord, 7, out string error), Is.True, error);

            Assert.That(_rig.Surface.Log, Is.EqualTo(new[] { "queue Keyboard w+a" }), "the chord is one atomic apply");
            ExecutionRecordMessage running = _rig.Last(BridgeExecStatus.Running);
            Assert.That(running.AppliedInputs.Select(i => i.Control), Is.EqualTo(new[] { "w", "a" }));
            Assert.That(running.AppliedInputs.All(i => i.InputStartWallS == Unix(start) && i.InputEndWallS == null && i.AppliedDurationS == null), Is.True);

            _rig.Clock.Advance(0.4);
            Assert.That(_rig.Scheduler.FireWhere(d => d.TotalSeconds > 0.3 && d.TotalSeconds <= 0.4), Is.True);

            ExecutionRecordMessage done = _rig.Last(BridgeExecStatus.Completed);
            Assert.That(done, Is.Not.Null);
            Assert.That(done.ReleaseVerified, Is.True);
            Assert.That(done.AppliedDurationS, Is.EqualTo(0.4).Within(1e-9));
            Assert.That(done.AppliedInputs.Count, Is.EqualTo(2));
            foreach (AppliedInputReceipt receipt in done.AppliedInputs)
            {
                Assert.That(receipt.StepIndex, Is.Zero);
                Assert.That(receipt.AppliedDurationS, Is.EqualTo(0.4).Within(1e-9));
                Assert.That(receipt.InputEndWallS, Is.EqualTo(Unix(start) + 0.4).Within(1e-6));
                Assert.That(receipt.ReleaseVerified, Is.True);
            }

            Assert.That(_rig.Surface.Log, Is.EqualTo(new[] { "queue Keyboard w+a", "neutral Keyboard" }));
            Assert.That(_rig.Surface.Keys, Is.Empty);
            Assert.That(_rig.Lease.IsActive, Is.False);
            Assert.That(_rig.Scheduler.Pending, Is.Zero);
        }

        [Test]
        public void ReceiptDuration_IsMeasuredFromTheDeviceNeverCopiedFromTheConfiguredHold()
        {
            BridgeActionDefinition held = InputRig.Action("held", 10, InputRig.Key("w"));
            Assert.That(_rig.Lease.TryStart(InputRig.Request("a1"), held, 1, out string error), Is.True, error);

            _rig.Clock.Advance(0.25);
            Assert.That(_rig.Lease.Stop("user"), Is.True);

            ExecutionRecordMessage cancelled = _rig.Last(BridgeExecStatus.Cancelled);
            Assert.That(cancelled.AppliedDurationS, Is.EqualTo(0.25).Within(1e-9), "not the configured 10 s");
            Assert.That(cancelled.AppliedInputs.Single().AppliedDurationS, Is.EqualTo(0.25).Within(1e-9));
            Assert.That(cancelled.ReleaseVerified, Is.True);
        }

        [Test]
        public void Skill_RunsStepsInOrderReleasingAndVerifyingEachBeforeTheNextApplies()
        {
            DateTime start = _rig.Clock.Now;
            BridgeActionDefinition skill = InputRig.Skill(
                "combo",
                1.0,
                new BridgeActionStep(new[] { InputRig.Key("w") }, 0.2, 0.1),
                new BridgeActionStep(new[] { new BridgeInputBinding("Mouse", "delta", 0.5, 0) }, 0.05, 0),
                new BridgeActionStep(
                    new[] { new BridgeInputBinding("Gamepad", "leftStick", 0.25, 0.75), new BridgeInputBinding("Gamepad", "buttonSouth") },
                    0.3,
                    0));

            Assert.That(_rig.Lease.TryStart(InputRig.Request("s1"), skill, 3, out string error), Is.True, error);
            Assert.That(_rig.Surface.Log, Is.EqualTo(new[] { "queue Keyboard w" }));
            Assert.That(_rig.Scheduler.Pending, Is.EqualTo(2), "the step hold and the max_s deadline");

            _rig.Clock.Advance(0.2);
            Assert.That(FireNear(0.2), Is.True);
            Assert.That(_rig.Surface.Log, Is.EqualTo(new[] { "queue Keyboard w", "neutral Keyboard" }), "step 0 is released before anything else");
            Assert.That(_rig.Surface.Keys, Is.Empty);
            Assert.That(_rig.Last(BridgeExecStatus.Completed), Is.Null, "the skill is not done after one step");
            Assert.That(_rig.Scheduler.PendingDelays.Any(d => Math.Abs(d.TotalSeconds - 0.1) < 0.05), Is.True, "step 0 now waits 0.1 s");

            _rig.Clock.Advance(0.1);
            Assert.That(FireNear(0.1), Is.True);
            Assert.That(_rig.Surface.Log.Last(), Is.EqualTo("queue Mouse delta"), "step 1 applies only after the wait");

            _rig.Clock.Advance(0.05);
            Assert.That(FireNear(0.05), Is.True);
            Assert.That(
                _rig.Surface.Log,
                Is.EqualTo(new[]
                {
                    "queue Keyboard w", "neutral Keyboard", "queue Mouse delta", "neutral Mouse", "queue Gamepad leftStick+buttonSouth",
                }),
                "step 1 is released and step 2 applied, with no wait declared between them");
            Assert.That(_rig.Surface.GamepadValue("leftStick"), Is.EqualTo(new[] { 0.25, 0.75 }));

            _rig.Clock.Advance(0.3);
            Assert.That(FireNear(0.3), Is.True);

            ExecutionRecordMessage done = _rig.Last(BridgeExecStatus.Completed);
            Assert.That(done, Is.Not.Null);
            Assert.That(done.ReleaseVerified, Is.True);
            Assert.That(done.AppliedDurationS, Is.EqualTo(0.65).Within(1e-9), "first confirmed apply to last confirmed release");
            Assert.That(
                done.AppliedInputs.Select(i => i.StepIndex + ":" + i.Device + "." + i.Control),
                Is.EqualTo(new[] { "0:Keyboard.w", "1:Mouse.delta", "2:Gamepad.leftStick", "2:Gamepad.buttonSouth" }));
            Assert.That(done.AppliedInputs[0].AppliedDurationS, Is.EqualTo(0.2).Within(1e-9));
            Assert.That(done.AppliedInputs[1].AppliedDurationS, Is.Null, "an impulse has no duration");
            Assert.That(done.AppliedInputs[1].AppliedDeltaX, Is.EqualTo(32).Within(1e-9));
            Assert.That(done.AppliedInputs[1].AppliedDeltaY, Is.EqualTo(0).Within(1e-9));
            Assert.That(done.AppliedInputs[2].AppliedDurationS, Is.EqualTo(0.3).Within(1e-9));
            Assert.That(done.AppliedInputs[3].AppliedDurationS, Is.EqualTo(0.3).Within(1e-9));
            Assert.That(done.AppliedInputs.All(i => i.ReleaseVerified == true), Is.True);
            Assert.That(done.AppliedInputs[0].InputStartWallS, Is.EqualTo(Unix(start)).Within(1e-6));
            Assert.That(
                _rig.Surface.Log.Last(),
                Is.EqualTo("neutral Gamepad"));
            Assert.That(_rig.Snapshot().Select(r => r.Status), Is.EqualTo(new[] { BridgeExecStatus.Running, BridgeExecStatus.Completed }));
            Assert.That(_rig.Scheduler.Pending, Is.Zero, "the deadline timer was cancelled with the run");
            Assert.That(_rig.Surface.ReadsOutsideUpdate, Is.Zero);
        }

        [Test]
        public void ASkillStepThatCannotBeVerifiedReleasedStopsTheSequenceAndNeverAppliesTheNextStep()
        {
            BridgeActionDefinition skill = InputRig.Skill(
                "two_steps",
                5,
                new BridgeActionStep(new[] { InputRig.Key("w") }, 0.1, 0),
                new BridgeActionStep(new[] { InputRig.Key("a") }, 0.1, 0));
            Assert.That(_rig.Lease.TryStart(InputRig.Request("s1"), skill, 1, out string error), Is.True, error);
            _rig.Surface.IgnoreNeutral = true;

            Assert.That(FireNear(0.1), Is.True);

            ExecutionRecordMessage failed = _rig.Last(BridgeExecStatus.Failed);
            Assert.That(failed, Is.Not.Null, "an unverified release fails the run");
            Assert.That(failed.ReleaseVerified, Is.False);
            Assert.That(failed.AppliedInputs.Single().Control, Is.EqualTo("w"));
            Assert.That(failed.AppliedInputs.Single().ReleaseVerified, Is.False);
            Assert.That(_rig.Last(BridgeExecStatus.Completed), Is.Null);
            Assert.That(_rig.Surface.Log, Does.Not.Contain("queue Keyboard a"), "step 1 was never applied");
            Assert.That(_rig.Lease.IsActive, Is.False);
            Assert.That(_rig.Scheduler.Pending, Is.Zero, "no timer survives a failed run");
            Assert.That(_rig.Logs.Any(l => l.Contains("step 0 was not released")), Is.True);
        }

        [Test]
        public void AFailedApplyOfALaterStepStopsTheRemainingStepsAndReleases()
        {
            BridgeActionDefinition skill = InputRig.Skill(
                "three_steps",
                5,
                new BridgeActionStep(new[] { InputRig.Key("w") }, 0.1, 0.2),
                new BridgeActionStep(new[] { InputRig.Key("a") }, 0.1, 0),
                new BridgeActionStep(new[] { InputRig.Key("s") }, 0.1, 0));
            Assert.That(_rig.Lease.TryStart(InputRig.Request("s1"), skill, 1, out string error), Is.True, error);
            Assert.That(FireNear(0.1), Is.True, "step 0 releases and verifies");
            Assert.That(_rig.Surface.Keys, Is.Empty);

            // The device stops accepting input while the skill waits between steps.
            _rig.Surface.DropQueuedEvents = true;
            Assert.That(FireNear(0.2), Is.True);

            ExecutionRecordMessage failed = _rig.Last(BridgeExecStatus.Failed);
            Assert.That(failed, Is.Not.Null);
            Assert.That(_rig.Surface.Log, Does.Not.Contain("queue Keyboard s"), "step 2 was never reached");
            Assert.That(failed.AppliedInputs.Select(i => i.Control), Is.EqualTo(new[] { "w", "a" }));
            Assert.That(failed.AppliedInputs[1].InputStartWallS, Is.Null, "step 1 was never confirmed applied");
            Assert.That(_rig.Scheduler.Pending, Is.Zero);
            Assert.That(_rig.Lease.IsActive, Is.False);
        }

        [Test]
        public void Supersede_CancelsTheWholeSequenceAndItsTimersAndNeverReplaysThem()
        {
            BridgeActionDefinition skill = InputRig.Skill(
                "long",
                5,
                new BridgeActionStep(new[] { InputRig.Key("w") }, 0.5, 0),
                new BridgeActionStep(new[] { InputRig.Key("a") }, 0.5, 0));
            Assert.That(_rig.Lease.TryStart(InputRig.Request("first"), skill, 1, out string error), Is.True, error);
            IReadOnlyList<Action> staleCallbacks = _rig.Scheduler.AllCallbacks;
            Assert.That(staleCallbacks.Count, Is.EqualTo(2));
            _rig.Clock.Advance(0.1);

            Assert.That(
                _rig.Lease.TryStart(InputRig.Request("second"), InputRig.Action("press", 0.3, InputRig.Key("s")), 2, out error),
                Is.True,
                error);

            Assert.That(
                _rig.Snapshot().Select(r => r.ActionId + ":" + r.Status),
                Is.EqualTo(new[] { "first:running", "first:cancelled", "second:running" }),
                "the previous run is reported cancelled before the new one runs");
            ExecutionRecordMessage cancelled = _rig.Last(BridgeExecStatus.Cancelled);
            Assert.That(cancelled.ReleaseVerified, Is.True);
            Assert.That(cancelled.AppliedInputs.Single().AppliedDurationS, Is.EqualTo(0.1).Within(1e-9));
            Assert.That(_rig.Scheduler.Pending, Is.EqualTo(1), "only the new run's hold timer remains");

            int records = _rig.Snapshot().Count;
            List<string> log = _rig.Surface.Log.ToList();
            foreach (Action stale in staleCallbacks)
            {
                stale();
            }

            Assert.That(_rig.Snapshot().Count, Is.EqualTo(records), "a stale timer reports nothing");
            Assert.That(_rig.Surface.Log, Is.EqualTo(log), "a stale timer applies and releases nothing");
            Assert.That(_rig.Surface.Keys, Is.EqualTo(new[] { "s" }));
        }

        [Test]
        public void Deadline_CancelsARunningSkillAsTimedOutWithOnlyTheStepsItRan()
        {
            BridgeActionDefinition skill = InputRig.Skill(
                "slow",
                1.0,
                new BridgeActionStep(new[] { InputRig.Key("w") }, 0.2, 0.1),
                new BridgeActionStep(new[] { InputRig.Key("a") }, 0.2, 0));
            Assert.That(_rig.Lease.TryStart(InputRig.Request("s1"), skill, 1, out string error), Is.True, error);
            IReadOnlyList<Action> staleCallbacks = _rig.Scheduler.AllCallbacks;

            Assert.That(_rig.Scheduler.FireWhere(d => d.TotalSeconds > 0.8), Is.True, "the max_s deadline is scheduled from the first confirmed apply");

            ExecutionRecordMessage timedOut = _rig.Last(BridgeExecStatus.TimedOut);
            Assert.That(timedOut, Is.Not.Null);
            Assert.That(timedOut.ReleaseVerified, Is.True);
            Assert.That(timedOut.AppliedInputs.Select(i => i.Control), Is.EqualTo(new[] { "w" }), "step 1 never ran");
            Assert.That(_rig.Surface.Keys, Is.Empty);
            Assert.That(_rig.Scheduler.Pending, Is.Zero);

            int records = _rig.Snapshot().Count;
            foreach (Action stale in staleCallbacks)
            {
                stale();
            }

            Assert.That(_rig.Snapshot().Count, Is.EqualTo(records));
            Assert.That(_rig.Surface.Log, Does.Not.Contain("queue Keyboard a"));
        }

        [Test]
        public void Stop_DuringAWaitCancelsTheNextStepAndItsTimer()
        {
            BridgeActionDefinition skill = InputRig.Skill(
                "waiting",
                10,
                new BridgeActionStep(new[] { InputRig.Key("w") }, 0.1, 2),
                new BridgeActionStep(new[] { InputRig.Key("a") }, 0.1, 0));
            Assert.That(_rig.Lease.TryStart(InputRig.Request("s1"), skill, 1, out string error), Is.True, error);
            _rig.Clock.Advance(0.1);
            Assert.That(FireNear(0.1), Is.True);
            Assert.That(_rig.Surface.Keys, Is.Empty, "step 0 is released, the skill is waiting");
            IReadOnlyList<Action> staleCallbacks = _rig.Scheduler.AllCallbacks;
            _rig.Clock.Advance(0.75);

            Assert.That(_rig.Lease.Stop("user"), Is.True);

            ExecutionRecordMessage cancelled = _rig.Last(BridgeExecStatus.Cancelled);
            Assert.That(cancelled, Is.Not.Null);
            Assert.That(cancelled.AppliedInputs.Select(i => i.Control), Is.EqualTo(new[] { "w" }));
            Assert.That(cancelled.AppliedDurationS, Is.EqualTo(0.85).Within(1e-6), "the terminal total includes the elapsed cancelled wait");
            Assert.That(_rig.Scheduler.Pending, Is.Zero);
            foreach (Action stale in staleCallbacks)
            {
                stale();
            }

            Assert.That(_rig.Surface.Log, Does.Not.Contain("queue Keyboard a"), "the cancelled wait never applies step 1");
            Assert.That(_rig.Snapshot().Count(r => r.Status != BridgeExecStatus.Running), Is.EqualTo(1));
            Assert.That(_rig.Lease.Stop("again"), Is.False, "a stopped lease has nothing left to stop");
        }

        [Test]
        public void ATrailingWaitIsHonoredBeforeTheSkillCompletes()
        {
            BridgeActionDefinition skill = InputRig.Skill("settle", 5, new BridgeActionStep(new[] { InputRig.Key("w") }, 0.1, 0.5));
            Assert.That(_rig.Lease.TryStart(InputRig.Request("s1"), skill, 1, out string error), Is.True, error);

            _rig.Clock.Advance(0.1);
            Assert.That(FireNear(0.1), Is.True);
            Assert.That(_rig.Last(BridgeExecStatus.Completed), Is.Null, "the trailing wait has not elapsed");
            Assert.That(_rig.Surface.Keys, Is.Empty);

            _rig.Clock.Advance(0.5);
            Assert.That(FireNear(0.5), Is.True);

            ExecutionRecordMessage done = _rig.Last(BridgeExecStatus.Completed);
            Assert.That(done, Is.Not.Null);
            Assert.That(done.AppliedDurationS, Is.EqualTo(0.6).Within(1e-9), "the program total includes its trailing wait");
            Assert.That(done.InputEndWallS - done.InputStartWallS, Is.EqualTo(0.6).Within(1e-6));
            Assert.That(done.AppliedInputs.Single().AppliedDurationS, Is.EqualTo(0.1).Within(1e-9), "per-input timing still ends at verified release");
        }

        [Test]
        public void InitialApplyFailureAfterDeviceAdmissionEmitsOneFailedRecordWithReceiptsAndNeverRejects()
        {
            _rig.Surface.FailQueueDevice = "Gamepad";
            _rig.Surface.FailNeutralDevice = "Gamepad";
            BridgeActionDefinition action = InputRig.Action(
                "split",
                0.5,
                InputRig.Key("w"),
                new BridgeInputBinding("Gamepad", "leftStick", 0.5, 0.5));
            Assert.That(_rig.Lease.TryBegin(InputRig.Request("partial"), action, 8, out BridgeLeaseStart start, out string error), Is.True, error);

            Assert.That(start.Execute(out error), Is.True, "the accepted act is reported as an execution failure, not rejected");
            Assert.That(_rig.Snapshot().Select(record => record.Status), Is.EqualTo(new[] { BridgeExecStatus.Failed }));
            ExecutionRecordMessage failed = _rig.Last(BridgeExecStatus.Failed);
            Assert.That(failed.AppliedInputs.Select(input => input.Device), Is.EqualTo(new[] { "Keyboard", "Gamepad" }));
            Assert.That(failed.ReleaseVerified, Is.False, "the refused neutral is unknown/unverified");

            Assert.That(start.Execute(out error), Is.False, "a transport client cannot emit a second terminal result");
            Assert.That(_rig.Snapshot().Count(record => BridgeExecStatus.Terminal.Contains(record.Status)), Is.EqualTo(1));
            Assert.That(_rig.Snapshot().Any(record => record.Status == BridgeExecStatus.Rejected), Is.False);

            _rig.Surface.FailNeutralDevice = null;
            Assert.That(_rig.Executor.TryVerifyRelease(out error), Is.True, error);
        }

        [Test]
        public void AnAcceptedApplyRefusedBeforeItsOwnReceiptStillEmitsFailedExecution()
        {
            var prior = new BridgeInputRun("prior");
            Assert.That(_rig.Executor.TryApplyInputs(prior, 0, new[] { InputRig.Key("w") }, out _, out string error), Is.True, error);

            Assert.That(_rig.Lease.TryStart(InputRig.Request("next"), InputRig.Action("next", 0.5, InputRig.Key("s")), 9, out error), Is.True,
                "a post-validation backend failure is an execution result, not a rejected choice");

            ExecutionRecordMessage failed = _rig.Last(BridgeExecStatus.Failed);
            Assert.That(failed, Is.Not.Null);
            Assert.That(failed.AppliedInputs, Is.Empty, "the backend refused before admitting this run's input");
            Assert.That(_rig.Snapshot().Any(record => record.Status == BridgeExecStatus.Rejected), Is.False);
            Assert.That(_rig.Executor.TryReleaseInputs(prior, out bool verified, out error), Is.True, error);
            Assert.That(verified, Is.True);
        }

        [Test]
        public void Validation_PrecedesAnyStateChangeForAnInvalidLaterStep()
        {
            Assert.That(_rig.Lease.TryStart(InputRig.Request("held"), InputRig.Action("hold", 5, InputRig.Key("w")), 1, out string error), Is.True, error);
            _rig.Surface.InvalidKey = "bogus";
            List<string> log = _rig.Surface.Log.ToList();
            int records = _rig.Snapshot().Count;
            BridgeActionDefinition bad = InputRig.Skill(
                "bad",
                5,
                new BridgeActionStep(new[] { InputRig.Key("a") }, 0.1, 0),
                new BridgeActionStep(new[] { InputRig.Key("bogus") }, 0.1, 0));

            Assert.That(_rig.Lease.TryStart(InputRig.Request("bad"), bad, 2, out error), Is.False);

            Assert.That(error, Does.StartWith("step 1:"));
            Assert.That(_rig.Lease.ActiveActionId, Is.EqualTo("held"), "the running action was not disturbed");
            Assert.That(_rig.Surface.Log, Is.EqualTo(log), "nothing was queued, not even neutral");
            Assert.That(_rig.Surface.Keys, Is.EqualTo(new[] { "w" }));
            Assert.That(_rig.Snapshot().Count, Is.EqualTo(records), "no record was emitted for either action");
        }

        [Test]
        public void ActParams_AreRejectedBeforeDisturbingARunningLease()
        {
            Assert.That(_rig.Lease.TryStart(InputRig.Request("held"), InputRig.Action("hold", 5, InputRig.Key("w")), 1, out string error), Is.True, error);
            List<string> log = _rig.Surface.Log.ToList();
            var withParams = new ActRequestMessage
            {
                ActionId = "tweak",
                Params = new Dictionary<string, object> { { "speed", 2 } },
            };

            Assert.That(_rig.Lease.TryStart(withParams, InputRig.Action("hold2", 1, InputRig.Key("s")), 2, out error), Is.False);

            Assert.That(error, Does.Contain("params are not supported"));
            Assert.That(_rig.Lease.ActiveActionId, Is.EqualTo("held"));
            Assert.That(_rig.Surface.Log, Is.EqualTo(log));
            Assert.That(_rig.Last(BridgeExecStatus.Cancelled), Is.Null);
        }

        [Test]
        public void SkillLease_MustBeAbsentOrExactlyMaxAndIsNeverReinterpretedAsAStepHold()
        {
            BridgeActionDefinition skill = InputRig.Skill("bounded", 2.0, new BridgeActionStep(new[] { InputRig.Key("w") }, 0.3, 0));

            Assert.That(_rig.Lease.TryStart(InputRig.Request("a", 1.0), skill, 1, out string error), Is.False);
            Assert.That(error, Does.Contain("absent or exactly max_s"));
            Assert.That(_rig.Surface.Log, Is.Empty);
            Assert.That(_rig.Lease.IsActive, Is.False);

            Assert.That(_rig.Lease.TryStart(InputRig.Request("b", 2.0), skill, 1, out error), Is.True, error);
            Assert.That(
                _rig.Scheduler.PendingDelays.Any(d => Math.Abs(d.TotalSeconds - 0.3) < 0.05),
                Is.True,
                "the hold is the declared step hold, not lease_s");
            Assert.That(_rig.Scheduler.PendingDelays.Any(d => Math.Abs(d.TotalSeconds - 2.0) < 0.05), Is.True, "lease_s is the max_s bound");
            _rig.Lease.Stop("done");

            Assert.That(_rig.Lease.TryStart(InputRig.Request("c"), skill, 1, out error), Is.True, error);
        }

        [Test]
        public void SlowRunningRecordEmission_ConsumesTheFirstStepHoldInsteadOfExtendingIt()
        {
            using (var slow = new InputRig(operationTimeoutMs: 400, onRecord: record =>
            {
                if (record.Status == BridgeExecStatus.Running)
                {
                    Thread.Sleep(300);
                }
            }))
            {
                BridgeActionDefinition skill = InputRig.Skill("slow_record", 5, new BridgeActionStep(new[] { InputRig.Key("w") }, 0.5, 0));

                Assert.That(slow.Lease.TryStart(InputRig.Request("s1"), skill, 1, out string error), Is.True, error);

                Assert.That(
                    slow.Scheduler.PendingDelays.Any(d => d.TotalSeconds > 0.05 && d.TotalSeconds < 0.25),
                    Is.True,
                    "0.5 s minus the 0.3 s the record took, not another full 0.5 s");
            }
        }

        [Test]
        public void Dispose_CancelsTimersWithoutReportingAndLeavesNothingHeld()
        {
            BridgeActionDefinition skill = InputRig.Skill(
                "d",
                5,
                new BridgeActionStep(new[] { InputRig.Key("w") }, 0.5, 0),
                new BridgeActionStep(new[] { InputRig.Key("a") }, 0.5, 0));
            Assert.That(_rig.Lease.TryStart(InputRig.Request("s1"), skill, 1, out string error), Is.True, error);
            IReadOnlyList<Action> staleCallbacks = _rig.Scheduler.AllCallbacks;

            _rig.Lease.Dispose();

            Assert.That(_rig.Snapshot().Select(r => r.Status), Is.EqualTo(new[] { BridgeExecStatus.Running }), "a disposed lease reports nothing more");
            Assert.That(_rig.Scheduler.Pending, Is.Zero);
            foreach (Action stale in staleCallbacks)
            {
                stale();
            }

            Assert.That(_rig.Surface.Keys, Is.Empty);
            Assert.That(_rig.Surface.Log, Is.EqualTo(new[] { "queue Keyboard w", "neutral Keyboard" }));
            Assert.That(_rig.Lease.TryStart(InputRig.Request("late"), skill, 1, out error), Is.False);
        }

        [Test]
        public void MainThreadStopAndVerify_DoNotDeadlockAgainstATimerThreadWaitingOnTheMainThread()
        {
            using (var rig = new InputRig(operationTimeoutMs: 5000, realScheduler: true))
            {
                Assert.That(rig.Lease.TryStart(InputRig.Request("a1"), InputRig.Action("short", 0.05, InputRig.Key("w")), 1, out string error), Is.True, error);
                rig.Loop.Paused = true;
                Thread.Sleep(400);
                Assert.That(rig.Last(BridgeExecStatus.Completed), Is.Null, "the timer thread is blocked waiting for the stalled main thread");

                var stopwatch = Stopwatch.StartNew();
                bool stopped = rig.Loop.Invoke(() => rig.Lease.Stop("scene_change"));
                bool verified = rig.Loop.Invoke(() => rig.Executor.TryVerifyRelease(out _));

                Assert.That(stopwatch.ElapsedMilliseconds, Is.LessThan(2000), "the main thread did not wait for the timer thread");
                Assert.That(stopped, Is.False, "the run was already being ended by the expiry");
                Assert.That(verified, Is.True, "the main thread ran the queued release itself and read the device");
                Assert.That(rig.Surface.Keys, Is.Empty);

                rig.Loop.Paused = false;
                Assert.That(rig.WaitFor(() => rig.Snapshot().Count(r => r.Status != BridgeExecStatus.Running) == 1), Is.True, "exactly one terminal record arrives");
                Thread.Sleep(100);
                ExecutionRecordMessage terminal = rig.Snapshot().Single(r => r.Status != BridgeExecStatus.Running);
                Assert.That(terminal.Status, Is.EqualTo(BridgeExecStatus.Completed));
                Assert.That(terminal.ReleaseVerified, Is.True);
                Assert.That(rig.Executor.OutstandingInputs, Is.Zero);
            }
        }

        [Test]
        public void ANewActArrivingWhileThePreviousHoldIsBeingReleasedWaitsForItInsteadOfBeingRefused()
        {
            using (var rig = new InputRig(operationTimeoutMs: 5000))
            {
                Assert.That(rig.Lease.TryStart(InputRig.Request("a"), InputRig.Action("first", 0.2, InputRig.Key("w")), 1, out string error), Is.True, error);
                rig.Loop.Paused = true;
                Task expiry = Task.Run(() => rig.Scheduler.FireWhere(d => d.TotalSeconds < 0.25));
                Thread.Sleep(150);

                Task<(bool Started, string Error)> next = Task.Run(() =>
                {
                    bool ok = rig.Lease.TryStart(InputRig.Request("b"), InputRig.Action("second", 5, InputRig.Key("s")), 2, out string e);
                    return (ok, e);
                });
                Thread.Sleep(150);
                Assert.That(next.IsCompleted, Is.False, "the new act waits for the previous release to be confirmed");

                rig.Loop.Paused = false;
                Assert.That(next.Wait(5000), Is.True);
                Assert.That(next.Result.Started, Is.True, next.Result.Error);
                Assert.That(expiry.Wait(5000), Is.True);
                Assert.That(
                    rig.Snapshot().Select(r => r.ActionId + ":" + r.Status),
                    Is.EqualTo(new[] { "a:running", "a:completed", "b:running" }));
                Assert.That(rig.Surface.Log, Is.EqualTo(new[] { "queue Keyboard w", "neutral Keyboard", "queue Keyboard s" }));
                Assert.That(rig.Surface.Keys, Is.EqualTo(new[] { "s" }));
            }
        }

        [Test]
        public void StopFromTheMainThread_TakesAStartThatIsStillWaitingForTheMainThread()
        {
            using (var rig = new InputRig(operationTimeoutMs: 5000))
            {
                rig.Loop.Paused = true;
                Task<(bool Started, string Error)> worker = Task.Run(() =>
                {
                    bool ok = rig.Lease.TryStart(InputRig.Request("a1"), InputRig.Action("press", 5, InputRig.Key("w")), 1, out string error);
                    return (ok, error);
                });
                Assert.That(rig.WaitFor(() => rig.Lease.IsActive), Is.True);
                Thread.Sleep(100);
                Assert.That(worker.IsCompleted, Is.False, "the start is waiting for the main thread");

                var stopwatch = Stopwatch.StartNew();
                bool stopped = rig.Loop.Invoke(() => rig.Lease.Stop("reset"));

                Assert.That(stopped, Is.True);
                Assert.That(stopwatch.ElapsedMilliseconds, Is.LessThan(2000));
                rig.Loop.Paused = false;
                Assert.That(worker.Wait(5000), Is.True);
                Assert.That(worker.Result.Started, Is.True, "the act was accepted and the stop reports it");
                Assert.That(rig.Snapshot().Select(r => r.Status), Is.EqualTo(new[] { BridgeExecStatus.Cancelled }));
                ExecutionRecordMessage cancelled = rig.Snapshot().Single();
                Assert.That(cancelled.ReleaseVerified, Is.Null, "nothing was ever applied, so there is nothing to verify");
                Assert.That(cancelled.InputStartWallS, Is.Null);
                Assert.That(rig.Surface.Log, Is.Empty, "the cancelled start never reached the device");
                Assert.That(rig.Surface.Keys, Is.Empty);
            }
        }

        private bool FireNear(double seconds)
        {
            return _rig.Scheduler.FireWhere(d => Math.Abs(d.TotalSeconds - seconds) < 0.05);
        }

        private static double Unix(DateTime value)
        {
            return (value - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
        }
    }
}
