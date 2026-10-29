// SPDX-License-Identifier: Apache-2.0
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

        [Test]
        public void AFloodEvictsTheOldestEventsSoLaterReadersStillFindTheirOwnErrors()
        {
            const int Flood = 300;
            int limit;
            SceneRuntimeErrors.Start();
            LogAssert.ignoreFailingMessages = true;
            try
            {
                limit = (int)SceneRuntimeErrors.Snapshot()["limit"];
                Assert.That(Flood, Is.GreaterThan(limit), "the flood must exceed the monitor's capacity");
                for (int i = 0; i < Flood; i++) Debug.LogError("flood-" + i);

                Dictionary<string, object> flooded = SceneRuntimeErrors.Snapshot();
                var events = (List<object>)flooded["events"];
                Assert.That(flooded["observed"], Is.EqualTo((long)Flood), "the counter covers every error, kept or not");
                Assert.That(flooded["truncated"], Is.True, "some events are no longer in the sample");
                Assert.That(events, Has.Count.EqualTo(limit));
                var oldest = (Dictionary<string, object>)events[0];
                var newest = (Dictionary<string, object>)events[events.Count - 1];
                Assert.That(oldest["message"], Is.EqualTo("flood-" + (Flood - limit)), "the oldest events are the ones evicted");
                Assert.That(newest["message"], Is.EqualTo("flood-" + (Flood - 1)));
                Assert.That(newest["last_sequence"], Is.EqualTo((long)Flood));

                // A window that starts after the flood finds its errors even though the monitor is truncated.
                Debug.LogError("after-the-flood");
                Dictionary<string, object> later = SceneRuntimeErrors.Snapshot();
                var laterEvents = (List<object>)later["events"];
                var last = (Dictionary<string, object>)laterEvents[laterEvents.Count - 1];
                Assert.That(later["observed"], Is.EqualTo((long)Flood + 1));
                Assert.That(last["message"], Is.EqualTo("after-the-flood"));
                Assert.That(last["first_sequence"], Is.EqualTo((long)Flood + 1));
            }
            finally
            {
                LogAssert.ignoreFailingMessages = false;
                SceneRuntimeErrors.Stop();
            }
        }

        [Test]
        public void ConsecutiveDuplicatesStayOneEventWithAnExactCountAndAContiguousRun()
        {
            SceneRuntimeErrors.Start();
            LogAssert.ignoreFailingMessages = true;
            try
            {
                for (int i = 0; i < 5; i++) Debug.LogError("same-message");
                Debug.LogError("different-message");
                for (int i = 0; i < 2; i++) Debug.LogError("same-message");

                Dictionary<string, object> snapshot = SceneRuntimeErrors.Snapshot();
                var events = (List<object>)snapshot["events"];
                Assert.That(snapshot["observed"], Is.EqualTo(8L));
                Assert.That(snapshot["truncated"], Is.False);
                Assert.That(events, Has.Count.EqualTo(3));
                var first = (Dictionary<string, object>)events[0];
                Assert.That(first["count"], Is.EqualTo(5L));
                Assert.That(first["first_sequence"], Is.EqualTo(1L));
                Assert.That(first["last_sequence"], Is.EqualTo(5L), "a merged run covers consecutive sequence numbers");
            }
            finally
            {
                LogAssert.ignoreFailingMessages = false;
                SceneRuntimeErrors.Stop();
            }
        }
    }
}
