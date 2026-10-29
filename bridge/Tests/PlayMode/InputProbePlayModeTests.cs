// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections;
using System.Diagnostics;
using System.IO;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Editor;
using Playtest.Bridge.Editor.Preflight;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;
using Debug = UnityEngine.Debug;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Hosts the bridge for the offline named-declaration input probe. It is a generic harness: it
    /// loads the first enabled scene, starts the bridge with the supplied action catalog (capture
    /// included), runs the Python probe against it and cleans up. It forwards the plan path and
    /// never reads the plan; the plan format belongs to the Python module.
    ///
    /// Environment: <c>UPA_INPUT_PROBE_CATALOG</c> (the action catalog file),
    /// <c>UPA_INPUT_PROBE_PLAN</c> (the plan file), <c>UPA_INPUT_PROBE_OUTPUT</c> (a new file outside
    /// the project), <c>UPA_INPUT_PROBE_PYTHON</c> (the interpreter), <c>UPA_REPO_ROOT</c> (the
    /// repository) and the optional <c>UPA_INPUT_PROBE_TIMEOUT_S</c>. Without
    /// <c>UPA_INPUT_PROBE_OUTPUT</c> the test only checks that an invalid catalog is refused, so an
    /// ordinary PlayMode run is not affected. The probe makes no model calls.
    /// </summary>
    public sealed class InputProbePlayModeTests
    {
        private const double DefaultTimeoutS = 1800;

        [UnityTest]
        public IEnumerator InputProbeRunsTheRequestedPlanOrChecksTheCatalogGuard()
        {
            string output = Environment.GetEnvironmentVariable("UPA_INPUT_PROBE_OUTPUT");
            if (string.IsNullOrEmpty(output))
            {
                Assert.Throws<InvalidDataException>(() => BridgeActionCatalog.Parse("{}", "probe guard"));
                yield break;
            }

            string catalogPath = Environment.GetEnvironmentVariable("UPA_INPUT_PROBE_CATALOG");
            string planPath = Environment.GetEnvironmentVariable("UPA_INPUT_PROBE_PLAN");
            string python = Environment.GetEnvironmentVariable("UPA_INPUT_PROBE_PYTHON");
            string repo = Environment.GetEnvironmentVariable("UPA_REPO_ROOT");
            Assert.That(File.Exists(catalogPath), Is.True, "UPA_INPUT_PROBE_CATALOG must name the action catalog file");
            Assert.That(File.Exists(planPath), Is.True, "UPA_INPUT_PROBE_PLAN must name the plan file");
            Assert.That(File.Exists(python), Is.True, "UPA_INPUT_PROBE_PYTHON must name the Python executable");
            Assert.That(Directory.Exists(repo), Is.True, "UPA_REPO_ROOT must name the repository");

            string project = Path.GetFullPath(Path.GetDirectoryName(Application.dataPath));
            output = OutsideProject(output, project);
            Assert.That(File.Exists(output), Is.False, "the output file must be new");
            BridgeActionCatalog catalog = BridgeActionCatalog.Load(catalogPath);
            Assert.That(catalog.Count, Is.GreaterThan(0), "the catalog declares no actions");

            double timeoutS = DefaultTimeoutS;
            string configured = Environment.GetEnvironmentVariable("UPA_INPUT_PROBE_TIMEOUT_S");
            if (!string.IsNullOrEmpty(configured))
            {
                Assert.That(
                    double.TryParse(configured, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out timeoutS) && timeoutS > 0,
                    Is.True,
                    "UPA_INPUT_PROBE_TIMEOUT_S must be a positive number");
            }

            Scene loaded = default(Scene);
            Process process = null;
            bool monitoring = false;
            try
            {
                string scenePath = SceneUtility.GetScenePathByBuildIndex(0);
                Assert.That(scenePath, Is.Not.Null.And.Not.Empty, "an enabled scene is required");
                yield return Load(scenePath, scene => loaded = scene);
                yield return new WaitForSecondsRealtime(1f);

                SceneRuntimeErrors.Start();
                SceneInputState.Start();
                monitoring = true;

                BridgeEditorHost.StartForPreflight(catalog);
                Assert.That(BridgeEditorHost.IsRunning, Is.True, BridgeEditorHost.LastError);

                var start = new ProcessStartInfo
                {
                    FileName = python,
                    Arguments = "-m playtest.preflight.input_probe --project \"" + project + "\" --plan \"" + planPath
                        + "\" --output \"" + output + "\"",
                    WorkingDirectory = repo,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                };
                process = Process.Start(start);
                Assert.That(process, Is.Not.Null, "the probe process did not start");

                DateTime deadline = DateTime.UtcNow.AddSeconds(timeoutS);
                while (!process.HasExited && DateTime.UtcNow < deadline)
                {
                    yield return null;
                }

                Assert.That(process.HasExited, Is.True, "the input probe timed out");
                Assert.That(process.ExitCode, Is.EqualTo(0), "the input probe failed; inspect its local output");
                Assert.That(File.Exists(output), Is.True, "the input probe wrote no output");
                Debug.Log("[playtest-bridge] input probe finished: " + output);
            }
            finally
            {
                if (process != null)
                {
                    if (!process.HasExited)
                    {
                        process.Kill();
                        process.WaitForExit(5000);
                    }

                    process.Dispose();
                }

                BridgeEditorHost.Stop();
                if (monitoring)
                {
                    SceneInputState.Stop();
                    SceneRuntimeErrors.Stop();
                }

                LogAssert.ignoreFailingMessages = false;
                if (loaded.IsValid() && loaded.isLoaded)
                {
                    for (int i = 0; i < SceneManager.sceneCount; i++)
                    {
                        Scene other = SceneManager.GetSceneAt(i);
                        if (other.IsValid() && other != loaded)
                        {
                            SceneManager.SetActiveScene(other);
                            break;
                        }
                    }

                    SceneManager.UnloadSceneAsync(loaded);
                }
            }
        }

        private static string OutsideProject(string path, string project)
        {
            string full = Path.GetFullPath(path);
            string prefix = project.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            Assert.That(
                full.Equals(project, StringComparison.OrdinalIgnoreCase) || full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase),
                Is.False,
                "the output must be outside the game project");
            return full;
        }

        private static IEnumerator Load(string path, Action<Scene> assign)
        {
            LogAssert.ignoreFailingMessages = true;
            try
            {
                AsyncOperation loading = SceneManager.LoadSceneAsync(path, LoadSceneMode.Additive);
                Assert.That(loading, Is.Not.Null);
                DateTime deadline = DateTime.UtcNow.AddSeconds(30);
                while (!loading.isDone && DateTime.UtcNow < deadline)
                {
                    yield return null;
                }

                Assert.That(loading.isDone, Is.True, "the scene did not load");
                yield return null;
                Scene scene = SceneManager.GetSceneByPath(path);
                Assert.That(scene.IsValid() && scene.isLoaded, Is.True);
                SceneManager.SetActiveScene(scene);
                assign(scene);
            }
            finally
            {
                LogAssert.ignoreFailingMessages = false;
            }
        }
    }
}
