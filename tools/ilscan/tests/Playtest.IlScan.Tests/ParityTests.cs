using System;
using System.Linq;
using System.Text.Json.Nodes;
using NUnit.Framework;

namespace Playtest.IlScan.Tests
{
    /// <summary>
    /// The scan used to run on live types in the Editor, with tokens resolved by the runtime. Loading the
    /// same assembly for inspection only must produce the same document: the same ids, order, counts and
    /// reasons. The fixture covers generic types and methods, nested and compiler-generated types,
    /// closed instantiations, other assemblies, struct fields and bodies without IL.
    /// </summary>
    [TestFixture]
    public sealed class ParityTests
    {
        [Test]
        public void InspectionOnlyScanMatchesLiveReflection()
        {
            JsonObject live = LiveDocument();
            using (var scope = new InspectionScope())
            {
                var inspected = new InspectionDocument();
                IlDiscovery.Build(inspected, scope.Fixtures.GetTypes(), scope.Resolver, false);
                AssertSame(inspected.ToJson(), live);
            }
        }

        [Test]
        public void CollectedFromFilesMatchesLiveReflection()
        {
            AssertSame(IlDiscovery.Collect(Fixture.Inputs()).ToJson(), LiveDocument());
        }

        [Test]
        public void TheFixtureExercisesEveryResolutionPath()
        {
            JsonObject live = LiveDocument();
            var data = (JsonObject)live["data"];
            Assert.That(live["errors"].AsArray(), Is.Empty);
            Assert.That(data["il_issues"].AsArray().Select(i => (string)i["kind"]).Distinct(),
                Is.EquivalentTo(new[] { "IndirectWrite", "MissingMethodBody" }));
            var written = data["il_write_sets"].AsArray().Where(r => r["direct_writers"].AsArray().Count > 0)
                .Select(r => (string)r["field_id"]).ToList();
            var unlisted = data["il_unlisted_writes"].AsArray().Select(r => (string)r["field_id"]).ToList();
            Assert.That(written, Has.Some.Contains("GenericOwner`1:Value"), "identity instantiation");
            Assert.That(written, Has.Some.Contains("Outer`1+Pair`1:Second"), "nested generic type");
            Assert.That(written, Has.Some.Contains("SingletonBase`1:s_instance"), "generic base class");
            Assert.That(unlisted, Has.Some.Contains("Dependency.ExternalState:Level"), "another assembly");
            Assert.That(unlisted, Has.Some.Contains("[[System.Int32"), "closed instantiation");
            Assert.That(unlisted, Has.Some.Contains("System.ValueTuple`2"), "framework value type");
        }

        private static JsonObject LiveDocument()
        {
            var document = new InspectionDocument();
            IlDiscovery.Build(document, Fixture.Live.GetTypes(), new RuntimeFieldResolver(), false);
            return document.ToJson();
        }

        private static void AssertSame(JsonObject actual, JsonObject expected)
        {
            string left = actual.ToJsonString();
            string right = expected.ToJsonString();
            if (left == right) return;

            // Report the first differing row instead of two long strings.
            var actualData = (JsonObject)actual["data"];
            var expectedData = (JsonObject)expected["data"];
            foreach (string section in new[] { "il_write_sets", "il_unlisted_writes", "il_issues" })
            {
                var a = actualData[section].AsArray().Select(n => n.ToJsonString()).ToList();
                var e = expectedData[section].AsArray().Select(n => n.ToJsonString()).ToList();
                for (int i = 0; i < Math.Max(a.Count, e.Count); i++)
                {
                    string got = i < a.Count ? a[i] : "<missing>";
                    string want = i < e.Count ? e[i] : "<missing>";
                    Assert.That(got, Is.EqualTo(want), section + "[" + i + "]");
                }
            }

            Assert.That(left, Is.EqualTo(right));
        }
    }
}
