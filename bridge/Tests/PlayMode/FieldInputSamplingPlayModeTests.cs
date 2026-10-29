// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections;
using System.Collections.Generic;
using System.Threading.Tasks;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Editor.Preflight;
using Playtest.Bridge.Transport;
using Playtest.Bridge.Unity;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    public sealed class FieldInputSamplingPlayModeTests
    {
        [UnityTest]
        public IEnumerator FieldSampleReportsAppliedAndReleasedKey()
        {
            var input = new InputSystemInputBackend();
            var source = new EditorInspectionSource();
            SceneKeyboardState.Start();
            try
            {
                Assert.That(input.IsAvailable, Is.True);
                var action = new BridgeActionDefinition { Name = "probe", Control = "a" };
                Assert.That(input.TryApply(action, out string error), Is.True, error);
                yield return new UnityEngine.WaitForSecondsRealtime(0.1f);
                Task<BridgeInspectionSnapshot> held = Task.Run(() => source.Inspect("fields"));
                DateTime deadline = DateTime.UtcNow.AddSeconds(30);
                while (!held.IsCompleted && DateTime.UtcNow < deadline) yield return null;
                Assert.That(held.IsCompleted, Is.True);
                var heldSnapshot = held.GetAwaiter().GetResult();
                Assert.That(heldSnapshot.Errors, Is.Empty);
                var heldState = (Dictionary<string, object>)heldSnapshot.Data["keyboard_state"];
                Assert.That(heldState["keys"], Is.InstanceOf<List<string>>());
                Assert.That((List<string>)heldState["keys"], Does.Contain("a"));
                Assert.That(heldState["update_type"], Is.EqualTo("dynamic").Or.EqualTo("fixed"));

                Assert.That(input.TryRelease(out _, out error), Is.True, error);
                Assert.That(input.TryVerifyRelease(out error), Is.True, error);
                yield return new UnityEngine.WaitForSecondsRealtime(0.1f);
                Task<BridgeInspectionSnapshot> released = Task.Run(() => source.Inspect("fields"));
                deadline = DateTime.UtcNow.AddSeconds(30);
                while (!released.IsCompleted && DateTime.UtcNow < deadline) yield return null;
                Assert.That(released.IsCompleted, Is.True);
                var releasedSnapshot = released.GetAwaiter().GetResult();
                Assert.That(releasedSnapshot.Errors, Is.Empty);
                var releasedState = (Dictionary<string, object>)releasedSnapshot.Data["keyboard_state"];
                Assert.That((List<string>)releasedState["keys"], Does.Not.Contain("a"));
            }
            finally
            {
                source.Dispose();
                input.Dispose();
                SceneKeyboardState.Stop();
            }
        }
    }
}
