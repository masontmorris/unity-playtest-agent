// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections;
using System.Globalization;
using System.IO;
using System.Text.RegularExpressions;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Editor;
using Playtest.Bridge.Editor.Preflight;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Hosts a replay reproduction: serves exactly the catalog the manifest exports, and reloads the first enabled
    /// scene each time the replay client asks, strictly in order. The client replays and scores. This host never
    /// sends, chooses or substitutes an input, and what the game logs is evidence, not a failure of this test.
    /// </summary>
    public sealed class ReplayPlayModeTests
    {
        private const double TimeoutMarginS = 30;

        private static readonly Regex RequestFile = new Regex(@"^reload-([1-9][0-9]*)\.request\.json$",
                                                              RegexOptions.CultureInvariant);

        [Serializable] private sealed class ReplayPlan
        {
            public int schema_version;
            public string kind;
            public int attempts;
            public double host_budget_s;
        }
        [Serializable] private sealed class ReloadRequest { public int number; }
        [Serializable] private sealed class ReloadDone
        {
            public string method = "scene_reload";
            public bool reloaded;
            public int number;
            public string scene_path;
            public string requested_at_utc;
            public string completed_at_utc;
            public double game_time_s;
            public double real_time_s;
            public int frame;
        }

        [UnityTest]
        public IEnumerator ReplayReloadsTheSceneForEachAttemptInOrder()
        {
            string planPath = Environment.GetEnvironmentVariable("UPA_REPLAY_PLAN");
            string output = Environment.GetEnvironmentVariable("UPA_REPLAY_OUTPUT");
            if (string.IsNullOrEmpty(planPath) && string.IsNullOrEmpty(output))
            {
                // Opt-in: the full run needs a wired game, a manifest's catalog and a replay plan.
                Assert.Throws<InvalidOperationException>(() => BridgeSessionCatalog.LoadExported(null, null));
                yield break;
            }
            Assert.That(string.IsNullOrEmpty(planPath) || string.IsNullOrEmpty(output), Is.False,
                        "UPA_REPLAY_PLAN and UPA_REPLAY_OUTPUT must be set together");
            string python = Environment.GetEnvironmentVariable("UPA_REPLAY_PYTHON");
            string handshake = Environment.GetEnvironmentVariable("UPA_REPLAY_HANDSHAKE_DIR");
            string manifest = Environment.GetEnvironmentVariable("UPA_REPLAY_MANIFEST");
            string repo = Environment.GetEnvironmentVariable("UPA_REPO_ROOT");
            Assert.That(File.Exists(python), Is.True, "the Python executable is missing");
            Assert.That(File.Exists(planPath), Is.True, "the replay plan is missing");
            Assert.That(File.Exists(manifest), Is.True, "the manifest is missing");
            Assert.That(Directory.Exists(repo), Is.True, "the repository path is missing");
            Assert.That(string.IsNullOrEmpty(handshake), Is.False, "the handshake directory is missing");

            // The served catalog is exactly the manifest's export, pinned by hash, or this throws before the scene loads.
            BridgeActionCatalog catalog = BridgeSessionCatalog.LoadExported(
                Environment.GetEnvironmentVariable(BridgeSessionCatalog.PathVariable),
                Environment.GetEnvironmentVariable(BridgeSessionCatalog.Sha256Variable));

            string project = Path.GetFullPath(Path.GetDirectoryName(Application.dataPath));
            output = OutsideProject(output, project);
            handshake = OutsideProject(handshake, project);
            Assert.That(File.Exists(output), Is.False, "the replay report must be new");
            Assert.That(!Directory.Exists(handshake) || Directory.GetFileSystemEntries(handshake).Length == 0, Is.True,
                        "the handshake directory must be new or empty");

            ReplayPlan plan = JsonUtility.FromJson<ReplayPlan>(File.ReadAllText(planPath));
            Assert.That(plan, Is.Not.Null);
            Assert.That(plan.schema_version, Is.EqualTo(1));
            Assert.That(plan.kind, Is.EqualTo("replay_reproduction"));
            Assert.That(plan.attempts, Is.InRange(1, 50), "the plan's attempt count is out of range");
            Assert.That(plan.host_budget_s, Is.GreaterThan(0), "the plan gives the host no time budget");
            double budgetS = Math.Min(CalibrationPlan.MaximumBudgetS, plan.host_budget_s);
            DateTime started = DateTime.UtcNow;
            ExtendTestTimeout(budgetS + TimeoutMarginS);

            Scene loaded = default(Scene);
            System.Diagnostics.Process process = null;
            bool observingErrors = false;
            try
            {
                LogAssert.ignoreFailingMessages = true;
                string scenePath = SceneUtility.GetScenePathByBuildIndex(0);
                Assert.That(scenePath, Is.Not.Null.And.Not.Empty, "an enabled scene is required");
                yield return Load(scenePath, scene => loaded = scene);
                yield return new WaitForSecondsRealtime(1f);

                SceneRuntimeErrors.Start();
                observingErrors = true;
                SceneKeyboardState.Start();
                LogAssert.ignoreFailingMessages = true;
                Directory.CreateDirectory(handshake);
                BridgeEditorHost.StartForPreflight(catalog);
                Assert.That(BridgeEditorHost.IsRunning, Is.True, BridgeEditorHost.LastError);

                var start = new System.Diagnostics.ProcessStartInfo
                {
                    FileName = python,
                    Arguments = "-m playtest.runner.reproduce run --project \"" + project + "\" --plan \"" + planPath +
                                "\" --manifest \"" + manifest + "\" --output \"" + output + "\" --handshake-dir \"" +
                                handshake + "\"",
                    WorkingDirectory = repo,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                };
                process = System.Diagnostics.Process.Start(start);
                Assert.That(process, Is.Not.Null);

                DateTime deadline = started.AddSeconds(budgetS);
                int served = 0;
                while (!process.HasExited && DateTime.UtcNow < deadline)
                {
                    CheckRequestFiles(handshake, served, plan.attempts);
                    int number = served + 1;
                    string requestPath = Path.Combine(handshake, "reload-" + number.ToString(CultureInfo.InvariantCulture) + ".request.json");
                    if (served < plan.attempts && File.Exists(requestPath))
                    {
                        ReloadRequest request = JsonUtility.FromJson<ReloadRequest>(File.ReadAllText(requestPath));
                        Assert.That(request, Is.Not.Null);
                        Assert.That(request.number, Is.EqualTo(number), "the client's reload request is out of order");
                        string requestedAt = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture);
                        yield return new WaitForSecondsRealtime(0.1f);
                        yield return Reload(loaded, scenePath, scene => loaded = scene);
                        LogAssert.ignoreFailingMessages = true;
                        yield return new WaitForSecondsRealtime(1f);
                        WriteDone(Path.Combine(handshake, "reload-" + number.ToString(CultureInfo.InvariantCulture) + ".done.json"),
                                  new ReloadDone
                                  {
                                      reloaded = true,
                                      number = number,
                                      scene_path = scenePath,
                                      requested_at_utc = requestedAt,
                                      completed_at_utc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
                                      game_time_s = Time.timeAsDouble,
                                      real_time_s = Time.realtimeSinceStartupAsDouble,
                                      frame = Time.frameCount,
                                  });
                        served++;
                    }
                    yield return null;
                }
                Assert.That(process.HasExited, Is.True,
                            "the replay client outlived the plan's budget of " +
                            budgetS.ToString("0", CultureInfo.InvariantCulture) + " s");
                CheckRequestFiles(handshake, served, plan.attempts);
                Assert.That(process.ExitCode, Is.EqualTo(0), "the replay client failed; inspect its output");
                Assert.That(File.Exists(output), Is.True, "the replay report was not written");
            }
            finally
            {
                if (process != null)
                {
                    if (!process.HasExited) { process.Kill(); process.WaitForExit(5000); }
                    process.Dispose();
                }
                BridgeEditorHost.Stop();
                if (observingErrors) SceneRuntimeErrors.Stop();
                SceneKeyboardState.Stop();
                LogAssert.ignoreFailingMessages = false;
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

        // Raises, never lowers, the Test Framework's timeout for this test.
        private static void ExtendTestTimeout(double remainingS)
        {
            NUnit.Framework.Internal.ITestExecutionContext context = NUnit.Framework.TestContext.CurrentTestExecutionContext;
            Assert.That(context, Is.Not.Null, "the Test Framework exposes no execution context, so the 180 s test timeout cannot follow the plan");
            double elapsedS = (DateTime.UtcNow - context.StartTime).TotalSeconds;
            int milliseconds = CalibrationPlan.TestTimeoutMs(Math.Max(0, elapsedS), remainingS);
            if (milliseconds > context.TestCaseTimeout) context.TestCaseTimeout = milliseconds;
            Assert.That(context.TestCaseTimeout, Is.GreaterThanOrEqualTo(milliseconds), "the test timeout could not be raised");
        }

        // Requests arrive one at a time, in order, and never beyond the plan. Anything else is a defect in the client.
        private static void CheckRequestFiles(string directory, int served, int attempts)
        {
            foreach (string path in Directory.GetFiles(directory))
            {
                string name = Path.GetFileName(path);
                if (!name.StartsWith("reload-", StringComparison.Ordinal) ||
                    !name.EndsWith(".request.json", StringComparison.Ordinal)) continue;
                Match match = RequestFile.Match(name);
                Assert.That(match.Success, Is.True, "unexpected reload request file: " + name);
                int number;
                Assert.That(int.TryParse(match.Groups[1].Value, NumberStyles.None, CultureInfo.InvariantCulture, out number),
                            Is.True, "reload request number is out of range: " + name);
                Assert.That(number, Is.LessThanOrEqualTo(attempts), "excess reload request " + name);
                Assert.That(number, Is.LessThanOrEqualTo(served + 1), "out-of-order reload request " + name);
            }
        }

        // The client reads the completion from another process, so it is published whole or not at all.
        private static void WriteDone(string donePath, ReloadDone done)
        {
            string writingPath = donePath + ".writing";
            using (var writer = new StreamWriter(new FileStream(writingPath, FileMode.CreateNew, FileAccess.Write)))
            {
                writer.WriteLine(JsonUtility.ToJson(done, true));
            }
            File.Move(writingPath, donePath);
        }

        private static IEnumerator Reload(Scene loaded, string scenePath, Action<Scene> assign)
        {
            for (int i = 0; i < SceneManager.sceneCount; i++)
            {
                Scene other = SceneManager.GetSceneAt(i);
                if (other.IsValid() && other != loaded) { SceneManager.SetActiveScene(other); break; }
            }
            AsyncOperation unloading = SceneManager.UnloadSceneAsync(loaded);
            Assert.That(unloading, Is.Not.Null);
            DateTime deadline = DateTime.UtcNow.AddSeconds(30);
            while (!unloading.isDone && DateTime.UtcNow < deadline) yield return null;
            Assert.That(unloading.isDone, Is.True, "the scene did not unload");
            assign(default(Scene));
            yield return Load(scenePath, assign);
        }

        private static IEnumerator Load(string path, Action<Scene> assign)
        {
            LogAssert.ignoreFailingMessages = true;
            AsyncOperation loading = SceneManager.LoadSceneAsync(path, LoadSceneMode.Additive);
            Assert.That(loading, Is.Not.Null);
            DateTime deadline = DateTime.UtcNow.AddSeconds(30);
            while (!loading.isDone && DateTime.UtcNow < deadline) yield return null;
            Assert.That(loading.isDone, Is.True, "the scene did not load");
            yield return null;
            Scene scene = SceneManager.GetSceneByPath(path);
            Assert.That(scene.IsValid() && scene.isLoaded, Is.True);
            SceneManager.SetActiveScene(scene);
            assign(scene);
        }

        private static string OutsideProject(string path, string project)
        {
            Assert.That(string.IsNullOrEmpty(path), Is.False);
            string full = Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string prefix = project.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            Assert.That(full.Equals(project, StringComparison.OrdinalIgnoreCase) ||
                        full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase), Is.False,
                        "the replay report and handshake files must be outside the game project");
            return full;
        }
    }
}
