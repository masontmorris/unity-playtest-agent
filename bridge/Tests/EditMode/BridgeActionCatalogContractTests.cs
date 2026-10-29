using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using NUnit.Framework;
using Playtest.Bridge.Actions;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The modern action catalog (actions with 1-3 inputs, skills with 1-8 ordered steps) and the
    /// original single-key shorthand. The Python action-catalog contract is authoritative;
    /// <see cref="ActionCatalogFixtureTests"/> runs its shared fixtures when UPA_FIXTURES is set.
    /// </summary>
    [TestFixture]
    public sealed class BridgeActionCatalogContractTests
    {
        private const string ModernCatalog = @"{""actions"": {
  ""strafe"": {""backend"": ""input_system"", ""inputs"": [
    {""device"": ""Keyboard"", ""control"": ""w""}, {""device"": ""Keyboard"", ""control"": ""a""}], ""default_lease_s"": 0.4},
  ""look"": {""backend"": ""input_system"", ""inputs"": [{""device"": ""Mouse"", ""control"": ""delta"", ""x"": 0.5, ""y"": -0.25}], ""default_lease_s"": 0.1},
  ""walk"": {""backend"": ""input_system"", ""inputs"": [
    {""device"": ""Gamepad"", ""control"": ""leftStick"", ""x"": 0.3, ""y"": 1.0},
    {""device"": ""Gamepad"", ""control"": ""leftTrigger"", ""x"": 0.5, ""y"": 0},
    {""device"": ""Gamepad"", ""control"": ""buttonSouth""}], ""default_lease_s"": 1},
  ""combo"": {""backend"": ""input_system"", ""steps"": [
    {""inputs"": [{""device"": ""Keyboard"", ""control"": ""w""}], ""hold_s"": 0.2, ""wait_s"": 0.1},
    {""inputs"": [{""device"": ""Keyboard"", ""control"": ""space""}, {""device"": ""Mouse"", ""control"": ""leftButton""}], ""hold_s"": 0.05}], ""max_s"": 1.0}
}}";

        public static IEnumerable<TestCaseData> InvalidCatalogs()
        {
            const string key = @"{""device"": ""Keyboard"", ""control"": ""w""}";
            yield return Invalid("not an object", @"{""actions"": {""x"": 3}}", "must be an object");
            yield return Invalid("no actions member", @"{""other"": {}}", "needs an 'actions' object");
            yield return Invalid("unknown root member", @"{""actions"": {}, ""schema"": 1}", "unexpected member 'schema'");
            yield return Invalid("duplicate action name", @"{""actions"": {""x"": {""control"": ""w""}, ""x"": {""control"": ""a""}}}", "not valid JSON");
            yield return Invalid("unknown member", @"{""actions"": {""x"": {""inputs"": [" + key + @"], ""default_lease_s"": 1, ""hold"": 2}}}", "unexpected member 'hold'");
            yield return Invalid("input and steps", @"{""actions"": {""x"": {""inputs"": [" + key + @"], ""steps"": [], ""default_lease_s"": 1}}}", "both inputs and steps");
            yield return Invalid("shorthand mixed with inputs", @"{""actions"": {""x"": {""control"": ""w"", ""inputs"": [" + key + @"], ""default_lease_s"": 1}}}", "unexpected member 'control'");
            yield return Invalid("skill with default lease", @"{""actions"": {""x"": {""steps"": [{""inputs"": [" + key + @"], ""hold_s"": 0.1}], ""max_s"": 1, ""default_lease_s"": 1}}}", "both action and skill fields");
            yield return Invalid("action with max_s", @"{""actions"": {""x"": {""inputs"": [" + key + @"], ""default_lease_s"": 1, ""max_s"": 1}}}", "both action and skill fields");
            yield return Invalid("action missing its lease", @"{""actions"": {""x"": {""inputs"": [" + key + @"]}}}", "needs inputs and default_lease_s");
            yield return Invalid("skill missing max_s", @"{""actions"": {""x"": {""steps"": [{""inputs"": [" + key + @"], ""hold_s"": 0.1}]}}}", "needs steps and max_s");
            yield return Invalid("zero inputs", @"{""actions"": {""x"": {""inputs"": [], ""default_lease_s"": 1}}}", "at least one input");
            yield return Invalid("four inputs", @"{""actions"": {""x"": {""inputs"": [" + key + "," + key.Replace("w", "a") + "," + key.Replace("w", "s") + "," + key.Replace("w", "d") + @"], ""default_lease_s"": 1}}}", "at most 3");
            yield return Invalid("duplicate control", @"{""actions"": {""x"": {""inputs"": [" + key + "," + key + @"], ""default_lease_s"": 1}}}", "repeats");
            yield return Invalid("zero steps", @"{""actions"": {""x"": {""steps"": [], ""max_s"": 1}}}", "at least one step");
            yield return Invalid("nine steps", @"{""actions"": {""x"": {""steps"": [" + string.Join(",", Enumerable.Repeat(@"{""inputs"": [" + key + @"], ""hold_s"": 0.01}", 9)) + @"], ""max_s"": 5}}}", "at most 8");
            yield return Invalid("zero hold", @"{""actions"": {""x"": {""steps"": [{""inputs"": [" + key + @"], ""hold_s"": 0}], ""max_s"": 1}}}", "hold_s must be positive");
            yield return Invalid("negative wait", @"{""actions"": {""x"": {""steps"": [{""inputs"": [" + key + @"], ""hold_s"": 0.1, ""wait_s"": -1}], ""max_s"": 1}}}", "wait_s must not be negative");
            yield return Invalid("declared time over max_s", @"{""actions"": {""x"": {""steps"": [{""inputs"": [" + key + @"], ""hold_s"": 0.6, ""wait_s"": 0.5}], ""max_s"": 1}}}", "exceeds max_s");
            yield return Invalid("max_s over an hour", @"{""actions"": {""x"": {""steps"": [{""inputs"": [" + key + @"], ""hold_s"": 0.1}], ""max_s"": 3600.5}}}", "at most 3600");
            yield return Invalid("lease over an hour", @"{""actions"": {""x"": {""inputs"": [" + key + @"], ""default_lease_s"": 3601}}}", "at most 3600");
            yield return Invalid("lease zero", @"{""actions"": {""x"": {""inputs"": [" + key + @"], ""default_lease_s"": 0}}}", "must be positive");
            yield return Invalid("lease as text", @"{""actions"": {""x"": {""inputs"": [" + key + @"], ""default_lease_s"": ""1""}}}", "must be a number");
            yield return Invalid("unsupported device", @"{""actions"": {""x"": {""inputs"": [{""device"": ""Touchscreen"", ""control"": ""primaryTouch""}], ""default_lease_s"": 1}}}", "unsupported input device");
            yield return Invalid("missing device", @"{""actions"": {""x"": {""inputs"": [{""control"": ""w""}], ""default_lease_s"": 1}}}", "needs a device");
            yield return Invalid("button with a value", @"{""actions"": {""x"": {""inputs"": [{""device"": ""Keyboard"", ""control"": ""w"", ""x"": 1}], ""default_lease_s"": 1}}}", "takes no x or y");
            yield return Invalid("stick missing y", @"{""actions"": {""x"": {""inputs"": [{""device"": ""Gamepad"", ""control"": ""leftStick"", ""x"": 1}], ""default_lease_s"": 1}}}", "finite x and y");
            yield return Invalid("stick beyond range", @"{""actions"": {""x"": {""inputs"": [{""device"": ""Gamepad"", ""control"": ""leftStick"", ""x"": 1.5, ""y"": 0}], ""default_lease_s"": 1}}}", "between -1 and 1");
            yield return Invalid("axis with nonzero y", @"{""actions"": {""x"": {""inputs"": [{""device"": ""Gamepad"", ""control"": ""rightTrigger"", ""x"": 1, ""y"": 0.5}], ""default_lease_s"": 1}}}", "y = 0");
            yield return Invalid("mouse scroll", @"{""actions"": {""x"": {""inputs"": [{""device"": ""Mouse"", ""control"": ""scroll"", ""x"": 0, ""y"": 1}], ""default_lease_s"": 1}}}", "scroll is not supported");
            yield return Invalid("child control path", @"{""actions"": {""x"": {""inputs"": [{""device"": ""Gamepad"", ""control"": ""dpad/up""}], ""default_lease_s"": 1}}}", "is a path");
            yield return Invalid("unknown input member", @"{""actions"": {""x"": {""inputs"": [{""device"": ""Keyboard"", ""control"": ""w"", ""remap"": ""s""}], ""default_lease_s"": 1}}}", "unexpected member 'remap'");
            yield return Invalid("other backend", @"{""actions"": {""x"": {""backend"": ""legacy"", ""inputs"": [" + key + @"], ""default_lease_s"": 1}}}", "unsupported backend");
            yield return Invalid("legacy shorthand without a control", @"{""actions"": {""x"": {""backend"": ""input_system""}}}", "needs a control");
            yield return Invalid("legacy shorthand with a touch device", @"{""actions"": {""x"": {""device"": ""Touchscreen"", ""control"": ""primaryTouch""}}}", "unsupported input device");
        }

        [Test]
        public void Parse_ReadsActionsAndSkillsExactlyAsDeclared()
        {
            BridgeActionCatalog catalog = BridgeActionCatalog.Parse(ModernCatalog, "test");

            Assert.That(catalog.Count, Is.EqualTo(4));
            Assert.That(catalog.Names, Is.EqualTo(new[] { "combo", "look", "strafe", "walk" }));

            Assert.That(catalog.TryGet("strafe", out BridgeActionDefinition strafe), Is.True);
            Assert.That(strafe.IsSkill, Is.False);
            Assert.That(strafe.DefaultLeaseS, Is.EqualTo(0.4));
            Assert.That(strafe.Inputs.Select(i => i.Control), Is.EqualTo(new[] { "w", "a" }));
            Assert.That(strafe.Inputs.All(i => i.X == null && i.Y == null), Is.True);

            Assert.That(catalog.TryGet("look", out BridgeActionDefinition look), Is.True);
            Assert.That(look.Inputs[0].Device, Is.EqualTo("Mouse"));
            Assert.That(look.Inputs[0].X, Is.EqualTo(0.5));
            Assert.That(look.Inputs[0].Y, Is.EqualTo(-0.25));

            Assert.That(catalog.TryGet("walk", out BridgeActionDefinition walk), Is.True);
            Assert.That(walk.Inputs.Select(i => i.Control), Is.EqualTo(new[] { "leftStick", "leftTrigger", "buttonSouth" }));
            Assert.That(walk.Inputs[0].X, Is.EqualTo(0.3), "a stick value is kept as declared, not normalized");
            Assert.That(walk.Inputs[0].Y, Is.EqualTo(1.0));
            Assert.That(walk.Inputs[1].Y, Is.EqualTo(0.0));

            Assert.That(catalog.TryGet("combo", out BridgeActionDefinition combo), Is.True);
            Assert.That(combo.IsSkill, Is.True);
            Assert.That(combo.MaxS, Is.EqualTo(1.0));
            Assert.That(combo.Steps.Count, Is.EqualTo(2));
            Assert.That(combo.Steps[0].HoldS, Is.EqualTo(0.2));
            Assert.That(combo.Steps[0].WaitS, Is.EqualTo(0.1));
            Assert.That(combo.Steps[1].Inputs.Select(i => i.Control), Is.EqualTo(new[] { "space", "leftButton" }));
            Assert.That(combo.Steps[1].WaitS, Is.EqualTo(0), "a missing wait_s is 0");
        }

        [Test]
        public void Parse_KeepsTheSingleKeyShorthandWorking()
        {
            BridgeActionCatalog catalog = BridgeActionCatalog.Parse(
                @"{""actions"": {""move"": {""backend"": ""input_system"", ""device"": ""Keyboard"", ""control"": ""w"", ""default_lease_s"": 0.3},
                                  ""second"": {""control"": ""space""}}}",
                "legacy");

            Assert.That(catalog.TryGet("move", out BridgeActionDefinition move), Is.True);
            Assert.That(move.Device, Is.EqualTo("Keyboard"));
            Assert.That(move.Control, Is.EqualTo("w"));
            Assert.That(move.DefaultLeaseS, Is.EqualTo(0.3).Within(0.0001));
            Assert.That(move.Inputs.Count, Is.EqualTo(1), "the shorthand is one declared input");
            Assert.That(move.Inputs[0].Control, Is.EqualTo("w"));

            Assert.That(catalog.TryGet("second", out BridgeActionDefinition second), Is.True);
            Assert.That(second.Device, Is.EqualTo("Keyboard"), "device defaults to Keyboard");
            Assert.That(second.DefaultLeaseS, Is.EqualTo(0.25), "the shorthand keeps its 0.25 s default lease");
        }

        [TestCaseSource(nameof(InvalidCatalogs))]
        public void Parse_RejectsInvalidCatalogsBeforeAnythingIsExecuted(string description, string json, string expected)
        {
            InvalidDataException ex = Assert.Throws<InvalidDataException>(() => BridgeActionCatalog.Parse(json, "invalid"), description);
            Assert.That(ex.Message, Does.Contain(expected), description);
        }

        [Test]
        public void ModernCatalogRequiresAnExplicitBackend()
        {
            const string json = @"{""actions"": {""x"": {""inputs"": [{""device"": ""Keyboard"", ""control"": ""w""}], ""default_lease_s"": 1}}}";
            InvalidDataException error = Assert.Throws<InvalidDataException>(() => BridgeActionCatalog.Parse(json, "missing backend"));
            Assert.That(error.Message, Does.Contain("needs 'backend'"));
        }

        [Test]
        public void Skill_TotalDeclaredTimeMayEqualMaxS()
        {
            const string json = @"{""actions"": {""x"": {""steps"": [
                {""inputs"": [{""device"": ""Keyboard"", ""control"": ""w""}], ""hold_s"": 0.1, ""wait_s"": 0.05},
                {""inputs"": [{""device"": ""Keyboard"", ""control"": ""a""}], ""hold_s"": 0.2}], ""max_s"": 0.35}}}";
            string withBackend = json.Replace("{\"steps\"", "{\"backend\": \"input_system\", \"steps\"");
            Assert.DoesNotThrow(() => BridgeActionCatalog.Parse(withBackend, "boundary"));
            Assert.Throws<InvalidDataException>(() => BridgeActionCatalog.Parse(withBackend.Replace("0.35", "0.349"), "over"));
        }

        [Test]
        public void FromDefinitions_ValidatesStructureButLeavesDeviceNamesToTheBackend()
        {
            var placeholder = new BridgeActionDefinition { Name = "placeholder", Device = "Device", Control = "control-a", DefaultLeaseS = 0.05 };

            BridgeActionCatalog catalog = BridgeActionCatalog.FromDefinitions(new[] { placeholder });

            Assert.That(catalog.TryGet("placeholder", out BridgeActionDefinition stored), Is.True);
            Assert.That(stored.Device, Is.EqualTo("Device"));
            Assert.That(stored.Control, Is.EqualTo("control-a"));
        }

        [Test]
        public void FromDefinitions_RejectsDefinitionsThatCannotBeExecuted()
        {
            BridgeInputBinding w = InputRig.Key("w");
            Func<BridgeActionDefinition>[] makers =
            {
                () => new BridgeActionDefinition { Name = "no_inputs" },
                () => InputRig.Action("four", 1, w, InputRig.Key("a"), InputRig.Key("s"), InputRig.Key("d")),
                () => InputRig.Action("zero_lease", 0, w),
                () => InputRig.Action("long_lease", 3601, w),
                () => InputRig.Action("with_max", 1, w).Also(d => d.MaxS = 1),
                () => InputRig.Skill("nine", 5, Enumerable.Repeat(new BridgeActionStep(new[] { w }, 0.01), 9).ToArray()),
                () => InputRig.Skill("zero_hold", 5, new BridgeActionStep(new[] { w }, 0)),
                () => InputRig.Skill("over", 0.5, new BridgeActionStep(new[] { w }, 0.4, 0.2)),
                () => InputRig.Skill("no_bound", 1, new BridgeActionStep(new[] { w }, 0.4)).Also(d => d.MaxS = null),
                () => InputRig.Skill("empty_step", 1, new BridgeActionStep(new BridgeInputBinding[0], 0.4)),
                () => new BridgeActionDefinition { Name = "mixed", Control = "w" }.Also(d => d.Inputs.Add(w)),
            };

            foreach (Func<BridgeActionDefinition> make in makers)
            {
                BridgeActionDefinition definition = make();
                Assert.Throws<ArgumentException>(
                    () => BridgeActionCatalog.FromDefinitions(new[] { definition }),
                    definition.Name);
            }
        }

        [Test]
        public void Catalog_StoresImmutableCopies()
        {
            BridgeActionDefinition source = InputRig.Action("held", 0.5, InputRig.Key("w"));
            BridgeActionCatalog catalog = BridgeActionCatalog.FromDefinitions(new[] { source });

            source.Control = "s";
            source.DefaultLeaseS = 9;
            source.Inputs.Clear();

            Assert.That(catalog.TryGet("held", out BridgeActionDefinition stored), Is.True);
            Assert.That(stored.IsFrozen, Is.True);
            Assert.That(stored.Control, Is.EqualTo("w"), "a later change to the source must not change a validated action");
            Assert.That(stored.DefaultLeaseS, Is.EqualTo(0.5));
            Assert.Throws<InvalidOperationException>(() => stored.Control = "x");
            Assert.Throws<InvalidOperationException>(() => stored.DefaultLeaseS = 1);
            stored.Inputs.Add(InputRig.Key("z"));
            Assert.That(catalog.TryGet("held", out BridgeActionDefinition again), Is.True);
            Assert.That(again.Inputs.Count, Is.EqualTo(1), "the returned list is a copy");
        }

        [Test]
        public void Load_MissingFileIsEmptyAndMalformedJsonThrows()
        {
            string missing = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N") + ".json");
            Assert.That(BridgeActionCatalog.Load(missing).Count, Is.Zero);

            string path = Path.Combine(Path.GetTempPath(), "upa-catalog-" + Guid.NewGuid().ToString("N") + ".json");
            File.WriteAllText(path, "{ not json");
            try
            {
                Assert.Throws<InvalidDataException>(() => BridgeActionCatalog.Load(path));
                File.WriteAllText(path, ModernCatalog);
                Assert.That(BridgeActionCatalog.Load(path).Count, Is.EqualTo(4));
            }
            finally
            {
                File.Delete(path);
            }
        }

        private static TestCaseData Invalid(string description, string json, string expected)
        {
            if (!json.Contains("\"backend\"", StringComparison.Ordinal)
                && (json.Contains("\"inputs\"", StringComparison.Ordinal) || json.Contains("\"steps\"", StringComparison.Ordinal)))
            {
                int inputs = json.IndexOf("\"inputs\":", StringComparison.Ordinal);
                int steps = json.IndexOf("\"steps\":", StringComparison.Ordinal);
                int insert = inputs < 0 ? steps : steps < 0 ? inputs : Math.Min(inputs, steps);
                json = json.Insert(insert, "\"backend\":\"input_system\",");
            }
            return new TestCaseData(description, json, expected).SetName("invalid catalog: " + description);
        }
    }

    internal static class DefinitionExtensions
    {
        public static BridgeActionDefinition Also(this BridgeActionDefinition definition, Action<BridgeActionDefinition> change)
        {
            change(definition);
            return definition;
        }
    }

    /// <summary>
    /// Runs the shared <c>fixtures/contracts/action_catalog</c> files through the C# loader, the
    /// same way the other contracts are checked. The fixture root is supplied through UPA_FIXTURES.
    /// </summary>
    [TestFixture]
    public sealed class ActionCatalogFixtureTests
    {
        public static IEnumerable<TestCaseData> Fixtures()
        {
            string directory = Path.Combine(TestPaths.Contracts, "action_catalog");
            if (!Directory.Exists(directory))
            {
                yield return new TestCaseData(null).SetName("action_catalog fixtures are not present yet");
                yield break;
            }

            string[] files = Directory.GetFiles(directory, "*.json");
            Array.Sort(files, StringComparer.Ordinal);
            foreach (string file in files)
            {
                yield return new TestCaseData(file).SetName("action_catalog/" + Path.GetFileNameWithoutExtension(file));
            }
        }

        [TestCaseSource(nameof(Fixtures))]
        public void Fixture_MatchesItsLabel(string path)
        {
            if (path == null)
            {
                Assert.Inconclusive("fixtures/contracts/action_catalog does not exist in this checkout: C# parity with the Python action_catalog contract is pending");
            }

            string name = Path.GetFileNameWithoutExtension(path);
            string json = File.ReadAllText(path);
            if (name.StartsWith("valid-", StringComparison.Ordinal))
            {
                Assert.DoesNotThrow(() => BridgeActionCatalog.Parse(json, path), path);
            }
            else if (name.StartsWith("invalid-", StringComparison.Ordinal))
            {
                Assert.Throws<InvalidDataException>(() => BridgeActionCatalog.Parse(json, path), path);
            }
            else
            {
                Assert.Fail("unlabelled fixture (expected valid-*.json or invalid-*.json): " + path);
            }
        }
    }
}
