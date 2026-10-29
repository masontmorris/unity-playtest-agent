using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using NUnit.Framework;

namespace Playtest.IlScan.Tests
{
    [TestFixture]
    public sealed class IlDiscoveryTests
    {
        private const string Fixtures = "Playtest.IlScan.Fixtures:Playtest.IlScan.Fixtures.";
        private static readonly string[] Caps = { "il_types", "il_fields", "il_methods", "il_writes", "il_issues" };
        private static readonly string[] Sections = { "il_write_sets", "il_unlisted_writes", "il_issues", "il_scope", "il_complete" };

        [Test]
        public void StoresInsideAGenericTypeNameTheFieldsItDeclares()
        {
            JsonObject data = Data(IlDiscovery.Collect(Fixture.Inputs()));

            Assert.That(Writers(data, Fixtures + "GenericOwner`1:Value"), Is.EqualTo(new[] { Fixtures + "GenericOwner`1:Set(T)" }));
            Assert.That(Writers(data, Fixtures + "GenericOwner`1:Shared"), Is.EqualTo(new[] { Fixtures + "GenericOwner`1:SetShared(T)" }));
            Assert.That(Writers(data, Fixtures + "GenericOwner`1:Flag"), Is.EqualTo(new[] { Fixtures + "GenericOwner`1:Set(T)" }));
            Assert.That(Writers(data, Fixtures + "SingletonBase`1:s_instance"), Is.EqualTo(new[] { Fixtures + "SingletonBase`1:Register(T)" }));
            Assert.That(Writers(data, Fixtures + "Outer`1+Inner:Item"), Is.EqualTo(new[] { Fixtures + "Outer`1+Inner:Put(T)" }));
            Assert.That(Writers(data, Fixtures + "Outer`1+Pair`1:First"), Is.EqualTo(new[] { Fixtures + "Outer`1+Pair`1:Assign(T,U)" }));
        }

        [Test]
        public void OpenAndClosedInstantiationsKeepTheirReflectionNames()
        {
            JsonObject data = Data(IlDiscovery.Collect(Fixture.Inputs()));
            List<string> unlisted = data["il_unlisted_writes"].AsArray().Select(r => (string)r["field_id"]).ToList();

            // An instantiation over another type's or a method's parameters has no full name, so reflection
            // names it by its simple name. The Editor's ids had the same form.
            Assert.That(unlisted, Does.Contain("Playtest.IlScan.Fixtures:BaseHolder`1:Held"));
            Assert.That(unlisted, Does.Contain("Playtest.IlScan.Fixtures:GenericOwner`1:Value"));
            Assert.That(unlisted.Count(id => id.StartsWith(Fixtures + "GenericOwner`1[[System.Int32, ") && id.EndsWith("]]:Value")),
                Is.EqualTo(1));
            Assert.That(unlisted.Count(id => id.StartsWith(Fixtures + "Outer`1+Pair`1[[System.String, ") && id.EndsWith("]]:Second")),
                Is.EqualTo(1));
        }

        [Test]
        public void AMissingReferenceIsRecordedAsUnresolvedNeverAsNoWrite()
        {
            InspectionDocument document = IlDiscovery.Collect(Fixture.Inputs(withDependency: false));
            JsonObject data = Data(document);

            Assert.That((bool)data["il_complete"], Is.False);
            List<JsonNode> unresolved = data["il_issues"].AsArray()
                .Where(i => (string)i["kind"] == "UnresolvedFieldToken").ToList();
            Assert.That(unresolved, Has.Count.EqualTo(5));
            Assert.That(unresolved.All(i => i["field_id"] == null && i["il_offset"] != null), Is.True);
            Assert.That(unresolved.All(i => ((string)i["reason"]).Contains("could not be resolved")), Is.True);
            Assert.That(unresolved.Count(i => (string)i["method"] == Fixtures + "ExternalWriter:Reset()"), Is.EqualTo(1));
            Assert.That(Writers(data, Fixtures + "ExternalWriter:Local"), Has.Count.EqualTo(1),
                "writes that resolve are still recorded");
            Assert.That(document.Caps["il_writes"].Observed, Is.Null);
            Assert.That(document.Caps["il_issues"].Observed, Is.Null);

            // A method whose parameter types cannot be resolved has no id. It is an error, never a guess.
            Assert.That(document.Errors, Is.EquivalentTo(new[]
            {
                "il_method:" + Fixtures + "ExternalWriter:Nested: FileNotFoundException",
                "il_method:" + Fixtures + "ExternalWriter:Write: FileNotFoundException",
            }));
            Assert.That(unresolved.Count(i => i["method"] == null), Is.EqualTo(4));
        }

        [Test]
        public void ABodyWithoutIlIsAnIssueNeverAnEmptyWriteSet()
        {
            JsonObject data = Data(IlDiscovery.Collect(Fixture.Inputs()));
            List<string> missing = data["il_issues"].AsArray().Where(i => (string)i["kind"] == "MissingMethodBody")
                .Select(i => (string)i["method"]).ToList();

            Assert.That(missing, Does.Contain(Fixtures + "ScanTargets+MissingBodyTarget:NativeCall()"));
            Assert.That(missing, Does.Contain(Fixtures + "Notify:Invoke(System.Int32)"));
            Assert.That((bool)data["il_complete"], Is.False);
        }

        [Test]
        public void AnIncompleteSelectionLeavesEveryCountUnknown()
        {
            InspectionDocument document = IlDiscovery.Collect(Fixture.Inputs(complete: false));

            Assert.That((bool)Data(document)["il_complete"], Is.False);
            foreach (string name in Caps) Assert.That(document.Caps[name].Observed, Is.Null, name);
            Assert.That(document.Caps["il_types"].Truncated, Is.Null);
            Assert.That(document.Caps["il_types"].Returned, Is.GreaterThan(0), "what was scanned is still returned");
        }

        [Test]
        public void AnUnreadableAssemblyIsAnErrorAndTheRestIsStillScanned()
        {
            string missing = Path.Combine(Path.GetTempPath(), "upa-ilscan-missing-" + System.Guid.NewGuid().ToString("N") + ".dll");
            var inputs = new IlScanInputs(new[]
            {
                new IlScanAssembly("Absent", missing),
                new IlScanAssembly(Fixture.AssemblyName, Fixture.AssemblyPath),
            }, Fixture.References(), true);

            InspectionDocument document = IlDiscovery.Collect(inputs);

            Assert.That(document.Errors, Is.EqualTo(new[] { "il_assembly:Absent: FileNotFoundException" }));
            Assert.That((bool)Data(document)["il_complete"], Is.False);
            Assert.That(document.Caps["il_types"].Observed, Is.Null);
            Assert.That(Writers(Data(document), Fixtures + "ScanTargets+DirectWriteTarget:InstanceValue"), Has.Count.EqualTo(1));
        }

        [Test]
        public void AFileHoldingAnotherAssemblyIsAnError()
        {
            var inputs = new IlScanInputs(new[] { new IlScanAssembly("Renamed", Fixture.AssemblyPath) }, Fixture.References(), true);

            InspectionDocument document = IlDiscovery.Collect(inputs);

            Assert.That(document.Errors, Is.EqualTo(new[] { "il_assembly:Renamed: the file holds a different assembly" }));
            Assert.That((bool)Data(document)["il_complete"], Is.False);
        }

        [Test]
        public void WithoutACoreAssemblyEverySectionIsStillPresentAndUnknown()
        {
            var inputs = new IlScanInputs(new[] { new IlScanAssembly(Fixture.AssemblyName, Fixture.AssemblyPath) },
                new[] { Fixture.AssemblyPath }, true);

            InspectionDocument document = IlDiscovery.Collect(inputs);

            Assert.That(document.Errors, Is.Not.Empty);
            Assert.That(document.Errors.All(e => e.StartsWith("il_")), Is.True, string.Join("; ", document.Errors));
            Assert.That(document.Caps.Keys, Is.EqualTo(Caps));
            Assert.That(Data(document).Select(p => p.Key), Is.EqualTo(Sections));
            Assert.That((bool)Data(document)["il_complete"], Is.False);
        }

        [Test]
        public void TheDocumentHasExactlyTheSectionsAndCapsTheEditorEmitted()
        {
            InspectionDocument document = IlDiscovery.Collect(Fixture.Inputs());

            Assert.That(document.Caps.Keys, Is.EqualTo(Caps));
            Assert.That(Data(document).Select(p => p.Key), Is.EqualTo(Sections));
            Assert.That((string)Data(document)["il_scope"], Is.EqualTo("direct_stores_in_loaded_assemblies_containing_authored_scripts"));
            Assert.That(document.ToJson().Select(p => p.Key), Is.EqualTo(new[] { "data", "caps", "errors" }));
        }

        private static JsonObject Data(InspectionDocument document) => (JsonObject)document.ToJson()["data"];

        private static List<string> Writers(JsonObject data, string fieldId)
        {
            JsonNode row = data["il_write_sets"].AsArray().Concat(data["il_unlisted_writes"].AsArray())
                .FirstOrDefault(r => (string)r["field_id"] == fieldId);
            Assert.That(row, Is.Not.Null, "no write set for " + fieldId);
            return row["direct_writers"].AsArray().Select(w => (string)w["method"]).ToList();
        }
    }
}
