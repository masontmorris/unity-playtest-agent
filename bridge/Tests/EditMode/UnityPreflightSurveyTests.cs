// SPDX-License-Identifier: Apache-2.0
using System.Collections.Generic;
using NUnit.Framework;
using Playtest.Bridge.Editor.Preflight;
using UnityEngine;

namespace Playtest.Bridge.Tests
{
    public sealed class UnityPreflightSurveyTests
    {
        [Test]
        public void CapRecordsBothOmittedAndRetainedItems()
        {
            var document = new InspectionDocument();
            List<int> kept = document.Take("items", new[] { 3, 5, 7 }, 2);
            Assert.That(kept, Is.EqualTo(new[] { 3, 5 }));
            Assert.That(document.Caps["items"].Limit, Is.EqualTo(2));
            Assert.That(document.Caps["items"].Observed, Is.EqualTo(3));
            Assert.That(document.Caps["items"].Returned, Is.EqualTo(2));
            Assert.That(document.Caps["items"].Truncated, Is.True);
        }

        [Test]
        public void UnavailableCollectionDoesNotClaimItWasEmpty()
        {
            var document = new InspectionDocument();
            document.Unavailable("items", 10, "unavailable");
            Assert.That(document.Caps["items"].Observed, Is.Null);
            Assert.That(document.Caps["items"].Truncated, Is.Null);
            Assert.That(document.Errors, Has.Count.EqualTo(1));
        }

        [Test]
        public void SurveyReportsVersionAndExplicitBounds()
        {
            InspectionDocument document = ProjectSurveyCollector.Collect();
            Assert.That(document.Data["unity_version"], Is.EqualTo(Application.unityVersion));
            foreach (string name in new[] { "packages", "scenes", "scripts", "input_assets", "legacy_axes" })
            {
                Assert.That(document.Data.ContainsKey(name), Is.True, name);
                Assert.That(document.Caps.ContainsKey(name), Is.True, name);
                Assert.That(document.Caps[name].Limit, Is.GreaterThan(0), name);
            }
        }

        [Test]
        public void SourceReportsBoundsAndUnrankedScope()
        {
            InspectionDocument document = SourceCollector.Collect();
            Assert.That(document.Data["source_selection"], Is.EqualTo("unranked_authored_scripts"));
            Assert.That(document.Caps.ContainsKey("source_files"), Is.True);
            Assert.That(document.Caps.ContainsKey("source_characters"), Is.True);
            Assert.That(document.Caps["source_characters"].Returned,
                Is.LessThanOrEqualTo(document.Caps["source_characters"].Limit));
        }
    }
}
