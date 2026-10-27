using System.Collections.Generic;
using NUnit.Framework;
using Playtest.Bridge.Editor.Preflight;
using UnityEngine;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    public sealed class UnityRuntimeErrorsTests
    {
        [Test]
        public void CapturesErrorsOnlyWhileTheObserverIsActive()
        {
            SceneRuntimeErrors.Start();
            try
            {
                LogAssert.Expect(LogType.Error, "preflight-observer-test");
                Debug.LogError("preflight-observer-test");
                Dictionary<string, object> snapshot = SceneRuntimeErrors.Snapshot();
                Assert.That(snapshot["active"], Is.True);
                Assert.That(snapshot["observed"], Is.EqualTo(1L));
                Assert.That(snapshot["truncated"], Is.False);
                var events = (List<object>)snapshot["events"];
                Assert.That(events, Has.Count.EqualTo(1));
                Assert.That(((Dictionary<string, object>)events[0])["message"], Is.EqualTo("preflight-observer-test"));
            }
            finally { SceneRuntimeErrors.Stop(); }
            var stopped = SceneRuntimeErrors.Snapshot();
            Assert.That(stopped["active"], Is.False);
            Assert.That(stopped["observed"], Is.Null);
        }
    }
}
