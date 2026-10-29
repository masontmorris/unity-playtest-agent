using System;
using System.Diagnostics;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using NUnit.Framework;
using Playtest.Bridge.Actions;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The device-agnostic executor against a simulated keyboard, mouse and gamepad: inputs apply
    /// together, an apply is acknowledged only after a device read shows it, a release is verified
    /// only from a device read, and a main-thread caller never waits on itself.
    /// </summary>
    [TestFixture]
    public sealed class BridgeInputExecutorTests
    {
        private InputRig _rig;

        [SetUp]
        public void SetUp()
        {
            _rig = new InputRig(operationTimeoutMs: 1500);
        }

        [TearDown]
        public void TearDown()
        {
            _rig.Dispose();
        }

        [Test]
        public void TwoKeys_ApplyAsOneQueuedStateAndBothReadBackBeforeTheAcknowledgement()
        {
            var run = new BridgeInputRun("chord");

            Assert.That(
                _rig.Executor.TryApplyInputs(run, 0, new[] { InputRig.Key("w"), InputRig.Key("a") }, out BridgeInputAck ack, out string error),
                Is.True,
                error);

            Assert.That(_rig.Surface.Log, Is.EqualTo(new[] { "queue Keyboard w+a" }), "both keys are one device state, never two events");
            Assert.That(_rig.Surface.Keys, Is.EqualTo(new[] { "a", "w" }));
            Assert.That(ack.AppliedUtc, Is.EqualTo(_rig.Clock.Now));
            Assert.That(run.Snapshot().Select(InputRig.Describe), Is.EqualTo(new[] { "0:Keyboard.w", "0:Keyboard.a" }));
            Assert.That(run.Snapshot().All(r => r.StartUtc == _rig.Clock.Now && r.EndUtc == null && r.ReleaseVerified == null), Is.True);
            Assert.That(_rig.Surface.ReadsOutsideUpdate, Is.Zero, "controls are only read inside an input update");
        }

        [Test]
        public void Apply_IsNotAcknowledgedUntilTheDeviceShowsTheInput()
        {
            using (var rig = new InputRig(operationTimeoutMs: 300))
            {
                rig.Surface.DropQueuedEvents = true;
                var run = new BridgeInputRun("dropped");

                bool applied = rig.Executor.TryApplyInputs(run, 0, new[] { InputRig.Key("w") }, out BridgeInputAck ack, out string error);

                Assert.That(applied, Is.False);
                Assert.That(ack, Is.Null);
                Assert.That(error, Does.Contain("reads as not pressed").And.Contain("not confirmed applied"));
                BridgeInputReceipt receipt = run.Snapshot().Single();
                Assert.That(receipt.StartUtc, Is.Null, "an unconfirmed input has no start");

                Assert.That(rig.Executor.TryReleaseInputs(run, out bool verified, out error), Is.True, error);
                Assert.That(verified, Is.True, "the device really is neutral");
                Assert.That(run.Snapshot().Single().ReleaseVerified, Is.True);
                Assert.That(run.Snapshot().Single().AppliedDurationS, Is.Null, "no confirmed start, so no duration");
            }
        }

        [Test]
        public void Release_MeasuresDurationBetweenTheTwoDeviceConfirmationsNotAHold()
        {
            var run = new BridgeInputRun("held");
            DateTime start = _rig.Clock.Now;
            Assert.That(_rig.Executor.TryApplyInputs(run, 0, new[] { InputRig.Key("w"), InputRig.Key("a") }, out _, out string error), Is.True, error);

            _rig.Clock.Advance(0.25);
            Assert.That(_rig.Executor.TryReleaseInputs(run, out bool verified, out error), Is.True, error);

            Assert.That(verified, Is.True);
            Assert.That(_rig.Surface.Keys, Is.Empty);
            foreach (BridgeInputReceipt receipt in run.Snapshot())
            {
                Assert.That(receipt.StartUtc, Is.EqualTo(start));
                Assert.That(receipt.EndUtc, Is.EqualTo(start.AddSeconds(0.25)));
                Assert.That(receipt.AppliedDurationS, Is.EqualTo(0.25).Within(1e-9));
                Assert.That(receipt.ReleaseVerified, Is.True);
            }

            Assert.That(_rig.Executor.OutstandingInputs, Is.Zero);
        }

        [Test]
        public void Release_ReportsAStuckInputAsUnverifiedAndLaterVerificationCompletesItsReceipt()
        {
            using (var rig = new InputRig(operationTimeoutMs: 300))
            {
                rig.Surface.IgnoreNeutral = true;
                var run = new BridgeInputRun("stuck");
                Assert.That(rig.Executor.TryApplyInputs(run, 0, new[] { InputRig.Key("w"), InputRig.Key("a") }, out _, out string error), Is.True, error);

                bool released = rig.Executor.TryReleaseInputs(run, out bool verified, out error);

                Assert.That(released, Is.True, "the release was issued");
                Assert.That(verified, Is.False, "but the device still shows the keys down");
                Assert.That(error, Does.Contain("still reads as pressed"));
                Assert.That(run.Snapshot().All(r => r.ReleaseVerified == false && r.EndUtc == null && r.AppliedDurationS == null), Is.True);
                Assert.That(rig.Executor.OutstandingInputs, Is.EqualTo(2));
                Assert.That(
                    rig.Executor.TryApplyInputs(new BridgeInputRun("next"), 0, new[] { InputRig.Key("s") }, out _, out error),
                    Is.False,
                    "nothing is applied over an input that is not confirmed released");
                Assert.That(error, Does.Contain("still held or not yet confirmed released"));

                rig.Surface.ForceNeutral();
                rig.Clock.Advance(0.5);
                Assert.That(rig.Executor.TryVerifyRelease(out error), Is.True, error);
                Assert.That(rig.Executor.OutstandingInputs, Is.Zero);
                Assert.That(
                    run.Snapshot().All(r => r.ReleaseVerified == true && r.EndUtc.HasValue && r.AppliedDurationS == 0.5),
                    Is.True,
                    "the late verification completes the receipt from a device read");
            }
        }

        [Test]
        public void MouseImpulse_ReportsTheDeltaTheDeviceReadAndNoDuration()
        {
            var run = new BridgeInputRun("look");
            var delta = new BridgeInputBinding("Mouse", "delta", 0.5, -0.25);
            var button = new BridgeInputBinding("Mouse", "rightButton");

            Assert.That(_rig.Executor.TryApplyInputs(run, 0, new[] { delta, button }, out _, out string error), Is.True, error);

            Assert.That(_rig.Surface.Log, Is.EqualTo(new[] { "queue Mouse delta+rightButton" }));
            BridgeInputReceipt impulse = run.Snapshot()[0];
            Assert.That(impulse.AppliedDeltaX, Is.EqualTo(32).Within(1e-9), "0.5 x 64 pixels");
            Assert.That(impulse.AppliedDeltaY, Is.EqualTo(-16).Within(1e-9), "-0.25 x 64 pixels");
            Assert.That(impulse.X, Is.EqualTo(0.5), "the receipt keeps the declared value");
            Assert.That(run.Snapshot()[1].AppliedDeltaX, Is.Null, "a button has no applied delta");

            _rig.Clock.Advance(0.1);
            Assert.That(_rig.Executor.TryReleaseInputs(run, out bool verified, out error), Is.True, error);

            Assert.That(verified, Is.True);
            impulse = run.Snapshot()[0];
            Assert.That(impulse.AppliedDurationS, Is.Null, "an impulse has no measured duration");
            Assert.That(impulse.ReleaseVerified, Is.True, "the delta read back as neutral");
            Assert.That(impulse.EndUtc, Is.Not.Null);
            Assert.That(run.Snapshot()[1].AppliedDurationS, Is.EqualTo(0.1).Within(1e-9));
            Assert.That(_rig.Surface.MouseButtons, Is.Empty);
        }

        [Test]
        public void MouseImpulse_IsAppliedOnceNotRepeatedEveryUpdate()
        {
            var run = new BridgeInputRun("look");
            Assert.That(_rig.Executor.TryApplyInputs(run, 0, new[] { new BridgeInputBinding("Mouse", "delta", 1, 1) }, out _, out string error), Is.True, error);

            long updates = _rig.Surface.UpdateCount;
            Assert.That(_rig.WaitFor(() => _rig.Surface.UpdateCount > updates + 5), Is.True);

            Assert.That(_rig.Surface.Log, Is.EqualTo(new[] { "queue Mouse delta" }), "one queued impulse, however long it is held");
            Assert.That(_rig.Surface.MouseDelta, Is.EqualTo(new[] { 0.0, 0.0 }), "the delta reset on the next update");
        }

        [Test]
        public void MouseImpulse_ThatTheDeviceDoesNotReproduceIsNotAcknowledgedButIsReported()
        {
            using (var rig = new InputRig(operationTimeoutMs: 300))
            {
                rig.Surface.MouseDeltaScale = 2;
                var run = new BridgeInputRun("look");

                bool applied = rig.Executor.TryApplyInputs(run, 0, new[] { new BridgeInputBinding("Mouse", "delta", 0.5, -0.25) }, out _, out string error);

                Assert.That(applied, Is.False);
                Assert.That(error, Does.Contain("read a delta of (64, -32) pixels, not the declared (32, -16)"));
                BridgeInputReceipt receipt = run.Snapshot().Single();
                Assert.That(receipt.AppliedDeltaX, Is.EqualTo(64), "the actual delta is reported even though it is wrong");
                Assert.That(receipt.AppliedDeltaY, Is.EqualTo(-32));
                Assert.That(receipt.StartUtc, Is.Null);
            }
        }

        [Test]
        public void Gamepad_ValuesReachTheDeviceUnchangedAndReleaseToNeutral()
        {
            var run = new BridgeInputRun("pad");
            var stick = new BridgeInputBinding("Gamepad", "leftStick", 0.3, -0.7);
            var trigger = new BridgeInputBinding("Gamepad", "rightTrigger", 0.55, 0);
            var button = new BridgeInputBinding("Gamepad", "buttonSouth");

            Assert.That(_rig.Executor.TryApplyInputs(run, 0, new[] { stick, trigger, button }, out _, out string error), Is.True, error);

            Assert.That(_rig.Surface.Log, Is.EqualTo(new[] { "queue Gamepad leftStick+rightTrigger+buttonSouth" }), "one gamepad state");
            Assert.That(_rig.Surface.GamepadValue("leftStick"), Is.EqualTo(new[] { 0.3, -0.7 }), "no normalization, dead zone or clamp");
            Assert.That(_rig.Surface.GamepadValue("rightTrigger"), Is.EqualTo(new[] { 0.55, 0.0 }));
            Assert.That(_rig.Surface.GamepadButtons, Is.EqualTo(new[] { "buttonSouth" }));

            _rig.Clock.Advance(0.4);
            Assert.That(_rig.Executor.TryReleaseInputs(run, out bool verified, out error), Is.True, error);

            Assert.That(verified, Is.True);
            Assert.That(_rig.Surface.GamepadValue("leftStick"), Is.EqualTo(new[] { 0.0, 0.0 }));
            Assert.That(_rig.Surface.GamepadValue("rightTrigger"), Is.EqualTo(new[] { 0.0, 0.0 }));
            Assert.That(_rig.Surface.GamepadButtons, Is.Empty);
            Assert.That(run.Snapshot().All(r => r.ReleaseVerified == true), Is.True);
            foreach (BridgeInputReceipt receipt in run.Snapshot())
                Assert.That(receipt.AppliedDurationS, Is.EqualTo(0.4).Within(1e-6), "measured fake-clock hold");
        }

        [Test]
        public void Gamepad_DiagonalStickAndDpadAreNotNormalized()
        {
            var run = new BridgeInputRun("pad");
            var stick = new BridgeInputBinding("Gamepad", "rightStick", 1, 1);
            var dpad = new BridgeInputBinding("Gamepad", "dpad", 1, -1);

            Assert.That(_rig.Executor.TryApplyInputs(run, 0, new[] { stick, dpad }, out _, out string error), Is.True, error);

            Assert.That(_rig.Surface.GamepadValue("rightStick"), Is.EqualTo(new[] { 1.0, 1.0 }));
            Assert.That(_rig.Surface.GamepadButtons, Is.EqualTo(new[] { "dpadDown", "dpadRight" }));
            Assert.That(_rig.Executor.TryReleaseInputs(run, out bool verified, out error), Is.True, error);
            Assert.That(verified, Is.True);
        }

        [Test]
        public void ARefusedDeviceKeepsEveryAttemptedInputOutstandingUntilNeutralIsVerified()
        {
            _rig.Surface.FailQueueDevice = "Gamepad";
            _rig.Surface.FailNeutralDevice = "Gamepad";
            var run = new BridgeInputRun("split");

            bool applied = _rig.Executor.TryApplyInputs(
                run,
                0,
                new[] { InputRig.Key("w"), new BridgeInputBinding("Gamepad", "leftStick", 0.5, 0.5) },
                out BridgeInputAck ack,
                out string error);

            Assert.That(applied, Is.False);
            Assert.That(ack, Is.Null);
            Assert.That(error, Does.Contain("refused"));
            Assert.That(_rig.Surface.Log, Is.EqualTo(new[] { "queue Keyboard w", "neutral Keyboard", "neutral Gamepad" }));
            Assert.That(_rig.Executor.TryReleaseInputs(run, out bool verified, out error), Is.False, "failed cleanup remains unverified");
            Assert.That(verified, Is.False);
            Assert.That(_rig.Executor.OutstandingInputs, Is.GreaterThan(0));
            Assert.That(_rig.Executor.TryApplyInputs(new BridgeInputRun("blocked"), 0, new[] { new BridgeInputBinding("Gamepad", "leftStick", 0, 1) }, out _, out error), Is.False);
            Assert.That(error, Does.Contain("still held or not yet confirmed released"));

            _rig.Surface.FailNeutralDevice = null;
            Assert.That(_rig.Executor.TryVerifyRelease(out error), Is.True, error);
            Assert.That(verified = run.Snapshot().All(r => r.ReleaseVerified == true), Is.True, "a later device read confirms neutral");
            Assert.That(_rig.WaitFor(() => run.Snapshot().All(r => r.ReleaseVerified == true)), Is.True, "the failed apply is only settled after a device read");
            Assert.That(run.Snapshot().Select(r => r.Device), Is.EqualTo(new[] { "Keyboard", "Gamepad" }));
            Assert.That(_rig.Executor.OutstandingInputs, Is.Zero);
            Assert.That(_rig.Executor.TryApplyInputs(new BridgeInputRun("next"), 0, new[] { InputRig.Key("s") }, out _, out error), Is.True, error);
        }

        [Test]
        public void ACancelledRunCanNoLongerApplyInput()
        {
            var run = new BridgeInputRun("late");
            run.Cancel();

            Assert.That(_rig.Executor.TryApplyInputs(run, 0, new[] { InputRig.Key("w") }, out _, out string error), Is.False);

            Assert.That(error, Does.Contain("cancelled"));
            Assert.That(_rig.Surface.Log, Is.Empty, "nothing reached the device");
        }

        [Test]
        public void InputsOfANewStepAreRefusedWhileEarlierOnesAreStillHeld()
        {
            var run = new BridgeInputRun("overlap");
            Assert.That(_rig.Executor.TryApplyInputs(run, 0, new[] { InputRig.Key("w") }, out _, out string error), Is.True, error);

            Assert.That(_rig.Executor.TryApplyInputs(run, 1, new[] { InputRig.Key("a") }, out _, out error), Is.False);

            Assert.That(error, Does.Contain("still held"));
            Assert.That(_rig.Surface.Log, Is.EqualTo(new[] { "queue Keyboard w" }));
            Assert.That(_rig.Surface.Keys, Is.EqualTo(new[] { "w" }));
        }

        [Test]
        public void Validation_RejectsBeforeAnythingIsQueued()
        {
            _rig.Surface.InvalidKey = "bogus";
            BridgeActionDefinition unknownKey = InputRig.Action("bad_key", 1, InputRig.Key("w"), InputRig.Key("bogus"));
            BridgeActionDefinition childPath = InputRig.Action("child", 1, new BridgeInputBinding("Gamepad", "dpad/up"));
            BridgeActionDefinition laterStep = InputRig.Skill(
                "later",
                5,
                new BridgeActionStep(new[] { InputRig.Key("w") }, 0.1),
                new BridgeActionStep(new[] { InputRig.Key("bogus") }, 0.1));

            Assert.That(_rig.Executor.TryValidate(unknownKey, out string error), Is.False);
            Assert.That(error, Does.Contain("unknown key 'bogus'"));
            Assert.That(_rig.Executor.TryValidate(childPath, out error), Is.False);
            Assert.That(error, Does.Contain("is a path"));
            Assert.That(_rig.Executor.TryValidate(laterStep, out error), Is.False);
            Assert.That(error, Does.StartWith("step 1:"), "a bad later step is found before the first step runs");

            Assert.That(
                _rig.Executor.TryApplyInputs(new BridgeInputRun("x"), 0, new[] { InputRig.Key("w"), InputRig.Key("bogus") }, out _, out error),
                Is.False);
            Assert.That(_rig.Surface.Log, Is.Empty);
            Assert.That(_rig.Executor.TryValidate(InputRig.Action("fine", 1, InputRig.Key("w")), out error), Is.True, error);
        }

        [Test]
        public void ReleaseIsScopedToTheRunThatAppliedTheInput()
        {
            var first = new BridgeInputRun("first");
            Assert.That(_rig.Executor.TryApplyInputs(first, 0, new[] { InputRig.Key("w") }, out _, out string error), Is.True, error);

            Assert.That(_rig.Executor.TryReleaseInputs(new BridgeInputRun("other"), out bool verified, out error), Is.True, error);

            Assert.That(verified, Is.True, "a run that applied nothing has nothing to release");
            Assert.That(_rig.Surface.Keys, Is.EqualTo(new[] { "w" }), "another run's release does not end this run's hold");
            Assert.That(_rig.Executor.TryReleaseInputs(first, out verified, out error), Is.True, error);
            Assert.That(_rig.Surface.Keys, Is.Empty);
        }

        [Test]
        public void ReleasedInputs_NameTheInputsUntilTheReleaseIsVerifiedAgain()
        {
            var run = new BridgeInputRun("names");
            Assert.That(
                _rig.Executor.TryApplyInputs(run, 0, new[] { InputRig.Key("W"), new BridgeInputBinding("Gamepad", "leftStick", 0, 1) }, out _, out string error),
                Is.True,
                error);
            Assert.That(_rig.Executor.ReleasedInputs, Is.Empty);

            Assert.That(_rig.Executor.TryReleaseInputs(run, out _, out error), Is.True, error);

            Assert.That(_rig.Executor.ReleasedInputs, Is.EquivalentTo(new[] { "key:w", "gamepad:leftstick" }));
            Assert.That(_rig.Executor.TryVerifyRelease(out error), Is.True, error);
            Assert.That(_rig.Executor.ReleasedInputs, Is.Empty);
        }

        [Test]
        public void MainThreadCaller_RunsInlineWithAManualUpdateAndDoesNotWaitOnItself()
        {
            _rig.Loop.AutoTick = false;
            var run = new BridgeInputRun("inline");

            bool applied = false;
            bool released = false;
            bool verified = false;
            string error = null;
            var stopwatch = Stopwatch.StartNew();
            _rig.Loop.Invoke(() =>
            {
                applied = _rig.Executor.TryApplyInputs(run, 0, new[] { InputRig.Key("w") }, out _, out error);
                released = applied && _rig.Executor.TryReleaseInputs(run, out verified, out error);
            });

            Assert.That(applied, Is.True, error);
            Assert.That(released && verified, Is.True, error);
            Assert.That(stopwatch.ElapsedMilliseconds, Is.LessThan(1000), "no waiting for a pump that only the blocked thread could run");
            Assert.That(_rig.Surface.Flushes, Is.GreaterThanOrEqualTo(2), "each inline call forced one input update");
            Assert.That(_rig.Surface.Keys, Is.Empty);
        }

        [Test]
        public void MainThreadStop_DoesNotWaitForAWorkerThatIsWaitingOnTheMainThread()
        {
            using (var rig = new InputRig(operationTimeoutMs: 5000))
            {
                rig.Loop.Paused = true;
                var run = new BridgeInputRun("contended");
                Task<(bool Applied, string Error)> worker = Task.Run(() =>
                {
                    bool ok = rig.Executor.TryApplyInputs(run, 0, new[] { InputRig.Key("w") }, out _, out string error);
                    return (ok, error);
                });
                Thread.Sleep(100);
                Assert.That(worker.IsCompleted, Is.False, "the worker is waiting for the stalled main thread");

                var stopwatch = Stopwatch.StartNew();
                bool verified = rig.Loop.Invoke(() =>
                {
                    run.Cancel();
                    rig.Executor.TryReleaseInputs(run, out bool ok, out _);
                    return ok;
                });

                Assert.That(stopwatch.ElapsedMilliseconds, Is.LessThan(2000), "the main thread stopped without waiting for the worker");
                Assert.That(verified, Is.True);

                rig.Loop.Paused = false;
                Assert.That(worker.Wait(5000), Is.True, "the worker returns once the main thread runs again");
                Assert.That(worker.Result.Applied, Is.False, "the stale apply does not run after the stop");
                Assert.That(rig.Surface.Log, Is.Empty, "no input ever reached the device");
                Assert.That(rig.Surface.Keys, Is.Empty);
            }
        }

        [Test]
        public void AnApplyThatTimesOutWaitingForTheMainThreadNeverRunsLater()
        {
            using (var rig = new InputRig(operationTimeoutMs: 250))
            {
                rig.Loop.Paused = true;
                var run = new BridgeInputRun("late");

                bool applied = rig.Executor.TryApplyInputs(run, 0, new[] { InputRig.Key("w") }, out _, out string error);

                Assert.That(applied, Is.False);
                Assert.That(error, Does.Contain("timed out"));
                rig.Loop.Paused = false;
                Thread.Sleep(150);
                Assert.That(rig.Surface.Log, Is.Empty, "the abandoned apply was skipped, not applied late");
                Assert.That(rig.Surface.Keys, Is.Empty);
                Assert.That(rig.Executor.TryReleaseInputs(run, out bool verified, out error), Is.True, error);
                Assert.That(verified, Is.True);
            }
        }

        [Test]
        public void ReleasingAnIdleRunNeedsNoMainThread()
        {
            _rig.Loop.Paused = true;
            var stopwatch = Stopwatch.StartNew();

            bool released = _rig.Executor.TryReleaseInputs(new BridgeInputRun("idle"), out bool verified, out string error);

            Assert.That(released && verified, Is.True, error);
            Assert.That(stopwatch.ElapsedMilliseconds, Is.LessThan(500));
        }

        [Test]
        public void ReadsOnlyHappenInsideInputUpdatesAcrossAFullApplyAndRelease()
        {
            var run = new BridgeInputRun("reads");
            Assert.That(
                _rig.Executor.TryApplyInputs(run, 0, new[] { InputRig.Key("w"), new BridgeInputBinding("Gamepad", "leftStick", 0.5, 0.5) }, out _, out string error),
                Is.True,
                error);
            Assert.That(_rig.Executor.TryReleaseInputs(run, out _, out error), Is.True, error);
            Assert.That(_rig.Executor.TryVerifyRelease(out error), Is.True, error);

            Assert.That(_rig.Surface.ReadsOutsideUpdate, Is.Zero);
        }
    }
}
