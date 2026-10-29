using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Which catalog a bench-style host registers. A manifest session serves exactly the catalog the manifest
    /// exports and fails closed when it cannot; every other module keeps the keyboard bench catalog. The
    /// exported catalog used here is <c>fixtures/contracts/action_catalog/valid-session-export.json</c>, which
    /// <c>tests/test_runner_catalog.py</c> keeps byte-equal to what <c>playtest-export-catalog</c> writes for a
    /// manifest holding one proven declaration of every mode. These tests do not start Unity: the host's own
    /// scene, bridge and process handling are covered by the PlayMode host, which is not run here.
    /// </summary>
    [TestFixture]
    public sealed class BridgeSessionCatalogTests
    {
        private static readonly string[] Exported =
        {
            "device_chord", "dpad_value", "key_chord", "key_hold", "key_sequence", "mouse_button", "pad_button",
            "pointer_impulse", "pointer_sequence", "stick_value", "trigger_value",
        };

        // The SHA-256 of the committed golden's bytes; tests/test_runner_catalog.py pins the same literal, and
        // PowerShell's Get-FileHash computes it too, so the pin means the same thing in all three languages.
        private const string GoldenSha256 = "6405eb6c3f96a642b475691827a4f1a061b33981e980eea146ae0be5db61dc29";

        private string _directory;

        [SetUp]
        public void SetUp()
        {
            _directory = Path.Combine(Path.GetTempPath(), "upa-session-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(_directory);
        }

        [TearDown]
        public void TearDown()
        {
            if (Directory.Exists(_directory))
            {
                Directory.Delete(_directory, true);
            }
        }

        private static string Golden
        {
            get { return Path.Combine(TestPaths.Contracts, "action_catalog", "valid-session-export.json"); }
        }

        private static string Digest(string path)
        {
            return BridgeSessionCatalog.Sha256Hex(File.ReadAllBytes(path));
        }

        private sealed class Calls
        {
            public int Count;
        }

        /// <summary>The keyboard bench catalog a host builds for its own modules; counts how often it is asked for.</summary>
        private static Func<BridgeActionCatalog> Keyboard(Calls calls)
        {
            return () =>
            {
                calls.Count++;
                return BridgeActionCatalog.FromDefinitions(new[]
                {
                    InputRig.Action("preflight_key_w", 0.4, InputRig.Key("w")),
                    InputRig.Action("preflight_key_a", 0.4, InputRig.Key("a")),
                });
            };
        }

        private string Write(string name, string text)
        {
            string path = Path.Combine(_directory, name);
            File.WriteAllText(path, text, new UTF8Encoding(false));
            return path;
        }

        private BridgeActionDefinition Declaration(string name)
        {
            BridgeActionCatalog catalog = BridgeSessionCatalog.LoadExported(Golden, Digest(Golden));
            Assert.That(catalog.TryGet(name, out BridgeActionDefinition definition), Is.True, name);
            return definition;
        }

        // --- the original bench path ----------------------------------------------------------------

        [Test]
        public void Select_EveryOtherModuleKeepsTheKeyboardBenchCatalogAndNeverReadsTheSessionVariables()
        {
            var calls = new Calls();
            string[] modules = { "playtest.bench.live", "playtest.bench.record", "playtest.bench.load", "playtest.runner.cli.other", "", null };

            foreach (string module in modules)
            {
                // The session variables hold nothing usable; a legacy module must not consult them.
                BridgeActionCatalog catalog = BridgeSessionCatalog.Select(module, "no/such/file.json", "not a digest", Keyboard(calls));
                Assert.That(catalog.Names, Is.EqualTo(new[] { "preflight_key_a", "preflight_key_w" }), module);
            }

            Assert.That(calls.Count, Is.EqualTo(modules.Length), "the keyboard catalog is built once per legacy host");
            Assert.That(BridgeSessionCatalog.IsSessionModule("playtest.bench.live"), Is.False);
            Assert.That(BridgeSessionCatalog.IsSessionModule("Playtest.Runner.Cli"), Is.False, "module names are exact");
        }

        [Test]
        public void Select_RequiresACatalogProducerForTheKeyboardBenchPath()
        {
            Assert.Throws<ArgumentNullException>(() => BridgeSessionCatalog.Select("playtest.bench.live", null, null, null));
            Assert.Throws<ArgumentNullException>(() => BridgeSessionCatalog.Select(BridgeSessionCatalog.SessionModule, Golden, Digest(Golden), null));
        }

        // --- a manifest session ---------------------------------------------------------------------

        [Test]
        public void TheCommittedExportHasTheDigestEveryLanguageComputes()
        {
            Assert.That(Digest(Golden), Is.EqualTo(GoldenSha256));
            Assert.That(Array.IndexOf(File.ReadAllBytes(Golden), (byte)'\r'), Is.EqualTo(-1), "LF on every checkout");
        }

        [Test]
        public void Select_ASessionLoadsExactlyTheExportedCatalogAndNeverBuildsTheKeyboardCatalog()
        {
            var calls = new Calls();

            BridgeActionCatalog catalog = BridgeSessionCatalog.Select(
                BridgeSessionCatalog.SessionModule, Golden, Digest(Golden), Keyboard(calls));

            Assert.That(calls.Count, Is.Zero, "a session never gets the keyboard bench catalog");
            Assert.That(catalog.Names, Is.EqualTo(Exported), "every supported declaration, under its own name, and nothing else");
            Assert.That(catalog.TryGet("preflight_key_w", out BridgeActionDefinition _), Is.False);
            Assert.That(BridgeSessionCatalog.IsSessionModule("playtest.runner.cli"), Is.True);
            Assert.That(BridgeSessionCatalog.LoadExported(Golden, Digest(Golden).ToUpperInvariant()).Count, Is.EqualTo(Exported.Length),
                "a digest is hex whatever its case");
        }

        [Test]
        public void Select_TheSessionCatalogKeepsEveryDeclaredInputValueHoldWaitAndBound()
        {
            BridgeActionCatalog catalog = BridgeSessionCatalog.LoadExported(Golden, Digest(Golden));
            var actions = new Dictionary<string, (string[] inputs, double lease)>
            {
                ["key_hold"] = (new[] { "Keyboard.w" }, 0.2),
                ["key_chord"] = (new[] { "Keyboard.w", "Keyboard.a" }, 0.2),
                ["device_chord"] = (new[] { "Keyboard.w", "Gamepad.rightTrigger(1,0)", "Mouse.leftButton" }, 0.3),
                ["pointer_impulse"] = (new[] { "Mouse.delta(0.5,-0.25)" }, 0.1),
                ["stick_value"] = (new[] { "Gamepad.leftStick(0.3,1)" }, 0.5),
                ["trigger_value"] = (new[] { "Gamepad.leftTrigger(0.5,0)" }, 0.3),
                ["dpad_value"] = (new[] { "Gamepad.dpad(0,1)" }, 0.2),
                ["pad_button"] = (new[] { "Gamepad.buttonSouth" }, 0.1),
                ["mouse_button"] = (new[] { "Mouse.leftButton" }, 0.1),
            };

            foreach (KeyValuePair<string, (string[] inputs, double lease)> expected in actions)
            {
                Assert.That(catalog.TryGet(expected.Key, out BridgeActionDefinition action), Is.True, expected.Key);
                Assert.That(action.IsSkill, Is.False, expected.Key);
                Assert.That(action.Inputs.Select(i => i.ToString()), Is.EqualTo(expected.Value.inputs), expected.Key + ": every input, in order, with its value");
                Assert.That(action.DefaultLeaseS, Is.EqualTo(expected.Value.lease), expected.Key);
                Assert.That(action.MaxS, Is.Null, expected.Key);
            }

            Assert.That(catalog.TryGet("key_sequence", out BridgeActionDefinition sequence), Is.True);
            Assert.That(sequence.IsSkill, Is.True);
            Assert.That(sequence.MaxS, Is.EqualTo(1.0));
            Assert.That(sequence.Steps.Select(s => string.Join("+", s.Inputs.Select(i => i.ToString()))), Is.EqualTo(new[] { "Keyboard.w", "Keyboard.a+Keyboard.space" }));
            Assert.That(sequence.Steps.Select(s => s.HoldS), Is.EqualTo(new[] { 0.2, 0.2 }));
            Assert.That(sequence.Steps.Select(s => s.WaitS), Is.EqualTo(new[] { 0.1, 0.0 }));

            Assert.That(catalog.TryGet("pointer_sequence", out BridgeActionDefinition pointer), Is.True);
            Assert.That(pointer.MaxS, Is.EqualTo(0.5));
            Assert.That(pointer.Steps.Select(s => string.Join("+", s.Inputs.Select(i => i.ToString()))),
                Is.EqualTo(new[] { "Mouse.delta(0.5,-0.25)+Mouse.leftButton", "Gamepad.leftStick(0,1)" }));
            Assert.That(pointer.Steps.Select(s => s.HoldS), Is.EqualTo(new[] { 0.05, 0.2 }));
            Assert.That(pointer.Steps.Select(s => s.WaitS), Is.EqualTo(new[] { 0.1, 0.0 }));
        }

        public static IEnumerable<TestCaseData> Refusals()
        {
            const string key = @"{""device"": ""Keyboard"", ""control"": ""w"", ""x"": null, ""y"": null}";
            yield return Refusal("no path", null, "a".PadLeft(64, 'a'), typeof(InvalidOperationException), "UPA_SESSION_CATALOG");
            yield return Refusal("empty path", "", "a".PadLeft(64, 'a'), typeof(InvalidOperationException), "UPA_SESSION_CATALOG");
            yield return Refusal("missing file", "absent.json", "a".PadLeft(64, 'a'), typeof(FileNotFoundException), "does not exist");
            yield return Text("malformed JSON", "{ not json", typeof(InvalidDataException), "not valid JSON");
            yield return Text("not an object", "[]", typeof(InvalidDataException), "needs an 'actions' object");
            yield return Text("no actions member", @"{""other"": {}}", typeof(InvalidDataException), "needs an 'actions' object");
            yield return Text("empty catalog", @"{""actions"": {}}", typeof(InvalidDataException), "declares no action or skill");
            yield return Text("unknown member", @"{""actions"": {""move"": {""backend"": ""input_system"", ""inputs"": [" + key + @"], ""default_lease_s"": 0.2, ""hold"": 1}}}",
                typeof(InvalidDataException), "unexpected member 'hold'");
            yield return Text("unsupported device", @"{""actions"": {""move"": {""backend"": ""input_system"", ""inputs"": [{""device"": ""Touchscreen"", ""control"": ""primaryTouch""}], ""default_lease_s"": 0.2}}}",
                typeof(InvalidDataException), "unsupported input device");
            yield return Text("button with a value", @"{""actions"": {""move"": {""backend"": ""input_system"", ""inputs"": [{""device"": ""Keyboard"", ""control"": ""w"", ""x"": 1, ""y"": 0}], ""default_lease_s"": 0.2}}}",
                typeof(InvalidDataException), "takes no x or y");
            yield return Text("skill over its bound", @"{""actions"": {""sequence"": {""backend"": ""input_system"", ""steps"": [{""inputs"": [" + key + @"], ""hold_s"": 0.6, ""wait_s"": 0.5}], ""max_s"": 1}}}",
                typeof(InvalidDataException), "exceeds max_s");
        }

        private static TestCaseData Refusal(string description, string path, string sha, Type exception, string message)
        {
            return new TestCaseData(description, path, sha, null, exception, message).SetName("session refused: " + description);
        }

        private static TestCaseData Text(string description, string text, Type exception, string message)
        {
            return new TestCaseData(description, "catalog.json", null, text, exception, message).SetName("session refused: " + description);
        }

        [TestCaseSource(nameof(Refusals))]
        public void Select_ASessionFailsClosedAndNeverFallsBackToTheKeyboardCatalog(
            string description, string path, string sha, string text, Type exception, string message)
        {
            var calls = new Calls();
            if (text != null)
            {
                path = Write(path, text);
                sha = Digest(path);                       // the pin is right: the content itself is what is wrong
            }
            else if (path != null && path.Length > 0)
            {
                path = Path.Combine(_directory, path);
            }

            Exception thrown = Assert.Throws(exception, () => BridgeSessionCatalog.Select(
                BridgeSessionCatalog.SessionModule, path, sha, Keyboard(calls)), description);
            Assert.That(thrown.Message, Does.Contain(message), description);
            Assert.That(calls.Count, Is.Zero, description + ": a failed session must not receive the keyboard bench catalog");
        }

        [TestCase(null)]
        [TestCase("")]
        [TestCase("abc123")]
        [TestCase("zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")]
        public void Select_ASessionRequiresTheDigestPin(string pin)
        {
            var calls = new Calls();
            InvalidOperationException thrown = Assert.Throws<InvalidOperationException>(() => BridgeSessionCatalog.Select(
                BridgeSessionCatalog.SessionModule, Golden, pin, Keyboard(calls)));
            Assert.That(thrown.Message, Does.Contain("UPA_SESSION_CATALOG_SHA256"));
            Assert.That(calls.Count, Is.Zero);
        }

        [Test]
        public void Select_AFileAlteredAfterExportIsRefusedEvenWhenItIsStillAValidCatalog()
        {
            string original = File.ReadAllText(Golden);
            string pin = Digest(Golden);
            string altered = Write("altered.json", original.Replace("\"default_lease_s\": 0.2", "\"default_lease_s\": 0.25"));
            Assert.That(altered, Is.Not.EqualTo(original), "the change is real");
            Assert.That(BridgeActionCatalog.Parse(File.ReadAllText(altered), altered).Count, Is.EqualTo(Exported.Length), "it still parses");

            var calls = new Calls();
            InvalidDataException thrown = Assert.Throws<InvalidDataException>(() => BridgeSessionCatalog.Select(
                BridgeSessionCatalog.SessionModule, altered, pin, Keyboard(calls)));
            Assert.That(thrown.Message, Does.Contain("not the file that was exported"));
            Assert.That(calls.Count, Is.Zero);

            string spaced = Write("spaced.json", original + "\n");
            Assert.Throws<InvalidDataException>(() => BridgeSessionCatalog.LoadExported(spaced, pin), "even whitespace changes the digest");
        }

        [Test]
        public void Select_InvalidUtf8WithItsOwnDigestPinnedIsRefused()
        {
            string path = Path.Combine(_directory, "bytes.json");
            File.WriteAllBytes(path, new byte[] { 0x7b, 0xff, 0xfe, 0x7d });
            var calls = new Calls();
            InvalidDataException thrown = Assert.Throws<InvalidDataException>(() => BridgeSessionCatalog.Select(
                BridgeSessionCatalog.SessionModule, path, Digest(path), Keyboard(calls)));
            Assert.That(thrown.Message, Does.Contain("not valid UTF-8"));
            Assert.That(calls.Count, Is.Zero);
        }

        [Test]
        public void LoadExported_AMissingFileIsNotAnEmptyCatalogUnlikeTheOptionalLoader()
        {
            string missing = Path.Combine(_directory, "absent.json");
            Assert.That(BridgeActionCatalog.Load(missing).Count, Is.Zero, "the project's own optional catalog may be absent");
            Assert.Throws<FileNotFoundException>(() => BridgeSessionCatalog.LoadExported(missing, Digest(Golden)));
        }

        // --- the exported declarations run on the bridge exactly as declared -------------------------------

        [TestCaseSource(nameof(ExportedNames))]
        public void TheBridgeRunsEveryExportedDeclarationExactlyAsDeclared(string name)
        {
            BridgeActionDefinition definition = Declaration(name);
            using (var rig = new InputRig(operationTimeoutMs: 400))
            {
                Assert.That(rig.Lease.TryStart(InputRig.Request("run-" + name), definition, 1, out string error), Is.True, error);
                RunToCompletion(rig);

                ExecutionRecordMessage done = rig.Last(BridgeExecStatus.Completed);
                Assert.That(done, Is.Not.Null, name);
                Assert.That(done.ReleaseVerified, Is.True, name);
                Assert.That(done.AppliedInputs.Select(Describe), Is.EqualTo(Declared(definition)),
                    name + ": one receipt per declared input, in order, with its declared value");
                foreach (AppliedInputReceipt receipt in done.AppliedInputs.Where(r => r.Device == "Mouse" && r.Control == "delta"))
                {
                    Assert.That(receipt.AppliedDeltaX, Is.EqualTo(receipt.X * BridgeInputVocabulary.MouseDeltaPixelsPerUnit).Within(1e-9), name);
                    Assert.That(receipt.AppliedDeltaY, Is.EqualTo(receipt.Y * BridgeInputVocabulary.MouseDeltaPixelsPerUnit).Within(1e-9), name);
                }

                Assert.That(rig.Surface.Keys, Is.Empty, name + ": everything was released");
                Assert.That(rig.Surface.GamepadButtons, Is.Empty, name);
            }
        }

        [Test]
        public void TheBridgeRefusesAReplacedSkillBoundAndAcceptsExactlyMaxS()
        {
            BridgeActionDefinition sequence = Declaration("key_sequence");
            using (var rig = new InputRig(operationTimeoutMs: 400))
            {
                Assert.That(rig.Lease.TryStart(InputRig.Request("replaced", 0.2), sequence, 1, out string error), Is.False,
                    "a default action lease must not replace a skill's own bound");
                Assert.That(error, Does.Contain("absent or exactly max_s"));
                Assert.That(rig.Surface.Log, Is.Empty, "nothing was applied");
                Assert.That(rig.Lease.TryStart(InputRig.Request("exact", 1.0), sequence, 1, out error), Is.True, error);
            }

            using (var rig = new InputRig(operationTimeoutMs: 400))
            {
                Assert.That(rig.Lease.TryStart(InputRig.Request("absent"), sequence, 1, out string error), Is.True, error);
            }
        }

        public static IEnumerable<TestCaseData> ExportedNames()
        {
            foreach (string name in Exported)
            {
                yield return new TestCaseData(name).SetName("exported declaration runs as declared: " + name);
            }
        }

        /// <summary>Fires the earliest timer, moving the wall clock by its delay, until the run has ended.</summary>
        private static void RunToCompletion(InputRig rig)
        {
            for (int guard = 0; guard < 64 && rig.Lease.IsActive; guard++)
            {
                IReadOnlyList<TimeSpan> pending = rig.Scheduler.PendingDelays;
                Assert.That(pending.Count, Is.GreaterThan(0), "an active run has a timer pending");
                TimeSpan next = pending.Min();
                rig.Clock.Advance(next.TotalSeconds);
                rig.Scheduler.FireWhere(delay => delay == next);
            }

            Assert.That(rig.Lease.IsActive, Is.False, "the run did not finish");
        }

        private static string Describe(AppliedInputReceipt receipt)
        {
            return receipt.StepIndex.ToString(CultureInfo.InvariantCulture) + ":" + receipt.Device + "." + receipt.Control + Values(receipt.X, receipt.Y);
        }

        private static string[] Declared(BridgeActionDefinition definition)
        {
            IReadOnlyList<IReadOnlyList<BridgeInputBinding>> sets = definition.InputSets();
            var lines = new List<string>();
            for (int step = 0; step < sets.Count; step++)
            {
                foreach (BridgeInputBinding input in sets[step])
                {
                    lines.Add(step.ToString(CultureInfo.InvariantCulture) + ":" + input.Device + "." + input.Control + Values(input.X, input.Y));
                }
            }

            return lines.ToArray();
        }

        private static string Values(double? x, double? y)
        {
            if (!x.HasValue && !y.HasValue)
            {
                return string.Empty;
            }

            return "(" + (x.HasValue ? x.Value.ToString("R", CultureInfo.InvariantCulture) : "null") + ","
                + (y.HasValue ? y.Value.ToString("R", CultureInfo.InvariantCulture) : "null") + ")";
        }
    }
}
