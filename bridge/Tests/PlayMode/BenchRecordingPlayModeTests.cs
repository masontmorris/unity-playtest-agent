using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Editor;
using Playtest.Bridge.Editor.Preflight;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>Hosts an explicitly requested offline bench recording; no model is called.</summary>
    public sealed class BenchRecordingPlayModeTests
    {
        // The Test Framework ends a UnityTest after 180 s by default, which cut a 10-minute session off
        // at three minutes. The host keeps its own deadline (UPA_BENCH_TIMEOUT_S, at most 1800 s); this
        // only has to outlast it.
        [UnityTest, Timeout(1900000)]
        public IEnumerator RecordRequestedFrames()
        {
            string output = Environment.GetEnvironmentVariable("UPA_BENCH_OUTPUT");
            if (string.IsNullOrEmpty(output))
            {
                Assert.Throws<ArgumentOutOfRangeException>(() => SceneTransformCollector.Collect(0));
                yield break;
            }
            string python = Environment.GetEnvironmentVariable("UPA_BENCH_PYTHON");
            string entity = Environment.GetEnvironmentVariable("UPA_BENCH_ENTITY");
            string calibration = Environment.GetEnvironmentVariable("UPA_BENCH_CALIBRATION");
            string repo = Environment.GetEnvironmentVariable("UPA_REPO_ROOT");
            Assert.That(File.Exists(python), Is.True, "the Python executable is missing");
            Assert.That(File.Exists(entity), Is.True, "the stage 2 entity record is missing");
            Assert.That(File.Exists(calibration), Is.True, "the calibration record is missing");
            Assert.That(Directory.Exists(repo), Is.True, "the repository path is missing");
            string project = Path.GetFullPath(Path.GetDirectoryName(Application.dataPath));
            output = Path.GetFullPath(output);
            string prefix = project.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            Assert.That(output.Equals(project, StringComparison.OrdinalIgnoreCase) ||
                        output.StartsWith(prefix, StringComparison.OrdinalIgnoreCase), Is.False,
                        "recording output must be outside the game project");
            Assert.That(File.Exists(output), Is.False, "recording output must be new");
            Scene loaded = default(Scene);
            System.Diagnostics.Process process = null;
            try
            {
                string scenePath = SceneUtility.GetScenePathByBuildIndex(0);
                Assert.That(scenePath, Is.Not.Null.And.Not.Empty, "an enabled scene is required");
                LogAssert.ignoreFailingMessages = true;
                AsyncOperation loading = SceneManager.LoadSceneAsync(scenePath, LoadSceneMode.Additive);
                Assert.That(loading, Is.Not.Null);
                DateTime loadDeadline = DateTime.UtcNow.AddSeconds(30);
                while (!loading.isDone && DateTime.UtcNow < loadDeadline) yield return null;
                Assert.That(loading.isDone, Is.True, "the scene did not load");
                yield return null;
                loaded = SceneManager.GetSceneByPath(scenePath);
                Assert.That(loaded.IsValid() && loaded.isLoaded, Is.True);
                SceneManager.SetActiveScene(loaded);
                yield return new WaitForSecondsRealtime(1f);

                SceneRuntimeErrors.Start();
                SceneKeyboardState.Start();
                var definitions = new List<BridgeActionDefinition>();
                var seen = new HashSet<string>(StringComparer.Ordinal);
                foreach (Key key in Enum.GetValues(typeof(Key)))
                {
                    if (key == Key.None) continue;
                    string control = key.ToString().ToLowerInvariant();
                    string name = "preflight_key_" + control;
                    if (seen.Add(name)) definitions.Add(new BridgeActionDefinition
                        { Name = name, Control = control, DefaultLeaseS = 0.4 });
                }
                BridgeEditorHost.StartForPreflight(BridgeActionCatalog.FromDefinitions(definitions));
                Assert.That(BridgeEditorHost.IsRunning, Is.True, BridgeEditorHost.LastError);
                // The environment picks the client module and its time limit; the default is the recorder.
                string module = Environment.GetEnvironmentVariable("UPA_BENCH_MODULE");
                if (string.IsNullOrEmpty(module)) module = "playtest.bench.record";
                Assert.That(System.Text.RegularExpressions.Regex.IsMatch(module, "^playtest\\.[A-Za-z0-9_.]+$"), Is.True,
                            "the bench client module name is not valid");
                double timeoutS;
                if (!double.TryParse(Environment.GetEnvironmentVariable("UPA_BENCH_TIMEOUT_S"),
                        System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture,
                        out timeoutS) || timeoutS < 30 || timeoutS > 1800) timeoutS = 240;
                var start = new System.Diagnostics.ProcessStartInfo
                {
                    FileName = python,
                    Arguments = "-m " + module + " --project \"" + project + "\" --entity \"" +
                                entity + "\" --calibration \"" + calibration + "\" --output \"" + output + "\"",
                    WorkingDirectory = repo,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                };
                process = System.Diagnostics.Process.Start(start);
                Assert.That(process, Is.Not.Null);
                DateTime deadline = DateTime.UtcNow.AddSeconds(timeoutS);
                while (!process.HasExited && DateTime.UtcNow < deadline) yield return null;
                Assert.That(process.HasExited, Is.True, "the bench client timed out");
                Assert.That(process.ExitCode, Is.EqualTo(0), "the bench client failed; inspect the local output");
                Assert.That(File.Exists(output), Is.True);
            }
            finally
            {
                if (process != null)
                {
                    if (!process.HasExited) { process.Kill(); process.WaitForExit(5000); }
                    process.Dispose();
                }
                BridgeEditorHost.Stop();
                SceneRuntimeErrors.Stop();
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
    }
}
