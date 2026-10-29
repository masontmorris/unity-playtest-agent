// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Unity;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The lease driving the real Input System: a keyboard chord, gamepad values, a mouse delta
    /// impulse and a two-step skill. Each test starts the action off the main thread, the way the
    /// transport does, while this coroutine yields every frame so the device can update. The
    /// assertions read the device itself and the per-input receipts the lease reported; they
    /// choose nothing and only execute the declared inputs. Needs a Play mode run.
    /// </summary>
    public sealed class InputDevicesPlayModeTests
    {
        private readonly List<ExecutionRecordMessage> _records = new List<ExecutionRecordMessage>();
        private BridgeActionLease _lease;
        private bool _started;
        private string _startError;

        [SetUp]
        public void SetUp()
        {
            _records.Clear();
            _lease = new BridgeActionLease(
                new InputSystemInputBackend(),
                record =>
                {
                    lock (_records)
                    {
                        _records.Add(record);
                    }
                });
        }

        [TearDown]
        public void TearDown()
        {
            // Disposing the lease releases anything held and disposes the backend, which removes
            // any device it created.
            _lease.Dispose();
        }

        [UnityTest]
        public IEnumerator KeyboardChord_IsDownTogetherThenReleasedWithMeasuredReceipts()
        {
            var action = Action(
                "chord",
                0.4,
                new BridgeInputBinding("Keyboard", "w"),
                new BridgeInputBinding("Keyboard", "a"));

            yield return Start(action, "chord-1");

            Keyboard keyboard = Keyboard.current;
            Assert.That(keyboard, Is.Not.Null);
            Assert.That(keyboard[Key.W].isPressed && keyboard[Key.A].isPressed, Is.True, "both keys of the chord are down together");

            yield return WaitForTerminal("chord-1");

            ExecutionRecordMessage done = Terminal("chord-1");
            Assert.That(done.Status, Is.EqualTo(BridgeExecStatus.Completed), Describe(done));
            Assert.That(done.ReleaseVerified, Is.True);
            Assert.That(done.AppliedInputs.Select(i => i.Control), Is.EqualTo(new[] { "w", "a" }));
            foreach (AppliedInputReceipt receipt in done.AppliedInputs)
            {
                Assert.That(receipt.ReleaseVerified, Is.True);
                Assert.That(receipt.AppliedDurationS, Is.GreaterThan(0.3), "measured from device reads, near the 0.4 s hold");
                Assert.That(receipt.InputEndWallS, Is.GreaterThan(receipt.InputStartWallS.Value));
            }

            Assert.That(keyboard[Key.W].isPressed || keyboard[Key.A].isPressed, Is.False, "both keys are up after the terminal record");
        }

        [UnityTest]
        public IEnumerator Gamepad_ValuesReachTheDeviceUnchangedAndReleaseToNeutral()
        {
            var action = Action(
                "pad",
                0.5,
                new BridgeInputBinding("Gamepad", "leftStick", 0.3, -0.7),
                new BridgeInputBinding("Gamepad", "rightTrigger", 0.55, 0),
                new BridgeInputBinding("Gamepad", "buttonSouth"));

            yield return Start(action, "pad-1");

            Gamepad pad = Gamepad.current;
            Assert.That(pad, Is.Not.Null, "an action that declares a gamepad control gets a gamepad device");
            Vector2 stick = pad.leftStick.ReadUnprocessedValue();
            Assert.That(stick.x, Is.EqualTo(0.3f).Within(1e-4f), "no dead zone, normalization or clamp");
            Assert.That(stick.y, Is.EqualTo(-0.7f).Within(1e-4f));
            Assert.That(pad.rightTrigger.ReadUnprocessedValue(), Is.EqualTo(0.55f).Within(1e-4f));
            Assert.That(pad.buttonSouth.isPressed, Is.True);

            yield return WaitForTerminal("pad-1");

            ExecutionRecordMessage done = Terminal("pad-1");
            Assert.That(done.Status, Is.EqualTo(BridgeExecStatus.Completed), Describe(done));
            Assert.That(done.AppliedInputs.Select(i => i.Control), Is.EqualTo(new[] { "leftStick", "rightTrigger", "buttonSouth" }));
            Assert.That(done.AppliedInputs.All(i => i.Device == "Gamepad" && i.ReleaseVerified == true), Is.True);
            Assert.That(done.AppliedInputs[0].X, Is.EqualTo(0.3), "the receipt keeps the declared value");
            Assert.That(pad.leftStick.ReadUnprocessedValue(), Is.EqualTo(Vector2.zero));
            Assert.That(pad.rightTrigger.ReadUnprocessedValue(), Is.EqualTo(0f));
            Assert.That(pad.buttonSouth.isPressed, Is.False);
        }

        [UnityTest]
        public IEnumerator MouseDelta_IsOneImpulseAndTheReceiptCarriesTheDeltaTheDeviceRead()
        {
            var action = Action("look", 0.2, new BridgeInputBinding("Mouse", "delta", 0.5, -0.25));

            yield return Start(action, "look-1");
            yield return WaitForTerminal("look-1");

            ExecutionRecordMessage done = Terminal("look-1");
            Assert.That(done.Status, Is.EqualTo(BridgeExecStatus.Completed), Describe(done));
            AppliedInputReceipt receipt = done.AppliedInputs.Single();
            Assert.That(receipt.AppliedDeltaX, Is.EqualTo(32).Within(0.01), "0.5 x 64 pixels");
            Assert.That(receipt.AppliedDeltaY, Is.EqualTo(-16).Within(0.01), "-0.25 x 64 pixels");
            Assert.That(receipt.AppliedDurationS, Is.Null, "an impulse has no measured duration");
            Assert.That(receipt.ReleaseVerified, Is.True, "the delta read back as zero");
            Assert.That(Mouse.current.delta.ReadValue(), Is.EqualTo(Vector2.zero));
        }

        [UnityTest]
        public IEnumerator Skill_AppliesItsStepsInOrderWithAWaitBetweenThem()
        {
            BridgeActionDefinition skill = new BridgeActionDefinition { Name = "two_step", MaxS = 3 };
            skill.Steps.Add(new BridgeActionStep(new[] { new BridgeInputBinding("Keyboard", "w") }, 0.15, 0.1));
            skill.Steps.Add(new BridgeActionStep(new[] { new BridgeInputBinding("Gamepad", "buttonSouth") }, 0.15));

            yield return Start(skill, "skill-1");
            yield return WaitForTerminal("skill-1");

            ExecutionRecordMessage done = Terminal("skill-1");
            Assert.That(done.Status, Is.EqualTo(BridgeExecStatus.Completed), Describe(done));
            Assert.That(done.AppliedInputs.Select(i => i.StepIndex + ":" + i.Control), Is.EqualTo(new[] { "0:w", "1:buttonSouth" }));
            Assert.That(done.AppliedInputs.All(i => i.ReleaseVerified == true), Is.True);
            double gap = done.AppliedInputs[1].InputStartWallS.Value - done.AppliedInputs[0].InputEndWallS.Value;
            Assert.That(gap, Is.GreaterThanOrEqualTo(0.09), "step 1 starts only after step 0 was released and the 0.1 s wait elapsed");
            Assert.That(Keyboard.current[Key.W].isPressed, Is.False);
            Assert.That(Gamepad.current.buttonSouth.isPressed, Is.False);
        }

        private static BridgeActionDefinition Action(string name, double leaseS, params BridgeInputBinding[] inputs)
        {
            var definition = new BridgeActionDefinition { Name = name, DefaultLeaseS = leaseS };
            definition.Inputs.AddRange(inputs);
            return definition;
        }

        private IEnumerator Start(BridgeActionDefinition action, string id)
        {
            var request = new ActRequestMessage { ActionId = id };
            Task<(bool Started, string Error)> start = Task.Run(() =>
            {
                bool ok = _lease.TryStart(request, action, 1, out string error);
                return (ok, error);
            });
            DateTime deadline = DateTime.UtcNow.AddSeconds(10);
            while (!start.IsCompleted && DateTime.UtcNow < deadline)
            {
                yield return null;
            }

            Assert.That(start.IsCompleted, Is.True, "the start never returned");
            _started = start.Result.Started;
            _startError = start.Result.Error;
            Assert.That(_started, Is.True, _startError);
        }

        private IEnumerator WaitForTerminal(string id)
        {
            DateTime deadline = DateTime.UtcNow.AddSeconds(15);
            while (Terminal(id) == null && DateTime.UtcNow < deadline)
            {
                yield return null;
            }

            Assert.That(Terminal(id), Is.Not.Null, "no terminal record for " + id);
        }

        private ExecutionRecordMessage Terminal(string id)
        {
            lock (_records)
            {
                return _records.FirstOrDefault(r => r.ActionId == id && r.Status != BridgeExecStatus.Running);
            }
        }

        private static string Describe(ExecutionRecordMessage record)
        {
            return record == null
                ? "no record"
                : "status=" + record.Status + " release_verified=" + (record.ReleaseVerified.HasValue ? record.ReleaseVerified.Value.ToString() : "null");
        }
    }
}
