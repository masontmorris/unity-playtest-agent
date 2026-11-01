// SPDX-License-Identifier: Apache-2.0
using NUnit.Framework;
using Playtest.Bridge.Editor.Preflight;

namespace Playtest.Bridge.Tests
{
    public sealed class UnityCalibrationInputProbesTests
    {
        [TestCase("Mouse", "delta")]
        [TestCase("Gamepad", "rightStick")]
        [TestCase("Gamepad", "leftStick")]
        public void NegatedProbePreservesControlAndNegatesValue(string device, string control)
        {
            var probe = CalibrationInputProbes.Negated("probe", device, control, 1, 0);
            Assert.That(probe.Name, Is.EqualTo("probe_negated"));
            Assert.That(probe.Inputs.Count, Is.EqualTo(1));
            Assert.That(probe.Inputs[0].Device, Is.EqualTo(device));
            Assert.That(probe.Inputs[0].Control, Is.EqualTo(control));
            Assert.That(probe.Inputs[0].X, Is.EqualTo(-1));
            Assert.That(probe.Inputs[0].Y, Is.EqualTo(0));
        }
        [Test]
        public void ButtonsAndUnsignedAxesHaveNoNegatedProbe()
        {
            Assert.That(CalibrationInputProbes.Negated("probe", "Keyboard", "w", null, null), Is.Null);
            Assert.That(CalibrationInputProbes.Negated("probe", "Gamepad", "leftTrigger", 1, 0), Is.Null);
        }
    }
}
