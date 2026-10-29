using System;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using NUnit.Framework;

namespace Playtest.IlScan.Tests
{
    [TestFixture]
    public sealed class ProgramTests
    {
        private string _directory;

        [SetUp]
        public void CreateDirectory()
        {
            _directory = Path.Combine(Path.GetTempPath(), "upa-ilscan-tests-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(_directory);
        }

        [TearDown]
        public void DeleteDirectory() => Directory.Delete(_directory, true);

        [Test]
        public void WritesTheDocumentForValidInputs()
        {
            string input = Inputs(Fixture.Inputs());
            string output = Path.Combine(_directory, "document.json");

            Assert.That(Program.Main(new[] { "--input", input, "--output", output }), Is.EqualTo(0));

            JsonObject document = JsonNode.Parse(File.ReadAllText(output)).AsObject();
            Assert.That(document.Select(p => p.Key), Is.EqualTo(new[] { "data", "caps", "errors" }));
            Assert.That(document.ToJsonString(), Is.EqualTo(IlDiscovery.Collect(Fixture.Inputs()).ToJson().ToJsonString()));
            Assert.That(Directory.GetFiles(_directory).Select(Path.GetFileName), Is.EquivalentTo(new[] { "inputs.json", "document.json" }));
        }

        [Test]
        public void RefusesAnExistingOutput()
        {
            string input = Inputs(Fixture.Inputs());
            string output = Path.Combine(_directory, "document.json");
            File.WriteAllText(output, "kept");

            Assert.That(Program.Main(new[] { "--input", input, "--output", output }), Is.EqualTo(2));
            Assert.That(File.ReadAllText(output), Is.EqualTo("kept"));
        }

        [TestCase("[]")]
        [TestCase("{\"assemblies\": [], \"references\": []}")]
        [TestCase("{\"assemblies\": [], \"references\": [], \"complete\": true, \"extra\": 1}")]
        [TestCase("{\"assemblies\": [], \"references\": [], \"complete\": 1}")]
        [TestCase("{\"assemblies\": [{\"name\": \"A\"}], \"references\": [], \"complete\": true}")]
        [TestCase("{\"assemblies\": [{\"name\": \"\", \"path\": \"a.dll\"}], \"references\": [], \"complete\": true}")]
        [TestCase("{\"assemblies\": [{\"name\": \"A\", \"path\": \"a.dll\"}, {\"name\": \"A\", \"path\": \"b.dll\"}], \"references\": [], \"complete\": true}")]
        [TestCase("{\"assemblies\": [], \"references\": [\"\"], \"complete\": true}")]
        [TestCase("{\"assemblies\": [], \"references\": [1], \"complete\": true}")]
        [TestCase("{\"assemblies\": [], \"assemblies\": [], \"references\": [], \"complete\": true}")]
        [TestCase("not json")]
        public void RefusesMalformedInputsWithoutWritingAnything(string json)
        {
            string input = Path.Combine(_directory, "inputs.json");
            File.WriteAllText(input, json);
            string output = Path.Combine(_directory, "document.json");

            Assert.That(Program.Main(new[] { "--input", input, "--output", output }), Is.EqualTo(2));
            Assert.That(File.Exists(output), Is.False);
        }

        [TestCase()]
        [TestCase("--input")]
        [TestCase("--input", "a.json")]
        [TestCase("--output", "b.json")]
        [TestCase("--input", "a.json", "--output", "b.json", "--extra")]
        [TestCase("--input", "a.json", "--input", "c.json", "--output", "b.json")]
        public void RefusesBadArguments(params string[] args)
        {
            Assert.That(Program.Main(args), Is.EqualTo(2));
        }

        private string Inputs(IlScanInputs inputs)
        {
            var assemblies = new JsonArray();
            foreach (IlScanAssembly item in inputs.Assemblies)
                assemblies.Add(new JsonObject { ["name"] = item.Name, ["path"] = item.Path });
            var references = new JsonArray();
            foreach (string reference in inputs.References) references.Add(reference);
            string path = Path.Combine(_directory, "inputs.json");
            File.WriteAllText(path, new JsonObject
            {
                ["assemblies"] = assemblies, ["references"] = references, ["complete"] = inputs.Complete,
            }.ToJsonString());
            return path;
        }
    }
}
