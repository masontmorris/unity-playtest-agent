// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections;
using System.IO;
using System.Threading.Tasks;
using NUnit.Framework;
using Playtest.Bridge.Editor.Preflight;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;
using UnityEngine;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    public sealed class UnityInspectionSurveyTests
    {
        [UnityTest]
        public IEnumerator SurveyIsMarshalledToTheEditorAndRecordsItsCaps()
        {
            using (var source = new EditorInspectionSource())
            {
                Task<BridgeInspectionSnapshot> pending = Task.Run(() => source.Inspect("inventory"));
                DateTime deadline = DateTime.UtcNow.AddSeconds(120);
                while (!pending.IsCompleted && DateTime.UtcNow < deadline) yield return null;
                Assert.That(pending.IsCompleted, Is.True, "inspection did not complete");
                BridgeInspectionSnapshot snapshot = pending.GetAwaiter().GetResult();
                Assert.That(snapshot.Data.ContainsKey("unity_version"), Is.True);
                Assert.That(snapshot.Caps.ContainsKey("scripts"), Is.True);

                Task<BridgeInspectionSnapshot> wrongMode = Task.Run(() => source.Inspect("discovery"));
                deadline = DateTime.UtcNow.AddSeconds(120);
                while (!wrongMode.IsCompleted && DateTime.UtcNow < deadline) yield return null;
                Assert.That(wrongMode.IsCompleted, Is.True, "mode rejection did not complete");
                Assert.Throws<BridgeInspectionUnavailableException>(() => wrongMode.GetAwaiter().GetResult());

                string output = Environment.GetEnvironmentVariable("UPA_PREFLIGHT_SURVEY_OUTPUT");
                if (!string.IsNullOrEmpty(output))
                {
                    string path = Path.GetFullPath(output);
                    string project = Path.GetFullPath(Path.GetDirectoryName(Application.dataPath))
                        .TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
                    Assert.That(path.StartsWith(project, StringComparison.OrdinalIgnoreCase), Is.False,
                        "output must be outside the inspected project");
                    Directory.CreateDirectory(Path.GetDirectoryName(path));
                    var message = new InspectionResultMessage
                    {
                        Session = "s-survey-test", Seq = 0, WallUtc = DateTime.UtcNow,
                        Stage = "inventory", InReplyTo = 0,
                        Data = snapshot.Data, Caps = snapshot.Caps, Errors = snapshot.Errors,
                    };
                    using (var writer = new StreamWriter(new FileStream(path, FileMode.CreateNew, FileAccess.Write)))
                        writer.WriteLine(BridgeProtocol.Serialize(message));
                }
            }
        }
    }
}
