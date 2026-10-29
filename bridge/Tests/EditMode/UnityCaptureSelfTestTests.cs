// SPDX-License-Identifier: Apache-2.0
using System;
using System.IO;
using NUnit.Framework;
using Playtest.Bridge.Unity;
using UnityEngine;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The Editor host runs its capture self-test before entering play mode. This measures the
    /// same path outside play mode: with a graphics device it must pass, and without one it must
    /// fail closed instead of pretending frames can be captured.
    /// </summary>
    [TestFixture]
    public sealed class CaptureSelfTestTests
    {
        [Test]
        public void SelfTest_MatchesTheGraphicsDeviceAvailable()
        {
            string directory = Path.Combine(
                Path.GetTempPath(),
                "upa-capture-editmode-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(directory);
            UnityCapture capture = null;
            try
            {
                capture = new UnityCapture(directory, 640, 10, 3);
                if (SystemInfo.graphicsDeviceType == UnityEngine.Rendering.GraphicsDeviceType.Null)
                {
                    Assert.That(capture.Available, Is.False, "capture must fail closed without a graphics device");
                    Assert.That(capture.SelfTest, Is.EqualTo(UnityCapture.Failed));
                    Assert.That(capture.LastError, Does.Contain("graphics"));
                    Assert.That(capture.Written, Is.EqualTo(0));
                    return;
                }

                Assert.That(capture.Available, Is.True, capture.LastError);
                Assert.That(capture.SelfTest, Is.EqualTo(UnityCapture.Passed), capture.LastError);
                Assert.That(File.Exists(capture.SelfTestPath), Is.True, "the self-test JPEG was not written");
                Assert.That(capture.Written, Is.EqualTo(0), "the self-test must not write a play-mode frame");
            }
            finally
            {
                if (capture != null)
                {
                    capture.Dispose();
                }

                try
                {
                    if (Directory.Exists(directory))
                    {
                        Directory.Delete(directory, true);
                    }
                }
                catch (IOException)
                {
                }
                catch (UnauthorizedAccessException)
                {
                }
            }
        }
    }
}
