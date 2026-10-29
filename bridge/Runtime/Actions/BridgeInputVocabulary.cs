using System;
using System.Collections.Generic;
using System.Globalization;

namespace Playtest.Bridge.Actions
{
    /// <summary>How a device control is driven and which declared values it takes.</summary>
    public enum BridgeControlKind
    {
        /// <summary>A pressed/not pressed control. Declares no x or y.</summary>
        Button,

        /// <summary>A one dimensional axis. Declares its value in x and y = 0.</summary>
        Axis,

        /// <summary>A two dimensional control whose x and y go into the device state unchanged.</summary>
        Vector2,

        /// <summary>Four digital directions. Declares x and y, each -1, 0 or 1.</summary>
        Dpad,

        /// <summary>A relative pixel displacement applied once per apply, never held.</summary>
        Impulse,
    }

    /// <summary>What the vocabulary knows about one declared control.</summary>
    public sealed class BridgeControlInfo
    {
        public BridgeControlInfo(string device, string control, BridgeControlKind kind, double minX, double maxX)
        {
            Device = device;
            Control = control;
            Kind = kind;
            MinX = minX;
            MaxX = maxX;
        }

        /// <summary>Canonical device name: Keyboard, Mouse or Gamepad.</summary>
        public string Device { get; }

        /// <summary>Canonical control name. Keyboard keys keep the declared spelling.</summary>
        public string Control { get; }

        public BridgeControlKind Kind { get; }

        /// <summary>Smallest value the device accepts in x.</summary>
        public double MinX { get; }

        /// <summary>Largest value the device accepts in x.</summary>
        public double MaxX { get; }
    }

    /// <summary>
    /// The devices and controls an action may declare. Only Unity root controls of a keyboard,
    /// mouse and gamepad are executable; absolute pointers, touch, VR and child control paths
    /// are rejected by name rather than approximated. This is pure data: it never chooses an
    /// action, it only says which declared inputs can be executed and which values they accept.
    /// </summary>
    public static class BridgeInputVocabulary
    {
        public const string Keyboard = "Keyboard";
        public const string Mouse = "Mouse";
        public const string Gamepad = "Gamepad";

        /// <summary>A mouse delta input of x = 1 moves the pointer this many pixels.</summary>
        public const double MouseDeltaPixelsPerUnit = 64;

        public const int MaxInputsPerStep = 3;

        public const int MaxSteps = 8;

        public const double MaxHoldSeconds = 3600;

        private static readonly string[] DeviceNames = { Keyboard, Mouse, Gamepad };

        private static readonly Dictionary<string, BridgeControlInfo> MouseControls = BuildMouse();

        private static readonly Dictionary<string, BridgeControlInfo> GamepadControls = BuildGamepad();

        private static readonly Dictionary<string, string> GamepadAliases =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                { "leftStickButton", "leftStickPress" },
                { "rightStickButton", "rightStickPress" },
                { "startButton", "start" },
                { "selectButton", "select" },
            };

        public static IReadOnlyList<string> Devices
        {
            get { return DeviceNames; }
        }

        public static bool TryCanonicalDevice(string device, out string canonical)
        {
            canonical = null;
            if (string.IsNullOrEmpty(device))
            {
                return false;
            }

            for (int i = 0; i < DeviceNames.Length; i++)
            {
                if (string.Equals(DeviceNames[i], device, StringComparison.OrdinalIgnoreCase))
                {
                    canonical = DeviceNames[i];
                    return true;
                }
            }

            return false;
        }

        /// <summary>Resolves a declared device and control name to its kind. Side-effect free.</summary>
        public static bool TryResolve(string device, string control, out BridgeControlInfo info, out string error)
        {
            info = null;
            error = null;
            if (string.IsNullOrEmpty(device))
            {
                error = "an input needs a device";
                return false;
            }

            if (!TryCanonicalDevice(device, out string canonicalDevice))
            {
                error = "unsupported input device '" + device + "'; supported devices are Keyboard, Mouse and Gamepad";
                return false;
            }

            if (string.IsNullOrEmpty(control))
            {
                error = "an input needs a control";
                return false;
            }

            if (control.IndexOf('/') >= 0 || control.IndexOf('<') >= 0 || control.IndexOf('>') >= 0)
            {
                error = "control '" + control + "' is a path; declare the device's root control name only";
                return false;
            }

            if (canonicalDevice == Keyboard)
            {
                if (control.Trim().Length != control.Length)
                {
                    error = "key name '" + control + "' has surrounding whitespace";
                    return false;
                }

                info = new BridgeControlInfo(Keyboard, control, BridgeControlKind.Button, 0, 0);
                return true;
            }

            if (canonicalDevice == Mouse)
            {
                if (MouseControls.TryGetValue(control, out info))
                {
                    return true;
                }

                if (string.Equals(control, "scroll", StringComparison.OrdinalIgnoreCase))
                {
                    error = "mouse scroll is not supported: its units differ by platform, so a normalized value would be inferred";
                }
                else if (string.Equals(control, "position", StringComparison.OrdinalIgnoreCase))
                {
                    error = "absolute pointer position is not supported; only the relative delta impulse is";
                }
                else
                {
                    error = "unsupported mouse control '" + control + "'";
                }

                return false;
            }

            string name = control;
            if (GamepadAliases.TryGetValue(control, out string aliased))
            {
                name = aliased;
            }

            if (GamepadControls.TryGetValue(name, out info))
            {
                return true;
            }

            error = "unsupported gamepad control '" + control + "'";
            return false;
        }

        /// <summary>Checks the declared x and y against what the control accepts.</summary>
        public static bool TryValidateValues(BridgeControlInfo info, double? x, double? y, out string error)
        {
            error = null;
            if (info == null)
            {
                error = "missing control";
                return false;
            }

            string label = info.Device + " " + info.Control;
            switch (info.Kind)
            {
                case BridgeControlKind.Button:
                    if (x.HasValue || y.HasValue)
                    {
                        error = label + " is a button and takes no x or y";
                        return false;
                    }

                    return true;

                case BridgeControlKind.Axis:
                    if (!IsFinite(x) || x.Value < info.MinX || x.Value > info.MaxX)
                    {
                        error = label + " needs x between " + Format(info.MinX) + " and " + Format(info.MaxX);
                        return false;
                    }

                    if (!y.HasValue || y.Value != 0)
                    {
                        error = label + " is a one dimensional axis: declare its value in x and y = 0";
                        return false;
                    }

                    return true;

                case BridgeControlKind.Dpad:
                    if (!IsFinite(x) || !IsFinite(y) || !IsDigital(x.Value) || !IsDigital(y.Value))
                    {
                        error = label + " needs x and y that are each -1, 0 or 1";
                        return false;
                    }

                    return true;

                default:
                    if (!IsFinite(x) || !IsFinite(y)
                        || x.Value < -1 || x.Value > 1 || y.Value < -1 || y.Value > 1)
                    {
                        error = label + " needs finite x and y between -1 and 1";
                        return false;
                    }

                    return true;
            }
        }

        /// <summary>Resolves one input and checks its values.</summary>
        public static bool TryValidate(BridgeInputBinding input, out BridgeControlInfo info, out string error)
        {
            info = null;
            if (input == null)
            {
                error = "missing input";
                return false;
            }

            if (!TryResolve(input.Device, input.Control, out info, out error))
            {
                return false;
            }

            return TryValidateValues(info, input.X, input.Y, out error);
        }

        /// <summary>
        /// Checks the inputs of one action or one skill step: one to three inputs, each valid, no
        /// control declared twice. The vocabulary is checked here; only the backend knows which
        /// keyboard key names exist.
        /// </summary>
        public static bool TryValidateInputs(
            IReadOnlyList<BridgeInputBinding> inputs,
            bool checkVocabulary,
            out string error)
        {
            error = null;
            if (inputs == null || inputs.Count == 0)
            {
                error = "at least one input is required";
                return false;
            }

            if (inputs.Count > MaxInputsPerStep)
            {
                error = "at most " + MaxInputsPerStep.ToString(CultureInfo.InvariantCulture) + " simultaneous inputs are allowed";
                return false;
            }

            var seen = new HashSet<string>(StringComparer.Ordinal);
            for (int i = 0; i < inputs.Count; i++)
            {
                BridgeInputBinding input = inputs[i];
                if (input == null)
                {
                    error = "input " + i.ToString(CultureInfo.InvariantCulture) + " is missing";
                    return false;
                }

                if (string.IsNullOrEmpty(input.Control))
                {
                    error = "input " + i.ToString(CultureInfo.InvariantCulture) + " needs a control";
                    return false;
                }

                string identity = input.Device == null ? string.Empty : input.Device.ToLowerInvariant();
                if (checkVocabulary)
                {
                    if (!TryValidate(input, out BridgeControlInfo info, out string inputError))
                    {
                        error = "input " + i.ToString(CultureInfo.InvariantCulture) + ": " + inputError;
                        return false;
                    }

                    identity = info.Device.ToLowerInvariant() + ":" + info.Control.ToLowerInvariant();
                }
                else
                {
                    identity += ":" + input.Control.ToLowerInvariant();
                }

                if (!seen.Add(identity))
                {
                    error = "input " + i.ToString(CultureInfo.InvariantCulture) + " repeats the control " + input.Control;
                    return false;
                }
            }

            return true;
        }

        /// <summary>The name reported in a stop result for a released input.</summary>
        public static string ReleasedName(BridgeInputBinding input)
        {
            string device = input.Device == null ? string.Empty : input.Device.ToLowerInvariant();
            string control = input.Control == null ? string.Empty : input.Control.ToLowerInvariant();
            return string.Equals(device, "keyboard", StringComparison.Ordinal)
                ? "key:" + control
                : device + ":" + control;
        }

        private static bool IsFinite(double? value)
        {
            return value.HasValue && !double.IsNaN(value.Value) && !double.IsInfinity(value.Value);
        }

        private static bool IsDigital(double value)
        {
            return value == -1 || value == 0 || value == 1;
        }

        private static string Format(double value)
        {
            return value.ToString("0.###", CultureInfo.InvariantCulture);
        }

        private static Dictionary<string, BridgeControlInfo> BuildMouse()
        {
            var table = new Dictionary<string, BridgeControlInfo>(StringComparer.OrdinalIgnoreCase);
            foreach (string button in new[] { "leftButton", "rightButton", "middleButton", "forwardButton", "backButton" })
            {
                table[button] = new BridgeControlInfo(Mouse, button, BridgeControlKind.Button, 0, 0);
            }

            table["delta"] = new BridgeControlInfo(Mouse, "delta", BridgeControlKind.Impulse, -1, 1);
            return table;
        }

        private static Dictionary<string, BridgeControlInfo> BuildGamepad()
        {
            var table = new Dictionary<string, BridgeControlInfo>(StringComparer.OrdinalIgnoreCase);
            foreach (string button in new[]
            {
                "buttonSouth", "buttonNorth", "buttonEast", "buttonWest", "leftShoulder", "rightShoulder",
                "leftStickPress", "rightStickPress", "start", "select",
            })
            {
                table[button] = new BridgeControlInfo(Gamepad, button, BridgeControlKind.Button, 0, 0);
            }

            table["leftStick"] = new BridgeControlInfo(Gamepad, "leftStick", BridgeControlKind.Vector2, -1, 1);
            table["rightStick"] = new BridgeControlInfo(Gamepad, "rightStick", BridgeControlKind.Vector2, -1, 1);
            table["dpad"] = new BridgeControlInfo(Gamepad, "dpad", BridgeControlKind.Dpad, -1, 1);
            table["leftTrigger"] = new BridgeControlInfo(Gamepad, "leftTrigger", BridgeControlKind.Axis, 0, 1);
            table["rightTrigger"] = new BridgeControlInfo(Gamepad, "rightTrigger", BridgeControlKind.Axis, 0, 1);
            return table;
        }
    }
}
