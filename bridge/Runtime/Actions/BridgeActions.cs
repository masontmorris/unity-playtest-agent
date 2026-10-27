using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Playtest.Bridge.Actions
{
    /// <summary>
    /// One manifest action. The name and binding are data from the project manifest; the bridge
    /// never hardcodes a game action. v0 supports the Input System keyboard backend.
    /// </summary>
    public sealed class BridgeActionDefinition
    {
        public string Name { get; set; }

        public string Backend { get; set; } = "input_system";

        public string Device { get; set; } = "Keyboard";

        public string Control { get; set; }

        public double DefaultLeaseS { get; set; } = 0.25;
    }

    /// <summary>
    /// The project's action bindings, loaded from a manifest file. A missing file means no
    /// actions, not a default action.
    /// </summary>
    public sealed class BridgeActionCatalog
    {
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
        /// Loads <c>{"actions": {"name": {"backend", "device", "control", "default_lease_s"}}}</c>.
        /// Missing files are an empty catalog; malformed files throw instead of inventing actions.
        /// </summary>
        public static BridgeActionCatalog Load(string path)
        {
            if (string.IsNullOrEmpty(path) || !File.Exists(path))
            {
                return Empty;
            }

            string text = File.ReadAllText(path);
            JToken token;
            try
            {
                token = JToken.Parse(text);
            }
            catch (JsonException ex)
            {
                throw new InvalidDataException("action catalog is not valid JSON: " + path, ex);
            }

            if (!(token is JObject root) || root["actions"] is not JObject actions)
            {
                throw new InvalidDataException("action catalog needs an 'actions' object: " + path);
            }

            var parsed = new Dictionary<string, BridgeActionDefinition>(StringComparer.Ordinal);
            foreach (JProperty property in actions.Properties())
            {
                if (!(property.Value is JObject body))
                {
                    throw new InvalidDataException("action '" + property.Name + "' must be an object: " + path);
                }

                string backend = ReadString(body, "backend", "input_system");
                string device = ReadString(body, "device", "Keyboard");
                string control = ReadString(body, "control", null);
                if (string.IsNullOrEmpty(control))
                {
                    throw new InvalidDataException("action '" + property.Name + "' needs a control: " + path);
                }

                double defaultLease = ReadNumber(body, "default_lease_s", 0.25);
                if (defaultLease <= 0 || double.IsNaN(defaultLease) || double.IsInfinity(defaultLease))
                {
                    throw new InvalidDataException("action '" + property.Name + "' default_lease_s must be positive: " + path);
                }

                parsed[property.Name] = new BridgeActionDefinition
                {
                    Name = property.Name,
                    Backend = backend,
                    Device = device,
                    Control = control,
                    DefaultLeaseS = defaultLease,
                };
            }

            return new BridgeActionCatalog(parsed);
        }

        /// <summary>Test seam: build a catalog without a file.</summary>
        public static BridgeActionCatalog FromDefinitions(IEnumerable<BridgeActionDefinition> definitions)
        {
            var parsed = new Dictionary<string, BridgeActionDefinition>(StringComparer.Ordinal);
            foreach (BridgeActionDefinition definition in definitions ?? Enumerable.Empty<BridgeActionDefinition>())
            {
                if (definition == null || string.IsNullOrEmpty(definition.Name))
                {
                    throw new ArgumentException("every action needs a name", nameof(definitions));
                }

                if (string.IsNullOrEmpty(definition.Control))
                {
                    throw new ArgumentException("action '" + definition.Name + "' needs a control", nameof(definitions));
                }

                if (definition.DefaultLeaseS <= 0 || double.IsNaN(definition.DefaultLeaseS) || double.IsInfinity(definition.DefaultLeaseS))
                {
                    throw new ArgumentException("action '" + definition.Name + "' default lease must be positive", nameof(definitions));
                }

                parsed[definition.Name] = definition;
            }

            return new BridgeActionCatalog(parsed);
        }

        private static string ReadString(JObject body, string name, string fallback)
        {
            JToken token = body[name];
            if (token == null || token.Type == JTokenType.Null)
            {
                return fallback;
            }

            if (token.Type != JTokenType.String)
            {
                throw new InvalidDataException("action catalog field '" + name + "' must be a string");
            }

            return (string)token;
        }

        private static double ReadNumber(JObject body, string name, double fallback)
        {
            JToken token = body[name];
            if (token == null || token.Type == JTokenType.Null)
            {
                return fallback;
            }

            if (token.Type != JTokenType.Integer && token.Type != JTokenType.Float)
            {
                throw new InvalidDataException("action catalog field '" + name + "' must be a number");
            }

            return Convert.ToDouble(token, CultureInfo.InvariantCulture);
        }
    }

    /// <summary>
    /// The input layer the lease drives. Implementations must be safe to call from the
    /// transport/lease threads; the Unity implementation queues device state.
    /// </summary>
    public interface IBridgeInputBackend : IDisposable
    {
        string Name { get; }

        bool IsAvailable { get; }

        /// <summary>Side-effect-free check that the action can be applied by this backend.</summary>
        bool TryValidate(BridgeActionDefinition action, out string error);

        /// <summary>Inputs released by the last <see cref="TryRelease"/> call.</summary>
        IReadOnlyList<string> ReleasedInputs { get; }

        bool TryApply(BridgeActionDefinition action, out string error);

        bool TryRelease(out bool releaseVerified, out string error);

        /// <summary>Checks that the released inputs are no longer held. May be called after TryRelease.</summary>
        bool TryVerifyRelease(out string error);
    }
}
