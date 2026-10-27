using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Editor;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>Runs the external preflight client against a loaded scene and the real bridge.</summary>
    public sealed class ControlledEntityPlayModeTests
    {
        [UnityTest]
        public IEnumerator PreflightEntryPointValidatesCatalogOrRunsRequestedMeasurement()
        {
            string output = Environment.GetEnvironmentVariable("UPA_ENTITY_OUTPUT");
            string python = Environment.GetEnvironmentVariable("UPA_ENTITY_PYTHON");
            string repo = Environment.GetEnvironmentVariable("UPA_REPO_ROOT");
            if (string.IsNullOrEmpty(output))
            {
                // The active probe is opt-in. The default suite still verifies the host boundary
                // rather than reporting a skipped suite or pretending to have measured the game.
                Assert.Throws<ArgumentNullException>(() => BridgeEditorHost.StartForPreflight(null));
                yield break;
            }
            Assert.That(File.Exists(python), Is.True, "UPA_ENTITY_PYTHON must name the repository Python executable");
            Assert.That(Directory.Exists(repo), Is.True, "UPA_REPO_ROOT must name the repository");
            string project = Path.GetFullPath(Path.GetDirectoryName(Application.dataPath));
            output = Path.GetFullPath(output);
            string prefix = project.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            Assert.That(output.StartsWith(prefix, StringComparison.OrdinalIgnoreCase), Is.False,
                "the measurement output must be outside the game project");
            Assert.That(File.Exists(output), Is.False, "measurement output must be a new file");

            Scene loaded = default(Scene);
            System.Diagnostics.Process process = null;
            try
            {
                string scenePath = SceneUtility.GetScenePathByBuildIndex(0);
                Assert.That(scenePath, Is.Not.Null.And.Not.Empty, "an enabled build scene is required");
                LogAssert.ignoreFailingMessages = true;
                try
                {
                    AsyncOperation loading = SceneManager.LoadSceneAsync(scenePath, LoadSceneMode.Additive);
                    Assert.That(loading, Is.Not.Null);
                    DateTime deadline = DateTime.UtcNow.AddSeconds(30);
                    while (!loading.isDone && DateTime.UtcNow < deadline) yield return null;
                    Assert.That(loading.isDone, Is.True, "the scene did not load");
                    yield return null;
                    loaded = SceneManager.GetSceneByPath(scenePath);
                    Assert.That(loaded.IsValid() && loaded.isLoaded, Is.True);
                    SceneManager.SetActiveScene(loaded);
                }
                finally { LogAssert.ignoreFailingMessages = false; }

                var definitions = new List<BridgeActionDefinition>();
                var added = new HashSet<string>(StringComparer.Ordinal);
                foreach (Key key in Enum.GetValues(typeof(Key)))
                {
                    if (key == Key.None) continue;
                    string control = key.ToString().ToLowerInvariant();
                    string name = "preflight_key_" + control;
                    if (!added.Add(name)) continue;
                    definitions.Add(new BridgeActionDefinition { Name = name, Control = control, DefaultLeaseS = 0.4 });
                }
                BridgeEditorHost.StartForPreflight(BridgeActionCatalog.FromDefinitions(definitions));
                Assert.That(BridgeEditorHost.IsRunning, Is.True, BridgeEditorHost.LastError);
                var start = new System.Diagnostics.ProcessStartInfo
                {
                    FileName = python,
                    Arguments = "-m playtest.preflight.entity --project \"" + project + "\" --output \"" + output + "\"",
                    WorkingDirectory = repo,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                };
                process = System.Diagnostics.Process.Start(start);
                Assert.That(process, Is.Not.Null, "the measurement client could not start");
                DateTime measurementDeadline = DateTime.UtcNow.AddSeconds(300);
                while (!process.HasExited && DateTime.UtcNow < measurementDeadline) yield return null;
                Assert.That(process.HasExited, Is.True, "the measurement client timed out");
                Assert.That(process.ExitCode, Is.EqualTo(0), "measurement was inconclusive; inspect the local output");
                Assert.That(File.Exists(output), Is.True, "the measurement client did not write its evidence");
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
