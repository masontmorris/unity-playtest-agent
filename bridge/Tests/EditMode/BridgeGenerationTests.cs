// SPDX-License-Identifier: Apache-2.0
using System;
using System.IO;
using System.Text.RegularExpressions;
using NUnit.Framework;
using Playtest.Bridge.Transport;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// A generation names exactly one play instance and also one directory component. Its shape is
    /// closed so it can never address another session's frames, a parent directory, or the legacy
    /// unversioned capture root that bench and preflight hosts still read.
    /// </summary>
    [TestFixture]
    public sealed class BridgeGenerationTests
    {
        private static readonly Regex Canonical = new Regex(
            "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            RegexOptions.CultureInvariant);

        [Test]
        public void NewIsACanonicalLowercaseGeneration()
        {
            string first = BridgeGeneration.New();
            string second = BridgeGeneration.New();

            Assert.That(first, Is.EqualTo(first.ToLowerInvariant()));
            Assert.That(Canonical.IsMatch(first), Is.True, first);
            Assert.That(BridgeGeneration.IsCanonical(first), Is.True);
            Assert.That(second, Is.Not.EqualTo(first), "a new instance never reuses a generation");
            Assert.That(Guid.Parse(first).ToString("D"), Is.EqualTo(first));
        }

        [Test]
        public void OnlyTheCanonicalHyphenatedFormIsAccepted(
            [Values(
                "3f2f1e9c-1a2b-4c3d-8e4f-5a6b7c8d9e0f",
                "00000000-0000-0000-0000-000000000000")]
            string value)
        {
            Assert.That(BridgeGeneration.IsCanonical(value), Is.True, value);
        }

        [Test]
        public void AnythingElseIsRejected(
            [Values(
                null,
                "",
                "3F2F1E9C-1A2B-4C3D-8E4F-5A6B7C8D9E0F",
                "3f2f1e9c1a2b4c3d8e4f5a6b7c8d9e0f",
                "{3f2f1e9c-1a2b-4c3d-8e4f-5a6b7c8d9e0f}",
                "3f2f1e9c-1a2b-4c3d-8e4f-5a6b7c8d9e0",
                "3f2f1e9c-1a2b-4c3d-8e4f-5a6b7c8d9e0f/../x",
                "../3f2f1e9c-1a2b-4c3d-8e4f-5a6b7c8d9e0f",
                "..",
                "C:\\Temp\\frames")]
            string value)
        {
            Assert.That(BridgeGeneration.IsCanonical(value), Is.False, value ?? "<null>");
            Assert.Throws<ArgumentException>(() => BridgeGeneration.Validate(value, "generation"));
        }

        [Test]
        public void TheGenerationDirectoryIsADirectChildOfTheFramesRoot()
        {
            string root = Path.Combine(Path.GetTempPath(), "upa-frames-" + Guid.NewGuid().ToString("N"));
            string generation = BridgeGeneration.New();

            string directory = BridgeGeneration.Directory(root, generation);
            string expected = Path.Combine(Path.GetFullPath(root), generation);

            Assert.That(directory, Is.EqualTo(expected));
            Assert.That(
                Path.GetDirectoryName(directory),
                Is.EqualTo(Path.GetFullPath(root).TrimEnd(
                    Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)));
            Assert.That(Path.GetFileName(directory), Is.EqualTo(generation));
        }

        [Test]
        public void ADirectoryIsNeverBuiltFromAnUnvalidatedGeneration()
        {
            string root = Path.Combine(Path.GetTempPath(), "upa-frames-" + Guid.NewGuid().ToString("N"));

            foreach (string bad in new[] { null, "", "..", "../escape", "3F2F1E9C-1A2B-4C3D-8E4F-5A6B7C8D9E0F" })
            {
                Assert.Throws<ArgumentException>(() => BridgeGeneration.Directory(root, bad));
            }

            Assert.Throws<ArgumentException>(() => BridgeGeneration.Directory(null, BridgeGeneration.New()));
            Assert.Throws<ArgumentException>(() => BridgeGeneration.Directory("", BridgeGeneration.New()));
        }

        [Test]
        public void TheSharedFramesFolderKeepsItsName()
        {
            Assert.That(BridgeGeneration.FramesDirectoryName, Is.EqualTo("playtest-bridge-frames"));
            Assert.That(BridgeGeneration.Validate(BridgeGeneration.New(), "generation"), Is.Not.Empty);
        }
    }
}
