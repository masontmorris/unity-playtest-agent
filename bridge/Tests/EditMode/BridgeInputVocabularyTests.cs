using System.Collections.Generic;
using NUnit.Framework;
using Playtest.Bridge.Actions;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The devices and controls an action may declare. Every rejection here is explicit: an
    /// unsupported control is never approximated, remapped or clamped into something executable.
    /// </summary>
    [TestFixture]
    public sealed class BridgeInputVocabularyTests
    {
        [TestCase("Keyboard", "w", BridgeControlKind.Button)]
        [TestCase("keyboard", "LeftShift", BridgeControlKind.Button)]
        [TestCase("Mouse", "leftButton", BridgeControlKind.Button)]
        [TestCase("Mouse", "delta", BridgeControlKind.Impulse)]
        [TestCase("Gamepad", "buttonSouth", BridgeControlKind.Button)]
        [TestCase("Gamepad", "leftStick", BridgeControlKind.Vector2)]
        [TestCase("Gamepad", "rightStick", BridgeControlKind.Vector2)]
        [TestCase("Gamepad", "dpad", BridgeControlKind.Dpad)]
        [TestCase("Gamepad", "leftTrigger", BridgeControlKind.Axis)]
        [TestCase("Gamepad", "rightTrigger", BridgeControlKind.Axis)]
        [TestCase("Gamepad", "leftStickButton", BridgeControlKind.Button)]
        public void Resolve_AcceptsRootControls(string device, string control, BridgeControlKind kind)
        {
            Assert.That(BridgeInputVocabulary.TryResolve(device, control, out BridgeControlInfo info, out string error), Is.True, error);
            Assert.That(info.Kind, Is.EqualTo(kind));
        }

        [TestCase("Gamepad", "leftStickButton", "leftStickPress")]
        [TestCase("Gamepad", "startButton", "start")]
        [TestCase("gamepad", "LEFTSTICK", "leftStick")]
        public void Resolve_CanonicalizesNamesWithoutChangingTheDeclaration(string device, string control, string canonical)
        {
            Assert.That(BridgeInputVocabulary.TryResolve(device, control, out BridgeControlInfo info, out string error), Is.True, error);
            Assert.That(info.Control, Is.EqualTo(canonical));
            Assert.That(info.Device, Is.EqualTo("Gamepad"));
        }

        [TestCase("Touchscreen", "primaryTouch", "unsupported input device")]
        [TestCase("Joystick", "stick", "unsupported input device")]
        [TestCase("XRController", "trigger", "unsupported input device")]
        [TestCase("Mouse", "scroll", "scroll is not supported")]
        [TestCase("Mouse", "position", "absolute pointer")]
        [TestCase("Mouse", "clickCount", "unsupported mouse control")]
        [TestCase("Gamepad", "dpad/up", "is a path")]
        [TestCase("Gamepad", "leftStick/x", "is a path")]
        [TestCase("Gamepad", "<Gamepad>/leftStick", "is a path")]
        [TestCase("Gamepad", "buttonNothing", "unsupported gamepad control")]
        [TestCase("Keyboard", " w", "whitespace")]
        [TestCase("Keyboard", "", "needs a control")]
        [TestCase("", "w", "needs a device")]
        public void Resolve_RejectsUnsupportedControlsByName(string device, string control, string expected)
        {
            Assert.That(BridgeInputVocabulary.TryResolve(device, control, out _, out string error), Is.False);
            Assert.That(error, Does.Contain(expected));
        }

        [Test]
        public void Values_ButtonsAndKeysDeclareNoXOrY()
        {
            Resolve("Keyboard", "w", out BridgeControlInfo key);
            Resolve("Gamepad", "buttonSouth", out BridgeControlInfo button);
            foreach (BridgeControlInfo info in new[] { key, button })
            {
                Assert.That(BridgeInputVocabulary.TryValidateValues(info, null, null, out string error), Is.True, error);
                Assert.That(BridgeInputVocabulary.TryValidateValues(info, 1, null, out _), Is.False);
                Assert.That(BridgeInputVocabulary.TryValidateValues(info, null, 0, out _), Is.False);
            }
        }

        [Test]
        public void Values_OneDimensionalAxisUsesXWithYZero()
        {
            Resolve("Gamepad", "leftTrigger", out BridgeControlInfo trigger);
            Assert.That(BridgeInputVocabulary.TryValidateValues(trigger, 0.75, 0, out string error), Is.True, error);
            Assert.That(BridgeInputVocabulary.TryValidateValues(trigger, 0.75, null, out _), Is.False, "y must be 0, not absent");
            Assert.That(BridgeInputVocabulary.TryValidateValues(trigger, 0.75, 0.5, out _), Is.False);
            Assert.That(BridgeInputVocabulary.TryValidateValues(trigger, null, 0, out _), Is.False);
            Assert.That(BridgeInputVocabulary.TryValidateValues(trigger, -0.5, 0, out _), Is.False, "a trigger does not go negative");
            Assert.That(BridgeInputVocabulary.TryValidateValues(trigger, 1.01, 0, out _), Is.False);
        }

        [Test]
        public void Values_VectorControlsNeedBothFiniteValuesAndAreNeverClamped()
        {
            Resolve("Gamepad", "leftStick", out BridgeControlInfo stick);
            Assert.That(BridgeInputVocabulary.TryValidateValues(stick, 0.3, -0.7, out string error), Is.True, error);
            Assert.That(BridgeInputVocabulary.TryValidateValues(stick, 1, 1, out _), Is.True, "a diagonal of length 1.41 is declared, not normalized");
            Assert.That(BridgeInputVocabulary.TryValidateValues(stick, 0.3, null, out _), Is.False);
            Assert.That(BridgeInputVocabulary.TryValidateValues(stick, null, null, out _), Is.False);
            Assert.That(BridgeInputVocabulary.TryValidateValues(stick, double.NaN, 0, out _), Is.False);
            Assert.That(BridgeInputVocabulary.TryValidateValues(stick, 0, double.PositiveInfinity, out _), Is.False);
            Assert.That(BridgeInputVocabulary.TryValidateValues(stick, 1.0000001, 0, out _), Is.False);
            Assert.That(BridgeInputVocabulary.TryValidateValues(stick, 0, -1.5, out _), Is.False);
        }

        [Test]
        public void Values_DpadIsFourDigitalDirections()
        {
            Resolve("Gamepad", "dpad", out BridgeControlInfo dpad);
            Assert.That(BridgeInputVocabulary.TryValidateValues(dpad, 1, -1, out string error), Is.True, error);
            Assert.That(BridgeInputVocabulary.TryValidateValues(dpad, 0, 0, out _), Is.True);
            Assert.That(BridgeInputVocabulary.TryValidateValues(dpad, 0.5, 0, out _), Is.False, "a dpad cannot represent half a direction");
            Assert.That(BridgeInputVocabulary.TryValidateValues(dpad, 1, null, out _), Is.False);
        }

        [Test]
        public void Values_MouseDeltaNeedsBothComponentsWithinTheDeclaredBound()
        {
            Resolve("Mouse", "delta", out BridgeControlInfo delta);
            Assert.That(BridgeInputVocabulary.TryValidateValues(delta, 0.5, -0.25, out string error), Is.True, error);
            Assert.That(BridgeInputVocabulary.TryValidateValues(delta, 1, -1, out _), Is.True);
            Assert.That(BridgeInputVocabulary.TryValidateValues(delta, 1.5, 0, out _), Is.False, "the 64 px bound is fixed");
            Assert.That(BridgeInputVocabulary.TryValidateValues(delta, 0.5, null, out _), Is.False);
            Assert.That(BridgeInputVocabulary.MouseDeltaPixelsPerUnit, Is.EqualTo(64));
        }

        [Test]
        public void Inputs_AreOneToThreeAndDistinct()
        {
            var w = new BridgeInputBinding("Keyboard", "w");
            var a = new BridgeInputBinding("Keyboard", "a");
            var s = new BridgeInputBinding("Keyboard", "s");
            var d = new BridgeInputBinding("Keyboard", "d");

            Assert.That(BridgeInputVocabulary.TryValidateInputs(new[] { w, a, s }, true, out string error), Is.True, error);
            Assert.That(BridgeInputVocabulary.TryValidateInputs(new[] { w, a, s, d }, true, out error), Is.False);
            Assert.That(error, Does.Contain("at most 3"));
            Assert.That(BridgeInputVocabulary.TryValidateInputs(new BridgeInputBinding[0], true, out error), Is.False);
            Assert.That(BridgeInputVocabulary.TryValidateInputs(new[] { w, new BridgeInputBinding("keyboard", "W".ToLowerInvariant()) }, true, out error), Is.False);
            Assert.That(error, Does.Contain("repeats"));
            Assert.That(
                BridgeInputVocabulary.TryValidateInputs(
                    new[] { new BridgeInputBinding("Gamepad", "leftStickButton"), new BridgeInputBinding("Gamepad", "leftStickPress") },
                    true,
                    out error),
                Is.False,
                "an alias of a control already declared is the same control");
        }

        [Test]
        public void ReleasedNames_KeepTheKeyPrefixAndNameOtherDevices()
        {
            Assert.That(BridgeInputVocabulary.ReleasedName(new BridgeInputBinding("Keyboard", "W")), Is.EqualTo("key:w"));
            Assert.That(BridgeInputVocabulary.ReleasedName(new BridgeInputBinding("Gamepad", "leftStick", 1, 0)), Is.EqualTo("gamepad:leftstick"));
            Assert.That(BridgeInputVocabulary.ReleasedName(new BridgeInputBinding("Mouse", "delta", 0.5, 0)), Is.EqualTo("mouse:delta"));
        }

        private static void Resolve(string device, string control, out BridgeControlInfo info)
        {
            Assert.That(BridgeInputVocabulary.TryResolve(device, control, out info, out string error), Is.True, error);
        }
    }
}
