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
    /// Hosts the one keyboard action a caller's reproduction plan names, then reloads the first enabled scene
    /// each time the reproduction client asks, strictly in order. The client presses and judges. This host never
    /// presses, chooses or substitutes anything, and an error the game logs is evidence for the client, not a
    /// failure of this test.
    /// </summary>
    public sealed class ReproPlayModeTests
    {
        // Wall-clock allowances behind the test timeout. They only bound a stuck run; the client owns the plan.
        private const double SceneLoadAllowanceS = 40;   // the first load is bounded at 30 s, then one second settles
        private const double ReloadAllowanceS = 70;      // the unload and the load are each bounded at 30 s
        private const double ConnectAllowanceS = 20;     // reconnect, the first reads, and the stop
        private const double PressAllowanceS = 10;       // observe, act, and the read that closes the window
        private const double ClientTailS = 60;           // the report write after the last attempt
        private const double TimeoutMarginS = 30;        // the Test Framework's limit sits above the loop's own

        private static readonly Regex RequestFile = new Regex(@"^reload-([1-9][0-9]*)\.request\.json$",
                                                              RegexOptions.CultureInvariant);

        // Keep these ranges in step with playtest.preflight.repro: the client is the authority on the plan.
        [Serializable] private sealed class ReproPlan
        {
            public int schema_version;
            public string control;
            public double hold_s;
            public int attempts;
            public int presses;
            public double settle_s;
            public double press_interval_s;
            public double window_s;
        }
        [Serializable] private sealed class RequestedAction
        {
            public string name;
            public string control;
            public double requested_hold_s;
        }
        [Serializable] private sealed class ReloadRequest
        {
            public int number;
            public RequestedAction action;
        }
        [Serializable] private sealed class HostAction
        {
            public string name;
            public string control;
            public double default_lease_s;
        }
        [Serializable] private sealed class ReloadDone
        {
            public string method = "scene_reload";
            public bool reloaded;
            public int number;
            public string scene_path;
            public HostAction action;
            public string requested_at_utc;
            public string completed_at_utc;
            public double game_time_s;
            public double real_time_s;
            public int frame;
        }

        [UnityTest]
        public IEnumerator ReproReloadsTheSceneForTheCallersPlanInOrder()
        {
            string planPath = Environment.GetEnvironmentVariable("UPA_REPRO_PLAN");
            string output = Environment.GetEnvironmentVariable("UPA_REPRO_OUTPUT");
            if (string.IsNullOrEmpty(planPath) && string.IsNullOrEmpty(output))
            {
                // Opt-in: the full run needs a wired game and a plan the caller wrote.
                Assert.Throws<ArgumentException>(() => BridgeActionCatalog.FromDefinitions(
                    new[] { new BridgeActionDefinition { Name = "repro_missing_control" } }));
                yield break;
            }
            Assert.That(string.IsNullOrEmpty(planPath) || string.IsNullOrEmpty(output), Is.False,
                        "UPA_REPRO_PLAN and UPA_REPRO_OUTPUT must be set together");
            string python = Environment.GetEnvironmentVariable("UPA_REPRO_PYTHON");
            string handshake = Environment.GetEnvironmentVariable("UPA_REPRO_HANDSHAKE_DIR");
            string repo = Environment.GetEnvironmentVariable("UPA_REPO_ROOT");
            Assert.That(File.Exists(python), Is.True, "the Python executable is missing");
            Assert.That(File.Exists(planPath), Is.True, "the reproduction plan is missing");
            Assert.That(Directory.Exists(repo), Is.True, "the repository path is missing");
            Assert.That(string.IsNullOrEmpty(handshake), Is.False, "the handshake directory is missing");

            string project = Path.GetFullPath(Path.GetDirectoryName(Application.dataPath));
            output = OutsideProject(output, project);
            handshake = OutsideProject(handshake, project);
            Assert.That(File.Exists(output) || Directory.Exists(output), Is.False, "the reproduction report must be new");
            Assert.That(!Directory.Exists(handshake) || Directory.GetFileSystemEntries(handshake).Length == 0, Is.True,
                        "the handshake directory must be new or empty");

            ReproPlan plan = JsonUtility.FromJson<ReproPlan>(File.ReadAllText(planPath));
            Assert.That(plan, Is.Not.Null);
            Assert.That(plan.schema_version, Is.EqualTo(1));
            Assert.That(plan.control, Does.Match("^[a-z0-9]+$"), "the plan names no keyboard control");
            Assert.That(plan.hold_s, Is.GreaterThan(0).And.LessThanOrEqualTo(2), "the plan's hold is out of range");
            Assert.That(plan.attempts, Is.InRange(1, 20), "the plan's attempt count is out of range");
            Assert.That(plan.presses, Is.InRange(1, 10), "the plan's press count is out of range");
            Assert.That(plan.settle_s, Is.GreaterThanOrEqualTo(0).And.LessThanOrEqualTo(60));
            Assert.That(plan.press_interval_s, Is.GreaterThanOrEqualTo(0).And.LessThanOrEqualTo(60));
            Assert.That(plan.window_s, Is.GreaterThanOrEqualTo(0).And.LessThanOrEqualTo(60));
            string actionName = "repro_" + plan.control;

            // The Test Framework's default is 180 s. The limit follows the plan, so a longer plan is not cut off
            // and a stuck run still ends: the budget is capped at the largest the calibration host accepts. The
            // loop below ends at the budget and the framework's limit sits a margin above it, so a stuck client
            // is killed by the cleanup in `finally` instead of being abandoned by the framework.
            DateTime started = DateTime.UtcNow;
            double attemptS = ReloadAllowanceS + ConnectAllowanceS + plan.settle_s +
                              plan.presses * (plan.hold_s + plan.window_s + plan.press_interval_s + PressAllowanceS);
            double budgetS = Math.Min(CalibrationPlan.MaximumBudgetS,
                                      SceneLoadAllowanceS + plan.attempts * attemptS + ClientTailS);
            ExtendTestTimeout(budgetS + TimeoutMarginS);

            Scene loaded = default(Scene);
            System.Diagnostics.Process process = null;
            bool observingErrors = false;
            try
            {
                // The game may log while it is probed. The client counts and judges every such message, so the
                // Test Framework must not turn one into a failed test: not during a load, not between reloads.
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
                // The one-entry catalog is the name, physical control and lease from the caller's plan.
                BridgeEditorHost.StartForPreflight(BridgeActionCatalog.FromDefinitions(new[]
                {
                    new BridgeActionDefinition { Name = actionName, Control = plan.control, DefaultLeaseS = plan.hold_s },
                }));
                Assert.That(BridgeEditorHost.IsRunning, Is.True, BridgeEditorHost.LastError);

                var start = new System.Diagnostics.ProcessStartInfo
                {
                    FileName = python,
                    Arguments = "-m playtest.preflight.repro --project \"" + project + "\" --plan \"" + planPath +
                                "\" --output \"" + output + "\" --handshake-dir \"" + handshake + "\"",
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
                    string requestPath = RequestPath(handshake, number);
                    if (served < plan.attempts && File.Exists(requestPath))
                    {
                        ReloadRequest request = JsonUtility.FromJson<ReloadRequest>(File.ReadAllText(requestPath));
                        Assert.That(request, Is.Not.Null);
                        Assert.That(request.number, Is.EqualTo(number), "the client's reload request is out of order");
                        Assert.That(request.action, Is.Not.Null, "the reload request names no action");
                        Assert.That(request.action.name, Is.EqualTo(actionName));
                        Assert.That(request.action.control, Is.EqualTo(plan.control));
                        Assert.That(request.action.requested_hold_s, Is.EqualTo(plan.hold_s).Within(0.000001),
                                    "the client's hold differs from the hosted action");
                        string requestedAt = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture);
                        yield return new WaitForSecondsRealtime(0.1f);
                        yield return Reload(loaded, scenePath, scene => loaded = scene);
                        LogAssert.ignoreFailingMessages = true;
                        yield return new WaitForSecondsRealtime(1f);
                        var done = new ReloadDone
                        {
                            reloaded = true,
                            number = number,
                            scene_path = scenePath,
                            action = new HostAction { name = actionName, control = plan.control, default_lease_s = plan.hold_s },
                            requested_at_utc = requestedAt,
                            completed_at_utc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
                            game_time_s = Time.timeAsDouble,
                            real_time_s = Time.realtimeSinceStartupAsDouble,
                            frame = Time.frameCount,
                        };
                        WriteDone(DonePath(handshake, number), done);
                        served++;
                    }
                    yield return null;
                }
                Assert.That(process.HasExited, Is.True,
                            "the reproduction client outlived the plan's budget of " +
                            budgetS.ToString("0", CultureInfo.InvariantCulture) + " s");
                // A request that arrived as the client exited is still out of order or in excess.
                CheckRequestFiles(handshake, served, plan.attempts);
                Assert.That(process.ExitCode, Is.EqualTo(0), "the reproduction client failed; inspect its output");
                Assert.That(File.Exists(output), Is.True, "the reproduction report was not written");
                Assert.That(served, Is.EqualTo(plan.attempts),
                            "the client stopped after " + served.ToString(CultureInfo.InvariantCulture) + " of " +
                            plan.attempts.ToString(CultureInfo.InvariantCulture) + " reloads; its report says why");
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

        // Raises, never lowers, the Test Framework's timeout for this test. The value is measured from the
        // start of the test, so it covers the time already spent plus the work still to come.
        private static void ExtendTestTimeout(double remainingS)
        {
            NUnit.Framework.Internal.ITestExecutionContext context = NUnit.Framework.TestContext.CurrentTestExecutionContext;
            Assert.That(context, Is.Not.Null, "the Test Framework exposes no execution context, so the 180 s test timeout cannot follow the plan");
            double elapsedS = (DateTime.UtcNow - context.StartTime).TotalSeconds;
            int milliseconds = CalibrationPlan.TestTimeoutMs(Math.Max(0, elapsedS), remainingS);
            if (milliseconds > context.TestCaseTimeout) context.TestCaseTimeout = milliseconds;
            Assert.That(context.TestCaseTimeout, Is.GreaterThanOrEqualTo(milliseconds), "the test timeout could not be raised");
        }

        private static string RequestPath(string directory, int number)
        {
            return Path.Combine(directory, "reload-" + number.ToString(CultureInfo.InvariantCulture) + ".request.json");
        }

        private static string DonePath(string directory, int number)
        {
            return Path.Combine(directory, "reload-" + number.ToString(CultureInfo.InvariantCulture) + ".done.json");
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
                Assert.That(number, Is.LessThanOrEqualTo(attempts),
                            "excess reload request " + name + ": the plan has " +
                            attempts.ToString(CultureInfo.InvariantCulture) + " attempts");
                Assert.That(number, Is.LessThanOrEqualTo(served + 1),
                            "out-of-order reload request " + name + ": reload " +
                            (served + 1).ToString(CultureInfo.InvariantCulture) + " is next");
            }
        }

        // The client reads the completion from another process, so it is published whole or not at all.
        private static void WriteDone(string donePath, ReloadDone done)
        {
            string directory = Path.GetDirectoryName(donePath);
            if (!string.IsNullOrEmpty(directory)) Directory.CreateDirectory(directory);
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

        // Loads the scene additively and makes it active. The caller owns LogAssert.ignoreFailingMessages: it
        // stays true through the whole run, so nothing the game logs on the frames after a load can fail the test.
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
                        "the reproduction report and handshake files must be outside the game project");
            return full;
        }
    }
}
