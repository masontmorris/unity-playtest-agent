using System;
using System.Collections;
using System.IO;
using System.Net.Sockets;
using System.Threading.Tasks;
using NUnit.Framework;
using Playtest.Bridge.Editor;
using Playtest.Bridge.Lock;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    public sealed class InspectionPlayModeTests
    {
        [UnityTest]
        public IEnumerator InspectionReadsTheLoadedSceneThroughTheBridge()
        {
            Scene loaded = default(Scene);
            try
            {
                string scenePath = SceneUtility.GetScenePathByBuildIndex(0);
                Assert.That(scenePath, Is.Not.Null.And.Not.Empty, "an enabled build scene is required");
                LogAssert.ignoreFailingMessages = true;
                try
                {
                    AsyncOperation loading = SceneManager.LoadSceneAsync(scenePath, LoadSceneMode.Additive);
                    Assert.That(loading, Is.Not.Null);
                    DateTime loadDeadline = DateTime.UtcNow.AddSeconds(30);
                    while (!loading.isDone && DateTime.UtcNow < loadDeadline) yield return null;
                    Assert.That(loading.isDone, Is.True, "the scene did not load");
                    yield return null;
                    loaded = SceneManager.GetSceneByPath(scenePath);
                    Assert.That(loaded.IsValid() && loaded.isLoaded, Is.True);
                    SceneManager.SetActiveScene(loaded);
                }
                finally { LogAssert.ignoreFailingMessages = false; }

                BridgeEditorHost.Start();
                Assert.That(BridgeEditorHost.IsRunning, Is.True, BridgeEditorHost.LastError);
                BridgeLockInfo endpoint = BridgeLockFile.Read(BridgeEditorHost.LockPath);
                Assert.That(endpoint, Is.Not.Null);
                Task<string> response = Task.Run(() => Request(endpoint.Port, endpoint.Token));
                DateTime deadline = DateTime.UtcNow.AddSeconds(120);
                while (!response.IsCompleted && DateTime.UtcNow < deadline) yield return null;
                Assert.That(response.IsCompleted, Is.True, "inspection did not complete");
                string json = response.GetAwaiter().GetResult();
                Assert.That(BridgeProtocol.TryReadInspectionResult(json, out InspectionResultMessage result, out ProtocolError error),
                    Is.True, error?.Message);
                Assert.That(result.Stage, Is.EqualTo("discovery"));
                Assert.That(result.InReplyTo, Is.EqualTo(2));
                Assert.That(result.Caps.ContainsKey("source_files"), Is.True);
                Assert.That(result.Caps.ContainsKey("il_assemblies"), Is.True);
                // The IL write scan runs outside Unity; the bridge reports what to scan.
                Assert.That(result.Data.ContainsKey("il_inputs"), Is.True);
                Assert.That(result.Data.ContainsKey("il_write_sets"), Is.False);

                string output = Environment.GetEnvironmentVariable("UPA_PREFLIGHT_DISCOVERY_OUTPUT");
                if (!string.IsNullOrEmpty(output))
                {
                    string path = Path.GetFullPath(output);
                    string project = Path.GetFullPath(Path.GetDirectoryName(Application.dataPath))
                        .TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
                    Assert.That(path.StartsWith(project, StringComparison.OrdinalIgnoreCase), Is.False,
                        "output must be outside the inspected project");
                    Directory.CreateDirectory(Path.GetDirectoryName(path));
                    using (var writer = new StreamWriter(new FileStream(path, FileMode.CreateNew, FileAccess.Write)))
                        writer.WriteLine(json);
                }
            }
            finally
            {
                BridgeEditorHost.Stop();
                if (loaded.IsValid() && loaded.isLoaded)
                {
                    for (int i = 0; i < SceneManager.sceneCount; i++)
                    {
                        Scene other = SceneManager.GetSceneAt(i);
                        if (other.IsValid() && other != loaded) { SceneManager.SetActiveScene(other); break; }
                    }
                    SceneManager.UnloadSceneAsync(loaded);
                }
            }
        }

        private static string Request(int port, string token)
        {
            using (var client = new TcpClient())
            {
                client.Connect("127.0.0.1", port);
                client.ReceiveTimeout = 120000;
                var connection = new JsonLineConnection(client.GetStream(), 8 * 1024 * 1024);
                connection.WriteLine(BridgeProtocol.Serialize(new HelloMessage
                {
                    Session = "s-inspection-test", Seq = 1, WallUtc = DateTime.UtcNow,
                    Token = token, CoreVersion = "test",
                }));
                string status = connection.ReadLine(120000);
                if (!BridgeProtocol.TryReadStatus(status, out StatusMessage _, out ProtocolError statusError))
                    throw new InvalidOperationException("handshake failed: " + statusError?.Message);
                connection.WriteLine(BridgeProtocol.Serialize(new InspectRequestMessage
                {
                    Session = "s-inspection-test", Seq = 2, WallUtc = DateTime.UtcNow,
                    Stage = "discovery",
                }));
                string response = connection.ReadLine(120000);
                if (BridgeProtocol.TryReadBridgeError(response, out BridgeErrorMessage bridgeError, out ProtocolError _))
                    throw new InvalidOperationException("inspection failed: " + bridgeError.Message);
                return response;
            }
        }
    }
}
