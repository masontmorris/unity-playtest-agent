// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Playtest.Bridge.Actions
{
    /// <summary>
    /// One declared device input. <see cref="X"/> and <see cref="Y"/> are the declared control
    /// values; buttons and keys declare neither. The device state receives them unchanged, except a
    /// mouse delta, which is a relative displacement of <see cref="BridgeInputVocabulary.MouseDeltaPixelsPerUnit"/>
    /// pixels per unit. The bridge never normalizes, clamps or infers a value.
    /// </summary>
    public sealed class BridgeInputBinding : IEquatable<BridgeInputBinding>
    {
        public BridgeInputBinding(string device, string control, double? x = null, double? y = null)
        {
            Device = device;
            Control = control;
            X = x;
            Y = y;
        }

        public string Device { get; }

        public string Control { get; }

        public double? X { get; }

        public double? Y { get; }

        public bool Equals(BridgeInputBinding other)
        {
            return other != null
                && string.Equals(Device, other.Device, StringComparison.OrdinalIgnoreCase)
                && string.Equals(Control, other.Control, StringComparison.Ordinal)
                && X.Equals(other.X)
                && Y.Equals(other.Y);
        }

        public override bool Equals(object obj)
        {
            return Equals(obj as BridgeInputBinding);
        }

        public override int GetHashCode()
        {
            return (Control ?? string.Empty).GetHashCode();
        }

        public override string ToString()
        {
            string values = X.HasValue || Y.HasValue
                ? "(" + Format(X) + "," + Format(Y) + ")"
                : string.Empty;
            return Device + "." + Control + values;
        }

        private static string Format(double? value)
        {
            return value.HasValue ? value.Value.ToString("R", CultureInfo.InvariantCulture) : "null";
        }
    }

    /// <summary>One skill step: hold the inputs for <see cref="HoldS"/>, release them, then wait <see cref="WaitS"/>.</summary>
    public sealed class BridgeActionStep
    {
        public BridgeActionStep(IEnumerable<BridgeInputBinding> inputs, double holdS, double waitS = 0)
        {
            Inputs = new ReadOnlyCollection<BridgeInputBinding>(
                new List<BridgeInputBinding>(inputs ?? Enumerable.Empty<BridgeInputBinding>()));
            HoldS = holdS;
            WaitS = waitS;
        }

        public IReadOnlyList<BridgeInputBinding> Inputs { get; }

        public double HoldS { get; }

        public double WaitS { get; }
    }

    /// <summary>
    /// One manifest action or skill. The name and bindings are data from the project manifest; the
    /// bridge never hardcodes a game action.
    ///
    /// An action declares one to three simultaneous <see cref="Inputs"/> and a
    /// <see cref="DefaultLeaseS"/>. A skill declares one to eight <see cref="Steps"/> and a
    /// <see cref="MaxS"/> that bounds the whole sequence. <see cref="Device"/> and
    /// <see cref="Control"/> are the original single-key shorthand and are kept as a compatibility
    /// adapter: setting them declares one input.
    /// </summary>
    public sealed class BridgeActionDefinition
    {
        public const string InputSystemBackend = "input_system";

        private readonly List<BridgeInputBinding> _inputs = new List<BridgeInputBinding>();
        private readonly List<BridgeActionStep> _steps = new List<BridgeActionStep>();
        private string _name;
        private string _backend = InputSystemBackend;
        private string _legacyDevice;
        private string _legacyControl;
        private double _defaultLeaseS = 0.25;
        private double? _maxS;
        private bool _frozen;

        public string Name
        {
            get { return _name; }
            set { Guard(); _name = value; }
        }

        public string Backend
        {
            get { return _backend; }
            set { Guard(); _backend = value; }
        }

        /// <summary>Single-key shorthand: the device of the one input. Defaults to Keyboard.</summary>
        public string Device
        {
            get { return _inputs.Count > 0 ? _inputs[0].Device : (_legacyDevice ?? BridgeInputVocabulary.Keyboard); }
            set { Guard(); _legacyDevice = value; }
        }

        /// <summary>Single-key shorthand: the control of the one input.</summary>
        public string Control
        {
            get { return _inputs.Count > 0 ? _inputs[0].Control : _legacyControl; }
            set { Guard(); _legacyControl = value; }
        }

        /// <summary>The hold, in seconds, when the request carries no lease. Actions only.</summary>
        public double DefaultLeaseS
        {
            get { return _defaultLeaseS; }
            set { Guard(); _defaultLeaseS = value; }
        }

        /// <summary>The bound, in seconds, on the whole skill. Skills only.</summary>
        public double? MaxS
        {
            get { return _maxS; }
            set { Guard(); _maxS = value; }
        }

        /// <summary>The simultaneous inputs of an action. A frozen definition returns a copy.</summary>
        public List<BridgeInputBinding> Inputs
        {
            get { return _frozen ? new List<BridgeInputBinding>(_inputs) : _inputs; }
        }

        /// <summary>The ordered steps of a skill. A frozen definition returns a copy.</summary>
        public List<BridgeActionStep> Steps
        {
            get { return _frozen ? new List<BridgeActionStep>(_steps) : _steps; }
        }

        public bool IsSkill
        {
            get { return _steps.Count > 0; }
        }

        public bool IsFrozen
        {
            get { return _frozen; }
        }

        internal bool HasLegacyControl
        {
            get { return !string.IsNullOrEmpty(_legacyControl); }
        }

        internal IReadOnlyList<BridgeInputBinding> RawInputs
        {
            get { return _inputs; }
        }

        internal IReadOnlyList<BridgeActionStep> RawSteps
        {
            get { return _steps; }
        }

        /// <summary>The input sets in execution order: an action's inputs, or each step's inputs.</summary>
        public IReadOnlyList<IReadOnlyList<BridgeInputBinding>> InputSets()
        {
            var sets = new List<IReadOnlyList<BridgeInputBinding>>();
            if (_steps.Count > 0)
            {
                for (int i = 0; i < _steps.Count; i++)
                {
                    sets.Add(_steps[i].Inputs);
                }
            }
            else if (_inputs.Count > 0)
            {
                sets.Add(_inputs);
            }
            else if (HasLegacyControl)
            {
                sets.Add(new[] { new BridgeInputBinding(_legacyDevice ?? BridgeInputVocabulary.Keyboard, _legacyControl) });
            }

            return sets;
        }

        /// <summary>
        /// An immutable copy with the single-key shorthand resolved into <see cref="Inputs"/>. The
        /// catalog stores only frozen copies, so a definition cannot change after it was validated.
        /// </summary>
        internal BridgeActionDefinition FrozenCopy()
        {
            var copy = new BridgeActionDefinition
            {
                _name = _name,
                _backend = _backend,
                _defaultLeaseS = _defaultLeaseS,
                _maxS = _maxS,
            };
            if (_steps.Count > 0)
            {
                copy._steps.AddRange(_steps);
            }
            else if (_inputs.Count > 0)
            {
                copy._inputs.AddRange(_inputs);
            }
            else if (HasLegacyControl)
            {
                copy._inputs.Add(new BridgeInputBinding(_legacyDevice ?? BridgeInputVocabulary.Keyboard, _legacyControl));
            }

            copy._frozen = true;
            return copy;
        }

        private void Guard()
        {
            if (_frozen)
            {
                throw new InvalidOperationException("a catalog action cannot be changed after it was validated");
            }
        }
    }

    /// <summary>
    /// The project's action bindings, loaded from a manifest file. A missing file means no
    /// actions, not a default action. Every definition is validated before any state is touched:
    /// input counts, device and control vocabulary, declared values, durations and the skill bound.
    /// </summary>
    public sealed class BridgeActionCatalog
    {
        public const double MaxSeconds = BridgeInputVocabulary.MaxHoldSeconds;

        // Python sums the declared durations with its own float arithmetic; a few ulps of
        // disagreement at an exact boundary must not decide validity.
        private const double DurationEpsilon = 1e-9;

        private static readonly string[] ActionMembers = { "backend", "inputs", "default_lease_s" };
        private static readonly string[] SkillMembers = { "backend", "steps", "max_s" };
        private static readonly string[] ModernMembers = { "backend", "inputs", "default_lease_s", "steps", "max_s" };
        private static readonly string[] LegacyMembers = { "backend", "device", "control", "default_lease_s" };
        private static readonly string[] InputMembers = { "device", "control", "x", "y" };
        private static readonly string[] StepMembers = { "inputs", "hold_s", "wait_s" };

        public static readonly BridgeActionCatalog Empty =
            new BridgeActionCatalog(new Dictionary<string, BridgeActionDefinition>(StringComparer.Ordinal));

        private readonly Dictionary<string, BridgeActionDefinition> _actions;

        private BridgeActionCatalog(Dictionary<string, BridgeActionDefinition> actions)
        {
            _actions = actions;
        }

        public int Count
        {
            get { return _actions.Count; }
        }

        public IEnumerable<string> Names
        {
            get { return _actions.Keys.OrderBy(name => name, StringComparer.Ordinal); }
        }

        public bool TryGet(string name, out BridgeActionDefinition action)
        {
            action = null;
            return !string.IsNullOrEmpty(name) && _actions.TryGetValue(name, out action);
        }

        /// <summary>
        /// Loads <c>{"actions": {"name": definition}}</c>. A definition is either an action
        /// (<c>inputs</c> and <c>default_lease_s</c>), a skill (<c>steps</c> and <c>max_s</c>) or the
        /// original single-key shorthand (<c>device</c>, <c>control</c>, <c>default_lease_s</c>).
        /// Missing files are an empty catalog; malformed files throw instead of inventing actions.
        /// </summary>
        public static BridgeActionCatalog Load(string path)
        {
            if (string.IsNullOrEmpty(path) || !File.Exists(path))
            {
                return Empty;
            }

            return Parse(File.ReadAllText(path), path);
        }

        /// <summary>Parses catalog text strictly. <paramref name="source"/> only labels errors.</summary>
        public static BridgeActionCatalog Parse(string text, string source)
        {
            JToken token;
            try
            {
                token = JToken.Parse(
                    text ?? string.Empty,
                    new JsonLoadSettings { DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error });
            }
            catch (JsonException ex)
            {
                throw new InvalidDataException("action catalog is not valid JSON: " + source, ex);
            }

            if (!(token is JObject root) || root["actions"] is not JObject actions)
            {
                throw new InvalidDataException("action catalog needs an 'actions' object: " + source);
            }

            foreach (JProperty member in root.Properties())
            {
                if (member.Name != "actions")
                {
                    throw new InvalidDataException("action catalog has an unexpected member '" + member.Name + "': " + source);
                }
            }

            var definitions = new List<BridgeActionDefinition>();
            foreach (JProperty property in actions.Properties())
            {
                if (!(property.Value is JObject body))
                {
                    throw new InvalidDataException("action '" + property.Name + "' must be an object: " + source);
                }

                try
                {
                    definitions.Add(ReadDefinition(property.Name, body));
                }
                catch (InvalidDataException ex)
                {
                    throw new InvalidDataException(ex.Message + ": " + source, ex);
                }
            }

            try
            {
                return Build(definitions, checkVocabulary: true);
            }
            catch (ArgumentException ex)
            {
                throw new InvalidDataException(ex.Message + ": " + source, ex);
            }
        }

        /// <summary>
        /// Test and harness seam: build a catalog without a file. Structure is validated; which
        /// devices exist is left to the backend that will execute the action.
        /// </summary>
        public static BridgeActionCatalog FromDefinitions(IEnumerable<BridgeActionDefinition> definitions)
        {
            return Build(definitions ?? Enumerable.Empty<BridgeActionDefinition>(), checkVocabulary: false);
        }

        private static BridgeActionCatalog Build(IEnumerable<BridgeActionDefinition> definitions, bool checkVocabulary)
        {
            var parsed = new Dictionary<string, BridgeActionDefinition>(StringComparer.Ordinal);
            foreach (BridgeActionDefinition definition in definitions)
            {
                string error = Validate(definition, checkVocabulary);
                if (error != null)
                {
                    throw new ArgumentException(error, nameof(definitions));
                }

                parsed[definition.Name] = definition.FrozenCopy();
            }

            return new BridgeActionCatalog(parsed);
        }

        /// <summary>The first problem with a definition, or null when it can be executed.</summary>
        internal static string Validate(BridgeActionDefinition definition, bool checkVocabulary)
        {
            if (definition == null || string.IsNullOrEmpty(definition.Name))
            {
                return "every action needs a name";
            }

            if (!Regex.IsMatch(definition.Name, @"\A[a-z][a-z0-9_]{0,39}\z", RegexOptions.CultureInvariant))
            {
                return "action name '" + definition.Name + "' is not a lower_snake_case identifier";
            }

            string label = "action '" + definition.Name + "'";
            if (string.IsNullOrEmpty(definition.Backend))
            {
                return label + " needs a backend";
            }

            if (checkVocabulary
                && !string.Equals(definition.Backend, BridgeActionDefinition.InputSystemBackend, StringComparison.OrdinalIgnoreCase))
            {
                return label + " uses unsupported backend '" + definition.Backend + "'";
            }

            bool hasInputs = definition.RawInputs.Count > 0;
            bool hasSteps = definition.RawSteps.Count > 0;
            if (hasInputs && hasSteps)
            {
                return label + " declares both inputs and steps; an action has inputs, a skill has steps";
            }

            if (definition.HasLegacyControl && (hasInputs || hasSteps))
            {
                return label + " mixes device/control with inputs or steps";
            }

            if (hasSteps)
            {
                return ValidateSkill(definition, label, checkVocabulary);
            }

            if (!hasInputs && !definition.HasLegacyControl)
            {
                return label + " needs a control";
            }

            if (definition.MaxS.HasValue)
            {
                return label + " declares max_s; only a skill does";
            }

            double lease = definition.DefaultLeaseS;
            if (double.IsNaN(lease) || double.IsInfinity(lease) || lease <= 0)
            {
                return label + " default_lease_s must be positive";
            }

            if (lease > MaxSeconds)
            {
                return label + " default_lease_s must be at most " + MaxSeconds.ToString("0", CultureInfo.InvariantCulture) + " s";
            }

            IReadOnlyList<BridgeInputBinding> inputs = definition.InputSets()[0];
            if (!BridgeInputVocabulary.TryValidateInputs(inputs, checkVocabulary, out string inputError))
            {
                return label + ": " + inputError;
            }

            return null;
        }

        private static string ValidateSkill(BridgeActionDefinition definition, string label, bool checkVocabulary)
        {
            IReadOnlyList<BridgeActionStep> steps = definition.RawSteps;
            if (steps.Count > BridgeInputVocabulary.MaxSteps)
            {
                return label + " has " + steps.Count.ToString(CultureInfo.InvariantCulture)
                    + " steps; at most " + BridgeInputVocabulary.MaxSteps.ToString(CultureInfo.InvariantCulture) + " are allowed";
            }

            double max = definition.MaxS ?? double.NaN;
            if (!definition.MaxS.HasValue || double.IsNaN(max) || double.IsInfinity(max) || max <= 0)
            {
                return label + " needs a positive max_s";
            }

            if (max > MaxSeconds)
            {
                return label + " max_s must be at most " + MaxSeconds.ToString("0", CultureInfo.InvariantCulture) + " s";
            }

            double total = 0;
            for (int i = 0; i < steps.Count; i++)
            {
                BridgeActionStep step = steps[i];
                string stepLabel = label + " step " + i.ToString(CultureInfo.InvariantCulture);
                if (step == null)
                {
                    return stepLabel + " is missing";
                }

                if (!BridgeInputVocabulary.TryValidateInputs(step.Inputs, checkVocabulary, out string inputError))
                {
                    return stepLabel + ": " + inputError;
                }

                if (double.IsNaN(step.HoldS) || double.IsInfinity(step.HoldS) || step.HoldS <= 0)
                {
                    return stepLabel + " hold_s must be positive";
                }

                if (double.IsNaN(step.WaitS) || double.IsInfinity(step.WaitS) || step.WaitS < 0)
                {
                    return stepLabel + " wait_s must not be negative";
                }

                total += step.HoldS + step.WaitS;
            }

            if (total > max + DurationEpsilon)
            {
                return label + " declares " + total.ToString("R", CultureInfo.InvariantCulture)
                    + " s of hold and wait, which exceeds max_s " + max.ToString("R", CultureInfo.InvariantCulture);
            }

            return null;
        }

        private static BridgeActionDefinition ReadDefinition(string name, JObject body)
        {
            string label = "action '" + name + "'";
            bool legacyShape = body.ContainsKey("device") || body.ContainsKey("control");
            bool modern = body.ContainsKey("inputs") || body.ContainsKey("steps") || body.ContainsKey("max_s")
                || (body.ContainsKey("default_lease_s") && !legacyShape);
            bool hasInputs = body["inputs"] != null && body["inputs"].Type != JTokenType.Null;
            bool hasSteps = body["steps"] != null && body["steps"].Type != JTokenType.Null;
            bool hasActionFields = hasInputs || (body["default_lease_s"] != null && body["default_lease_s"].Type != JTokenType.Null);
            bool hasSkillFields = hasSteps || (body["max_s"] != null && body["max_s"].Type != JTokenType.Null);
            if (hasInputs && hasSteps)
            {
                throw new InvalidDataException(label + " declares both inputs and steps; an action has inputs, a skill has steps");
            }

            var definition = new BridgeActionDefinition { Name = name };
            if (modern)
            {
                CheckMembers(body, ModernMembers, label);
                definition.Backend = ReadRequiredString(body, "backend", label);
                if (hasActionFields && hasSkillFields)
                {
                    throw new InvalidDataException(label + " declares both action and skill fields");
                }

                if (hasActionFields)
                {
                    if (!hasInputs || body["default_lease_s"] == null || body["default_lease_s"].Type == JTokenType.Null)
                    {
                        throw new InvalidDataException(label + " needs inputs and default_lease_s");
                    }

                    foreach (JToken input in ReadArray(body, "inputs", label))
                    {
                        definition.Inputs.Add(ReadInput(input, label));
                    }

                    if (definition.Inputs.Count == 0)
                    {
                        throw new InvalidDataException(label + " needs at least one input");
                    }

                    definition.DefaultLeaseS = ReadRequiredNumber(body, "default_lease_s", label);
                }
                else if (hasSkillFields)
                {
                    if (!hasSteps || body["max_s"] == null || body["max_s"].Type == JTokenType.Null)
                    {
                        throw new InvalidDataException(label + " needs steps and max_s");
                    }

                    foreach (JToken step in ReadArray(body, "steps", label))
                    {
                        definition.Steps.Add(ReadStep(step, label + " step " + definition.Steps.Count.ToString(CultureInfo.InvariantCulture)));
                    }

                    if (definition.Steps.Count == 0)
                    {
                        throw new InvalidDataException(label + " needs at least one step");
                    }

                    definition.MaxS = ReadRequiredNumber(body, "max_s", label);
                }
                else
                {
                    throw new InvalidDataException(label + " needs an action or skill declaration");
                }
            }
            else
            {
                CheckMembers(body, LegacyMembers, label);
                definition.Backend = ReadString(body, "backend", BridgeActionDefinition.InputSystemBackend, label);
                string control = ReadString(body, "control", null, label);
                if (string.IsNullOrEmpty(control))
                {
                    throw new InvalidDataException(label + " needs a control");
                }

                definition.Device = ReadString(body, "device", BridgeInputVocabulary.Keyboard, label);
                definition.Control = control;
                definition.DefaultLeaseS = ReadNumber(body, "default_lease_s", 0.25, label);
            }

            return definition;
        }

        private static BridgeInputBinding ReadInput(JToken token, string label)
        {
            if (!(token is JObject body))
            {
                throw new InvalidDataException(label + " inputs must be objects");
            }

            CheckMembers(body, InputMembers, label + " input");
            string device = ReadString(body, "device", null, label + " input");
            string control = ReadString(body, "control", null, label + " input");
            if (string.IsNullOrEmpty(device))
            {
                throw new InvalidDataException(label + " input needs a device");
            }

            if (string.IsNullOrEmpty(control))
            {
                throw new InvalidDataException(label + " input needs a control");
            }

            return new BridgeInputBinding(
                device,
                control,
                ReadNullableNumber(body, "x", label + " input"),
                ReadNullableNumber(body, "y", label + " input"));
        }

        private static BridgeActionStep ReadStep(JToken token, string label)
        {
            if (!(token is JObject body))
            {
                throw new InvalidDataException(label + " must be an object");
            }

            CheckMembers(body, StepMembers, label);
            var inputs = new List<BridgeInputBinding>();
            foreach (JToken input in ReadArray(body, "inputs", label))
            {
                inputs.Add(ReadInput(input, label));
            }

            return new BridgeActionStep(
                inputs,
                ReadRequiredNumber(body, "hold_s", label),
                ReadNumber(body, "wait_s", 0, label));
        }

        private static void CheckMembers(JObject body, string[] allowed, string label)
        {
            foreach (JProperty property in body.Properties())
            {
                if (Array.IndexOf(allowed, property.Name) < 0)
                {
                    throw new InvalidDataException(label + " has an unexpected member '" + property.Name + "'");
                }
            }
        }

        private static IEnumerable<JToken> ReadArray(JObject body, string name, string label)
        {
            JToken token = body[name];
            if (token == null || token.Type != JTokenType.Array)
            {
                throw new InvalidDataException(label + " needs '" + name + "' as an array");
            }

            return token.Children();
        }

        private static string ReadString(JObject body, string name, string fallback, string label)
        {
            JToken token = body[name];
            if (token == null || token.Type == JTokenType.Null)
            {
                return fallback;
            }

            if (token.Type != JTokenType.String)
            {
                throw new InvalidDataException(label + " field '" + name + "' must be a string");
            }

            return (string)token;
        }

        private static string ReadRequiredString(JObject body, string name, string label)
        {
            JToken token = body[name];
            if (token == null || token.Type != JTokenType.String || string.IsNullOrEmpty((string)token))
            {
                throw new InvalidDataException(label + " needs '" + name + "' as a non-empty string");
            }

            return (string)token;
        }

        private static double ReadRequiredNumber(JObject body, string name, string label)
        {
            JToken token = body[name];
            if (token == null || token.Type == JTokenType.Null)
            {
                throw new InvalidDataException(label + " needs '" + name + "'");
            }

            return ReadNumberToken(token, name, label);
        }

        private static double ReadNumber(JObject body, string name, double fallback, string label)
        {
            JToken token = body[name];
            if (token == null || token.Type == JTokenType.Null)
            {
                return fallback;
            }

            return ReadNumberToken(token, name, label);
        }

        private static double? ReadNullableNumber(JObject body, string name, string label)
        {
            JToken token = body[name];
            if (token == null || token.Type == JTokenType.Null)
            {
                return null;
            }

            return ReadNumberToken(token, name, label);
        }

        private static double ReadNumberToken(JToken token, string name, string label)
        {
            if (token.Type != JTokenType.Integer && token.Type != JTokenType.Float)
            {
                throw new InvalidDataException(label + " field '" + name + "' must be a number");
            }

            double value = Convert.ToDouble(token, CultureInfo.InvariantCulture);
            if (double.IsNaN(value) || double.IsInfinity(value))
            {
                throw new InvalidDataException(label + " field '" + name + "' must be finite");
            }

            return value;
        }
    }

    /// <summary>
    /// The input layer the lease drives. Every call is safe from any thread: device effects happen
    /// on the Unity main thread, and a call from another thread waits for them without holding a
    /// lock the main thread needs. A call made on the main thread runs inline and never waits on
    /// itself. Release verification comes from device-state reads, never from a queued request.
    /// </summary>
    public interface IBridgeInputBackend : IDisposable
    {
        string Name { get; }

        bool IsAvailable { get; }

        /// <summary>
        /// Side-effect-free check that every input of the action or skill can be applied by this
        /// backend. The lease calls it before it touches the running action.
        /// </summary>
        bool TryValidate(BridgeActionDefinition action, out string error);

        /// <summary>Inputs released by the last release that released something, until verified.</summary>
        IReadOnlyList<string> ReleasedInputs { get; }

        /// <summary>
        /// Applies all <paramref name="inputs"/> together and returns once a device read confirmed
        /// them applied, which is the acknowledgement the lease times the hold from. Nothing is
        /// applied if the run is already cancelled. A failure may leave some input applied; the
        /// caller must release.
        /// </summary>
        bool TryApplyInputs(
            BridgeInputRun run,
            int stepIndex,
            IReadOnlyList<BridgeInputBinding> inputs,
            out BridgeInputAck ack,
            out string error);

        /// <summary>
        /// Releases every input the run applied and returns once device reads confirmed them
        /// neutral. <paramref name="releaseVerified"/> is true only for that confirmation.
        /// </summary>
        bool TryReleaseInputs(BridgeInputRun run, out bool releaseVerified, out string error);

        /// <summary>Confirms from device state that everything released is neutral. May be called after a release.</summary>
        bool TryVerifyRelease(out string error);
    }
}
