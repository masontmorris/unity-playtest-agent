using System.Linq;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Unity;
using UnityEngine.InputSystem;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Validation on the real Input System backend. Validation is side-effect free: it checks key
    /// names against the real <c>Key</c> enum by name, so a number is not a key, and it creates no
    /// device. Unity-only, so the Unity-free harness skips it by its name.
    /// </summary>
    [TestFixture]
    public sealed class UnityInputSystemBackendValidationTests
    {
        [TestCase("w", true)]
        [TestCase("W", true)]
        [TestCase("digit1", true)]
        [TestCase("leftShift", true)]
        [TestCase("altGr", true)]
        [TestCase("1", false)]
        [TestCase("9999", false)]
        [TestCase("none", false)]
        [TestCase("notAKey", false)]
        public void TryValidate_ChecksKeyNamesNotNumbers(string key, bool valid)
        {
            using (var backend = new InputSystemInputBackend())
            {
                var action = new BridgeActionDefinition { Name = "k", Control = key };

                Assert.That(backend.TryValidate(action, out string error), Is.EqualTo(valid), error);
            }
        }

        [Test]
        public void TryValidate_CreatesNoDeviceAndRejectsUnsupportedControls()
        {
            using (var backend = new InputSystemInputBackend())
            {
                int devices = InputSystem.devices.Count;
                var padAction = new BridgeActionDefinition { Name = "pad" };
                padAction.Inputs.Add(new BridgeInputBinding("Gamepad", "leftStick", 0.25, -0.5));
                var mouseAction = new BridgeActionDefinition { Name = "mouse" };
                mouseAction.Inputs.Add(new BridgeInputBinding("Mouse", "delta", 1, 1));
                var scroll = new BridgeActionDefinition { Name = "scroll" };
                scroll.Inputs.Add(new BridgeInputBinding("Mouse", "scroll", 0, 1));
                var child = new BridgeActionDefinition { Name = "child" };
                child.Inputs.Add(new BridgeInputBinding("Gamepad", "dpad/up"));

                Assert.That(backend.TryValidate(padAction, out string error), Is.True, error);
                Assert.That(backend.TryValidate(mouseAction, out error), Is.True, error);
                Assert.That(backend.TryValidate(scroll, out error), Is.False);
                Assert.That(error, Does.Contain("scroll"));
                Assert.That(backend.TryValidate(child, out error), Is.False);

                Assert.That(
                    InputSystem.devices.Count,
                    Is.EqualTo(devices),
                    "validating a gamepad or mouse action must not add a device; only applying one does");
            }
        }
    }
}
