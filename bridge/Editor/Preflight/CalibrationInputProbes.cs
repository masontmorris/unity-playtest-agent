// SPDX-License-Identifier: Apache-2.0
using Playtest.Bridge.Actions;

namespace Playtest.Bridge.Editor.Preflight
{
    public static class CalibrationInputProbes
    {
        // Registration grants a probe name; Python decides from measurements whether to execute it.
        public static BridgeActionDefinition Negated(string name, string device, string control, double? x, double? y)
        {
            if (!x.HasValue || !y.HasValue || (x.Value == 0 && y.Value == 0)) return null;
            if (device == "Gamepad" && (control == "leftTrigger" || control == "rightTrigger")) return null;
            return new BridgeActionDefinition
            {
                Name = name + "_negated",
                Inputs = { new BridgeInputBinding(device, control, -x.Value, -y.Value) },
                DefaultLeaseS = 0.4,
            };
        }
    }
}
