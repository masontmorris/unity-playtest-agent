// SPDX-License-Identifier: Apache-2.0
using System;
using System.IO;
using NUnit.Framework;
using Playtest.Bridge.Editor;
using Playtest.Bridge.Editor.Session;
using Playtest.Bridge.Transport;
using Playtest.Bridge.Unity;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The Editor-side wiring the wrapper depends on: the entry point it passes to
    /// <c>-executeMethod</c>, the control-directory variable the host and client share, and the
    /// generation-scoped frame directory the live capture writes into. These need the Editor
    /// assembly, so the fast dotnet harness skips this file by name.
    /// </summary>
    [TestFixture]
    public sealed class UnityPlayModeSessionHostTests
    {
        [Test]
        public void TheEntryPointAndVariableAreTheOnesTheWrapperUses()
        {
            Assert.That(
                PlayModeSessionHost.EntryPoint,
                Is.EqualTo("Playtest.Bridge.Editor.PlayModeSessionHost.Start"));
            Assert.That(
                PlayModeSessionHost.ControlDirectoryVariable,
                Is.EqualTo("UPA_SESSION_CONTROL_DIRECTORY"));
            Assert.That(
                PlayModeSessionHost.ControlDirectoryVariable,
                Is.EqualTo(SessionControlDirectory.VariableName));
        }

        [Test]
        public void APlayInstanceCapturesIntoItsOwnGenerationDirectory()
        {
            string root = Path.Combine(Path.GetTempPath(), "upa-frames-" + Guid.NewGuid().ToString("N"));
            string generation = BridgeGeneration.New();

            string directory = UnityCapture.GenerationDirectory(root, generation);

            Assert.That(directory, Is.EqualTo(BridgeGeneration.Directory(root, generation)));
            Assert.That(Path.GetFileName(directory), Is.EqualTo(generation));
            Assert.That(
                Path.GetDirectoryName(directory),
                Is.EqualTo(Path.GetFullPath(root).TrimEnd(
                    Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)));
            Assert.That(Path.GetFileName(Path.GetDirectoryName(directory)),
                Is.EqualTo(BridgeGeneration.FramesDirectoryName));
        }

        [Test]
        public void TwoPlayInstancesNeverShareAFrameDirectory()
        {
            string root = Path.Combine(Path.GetTempPath(), "upa-frames-" + Guid.NewGuid().ToString("N"));

            string first = UnityCapture.GenerationDirectory(root, BridgeGeneration.New());
            string second = UnityCapture.GenerationDirectory(root, BridgeGeneration.New());

            Assert.That(first, Is.Not.EqualTo(second));
            Assert.Throws<ArgumentException>(() => UnityCapture.GenerationDirectory(root, "../escape"));
        }
    }
}
