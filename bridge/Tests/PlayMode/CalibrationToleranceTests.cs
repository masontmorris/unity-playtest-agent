using System;
using System.Collections.Generic;
using NUnit.Framework;
using Playtest.Bridge.Editor.Preflight;
using UnityEngine;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The calibration probe must tolerate a game that logs while it is probed: a logged exception
    /// marks that trial invalid as evidence and does not fail the Unity test.
    /// </summary>
    public sealed class CalibrationToleranceTests
    {
        [Test]
        public void ALoggedExceptionErrorAndAssertionAreCountedAndDoNotFailTheTestInsideTheScope()
        {
            SceneRuntimeErrors.Start();
            try
            {
                using (new CalibrationLogTolerance())
                {
                    Assert.That(LogAssert.ignoreFailingMessages, Is.True);
                    Debug.LogException(new InvalidOperationException("calibration-tolerance-exception"));
                    Debug.LogError("calibration-tolerance-error");
                    Debug.LogAssertion("calibration-tolerance-assertion");
                }
                Dictionary<string, object> snapshot = SceneRuntimeErrors.Snapshot();
                Assert.That(snapshot["observed"], Is.EqualTo(3L), "every logged message is evidence for the client");
                var types = new List<string>();
                foreach (object item in (List<object>)snapshot["events"])
                    types.Add((string)((Dictionary<string, object>)item)["type"]);
                Assert.That(types, Is.EqualTo(new[] { "Exception", "Error", "Assert" }));
            }
            finally { SceneRuntimeErrors.Stop(); }
        }

        [Test]
        public void TheToleranceEndsWithTheScopeSoLaterMessagesAreNotIgnored()
        {
            Assert.That(LogAssert.ignoreFailingMessages, Is.False, "an ordinary test starts without the tolerance");
            using (new CalibrationLogTolerance())
                Assert.That(LogAssert.ignoreFailingMessages, Is.True);
            Assert.That(LogAssert.ignoreFailingMessages, Is.False);
        }

        [Test]
        public void TheToleranceEndsEvenWhenTheScopeIsLeftByAnException()
        {
            Assert.Throws<InvalidOperationException>(() =>
            {
                using (new CalibrationLogTolerance()) throw new InvalidOperationException("left the scope");
            });
            Assert.That(LogAssert.ignoreFailingMessages, Is.False);
        }
    }
}
