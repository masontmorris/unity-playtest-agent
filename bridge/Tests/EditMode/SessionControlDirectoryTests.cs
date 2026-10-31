// SPDX-License-Identifier: Apache-2.0
using System;
using System.IO;
using System.Linq;
using NUnit.Framework;
using Playtest.Bridge.Editor.Session;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The control directory is the one place the host publishes. These tests pin what it may contain
    /// (host control records only), that a reader never sees a partial record, and that the environment
    /// is the single handle both processes share.
    /// </summary>
    [TestFixture]
    public sealed class SessionControlDirectoryTests
    {
        private string _root;
        private string _previous;

        [SetUp]
        public void SetUp()
        {
            _root = SessionTestSupport.TempDirectory();
            Directory.CreateDirectory(_root);
            _previous = Environment.GetEnvironmentVariable(SessionControlDirectory.VariableName);
        }

        [TearDown]
        public void TearDown()
        {
            Environment.SetEnvironmentVariable(SessionControlDirectory.VariableName, _previous);
            try
            {
                if (Directory.Exists(_root))
                {
                    Directory.Delete(_root, true);
                }
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
        }

        [Test]
        public void TheAgreedMailboxFilenamesAreExact()
        {
            SessionControlDirectory control = SessionControlDirectory.Open(_root);

            Assert.That(Path.GetFileName(control.ConfigPath), Is.EqualTo("config.json"));
            Assert.That(Path.GetFileName(control.HostStatePath), Is.EqualTo("host-state.json"));
            Assert.That(Path.GetFileName(control.RequestPath), Is.EqualTo("request.json"));
            Assert.That(Path.GetFileName(control.ResultPath), Is.EqualTo("result.json"));
            Assert.That(Path.GetFileName(control.CancelPath), Is.EqualTo("cancel.json"));
            Assert.That(Path.GetFileName(control.ShutdownPath), Is.EqualTo("shutdown"));
            Assert.That(SessionControlDirectory.VariableName, Is.EqualTo("UPA_SESSION_CONTROL_DIRECTORY"));
        }

        [Test]
        public void ItOpensOnlyAnExistingDirectory()
        {
            Assert.Throws<DirectoryNotFoundException>(
                () => SessionControlDirectory.Open(Path.Combine(_root, "missing")));
            Assert.Throws<ArgumentException>(() => SessionControlDirectory.Open(null));
            Assert.Throws<ArgumentException>(() => SessionControlDirectory.Open("  "));
        }

        [Test]
        public void TheEnvironmentNamesTheDirectoryTheHostReads()
        {
            Environment.SetEnvironmentVariable(SessionControlDirectory.VariableName, _root);
            SessionControlDirectory control = SessionControlDirectory.FromEnvironment();

            Assert.That(control.Root, Is.EqualTo(Path.GetFullPath(_root)));

            Environment.SetEnvironmentVariable(SessionControlDirectory.VariableName, null);
            Assert.Throws<InvalidOperationException>(() => SessionControlDirectory.FromEnvironment());
        }

        [Test]
        public void APublishedRecordIsReadableAndLeavesNoTemporaryFile()
        {
            SessionControlDirectory control = SessionControlDirectory.Open(_root);
            var state = new HostStateRecord
            {
                RunId = SessionTestSupport.RunId,
                Phase = PlayModeSessionPhase.Ready,
                Generation = SessionTestSupport.Generation,
                ScenePath = SessionTestSupport.ScenePath,
                SceneHandle = 4,
                ReadyUtc = SessionTestSupport.T0,
                Capabilities = new[] { HostStateRecord.RestartCapability },
            };

            control.WriteHostState(state);
            Assert.That(control.TryReadText(control.HostStatePath, out string text), Is.True);
            HostStateRecord read = HostStateRecord.Parse(text);
            Assert.That(read.Generation, Is.EqualTo(SessionTestSupport.Generation));
            Assert.That(read.SceneHandle, Is.EqualTo(4));

            string[] files = Directory.GetFiles(_root);
            Assert.That(files.Select(Path.GetFileName), Is.EquivalentTo(new[] { "host-state.json" }),
                "an atomic publication leaves only the record itself");

            control.WriteResult(SessionTestSupport.ReadyResult());
            Assert.That(control.TryReadText(control.ResultPath, out string result), Is.True);
            Assert.That(RestartResultRecord.Parse(result).Phase, Is.EqualTo(RestartPhase.Ready));
        }

        [Test]
        public void RewritingReplacesTheBodyAndAMissingRecordIsNotThereYet()
        {
            SessionControlDirectory control = SessionControlDirectory.Open(_root);
            Assert.That(control.TryReadText(control.RequestPath, out string missing), Is.False);
            Assert.That(missing, Is.Null);

            control.Write(control.RequestPath, "{\"version\":1}");
            control.Write(control.RequestPath, "{\"version\":2}");
            Assert.That(control.TryReadText(control.RequestPath, out string text), Is.True);
            Assert.That(text, Is.EqualTo("{\"version\":2}"));
            Assert.That(Directory.GetFiles(_root, "*.tmp").Length, Is.EqualTo(0));
        }

        [Test]
        public void TheShutdownMarkerIsOutOfBandAndOnlyItsPresenceCounts()
        {
            SessionControlDirectory control = SessionControlDirectory.Open(_root);
            Assert.That(control.ShutdownRequested, Is.False);

            control.Write(control.ShutdownPath, string.Empty);
            Assert.That(control.ShutdownRequested, Is.True);
            Assert.That(File.ReadAllText(control.ShutdownPath), Is.Empty);
        }
    }
}
