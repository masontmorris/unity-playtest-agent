// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Editor;
using Playtest.Bridge.Editor.Preflight;
using Playtest.Bridge.Unity;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Hosts exactly one manifest action, lets the stage 7 client execute it, then performs
    /// one independent scene reload on request. No action is chosen or substituted here.
    /// </summary>
    public sealed class StageSevenPlayModeTests
    {
        [Serializable] private sealed class ManifestScene
        {
            public string id;
            public string scene_id;
            public string scene_path;
        }
        [Serializable] private sealed class ManifestInput { public string control_id; public string device; public string control; }
        [Serializable] private sealed class ManifestAction
        {
            public string name;
            public ManifestInput[] inputs;
            public double hold_s;
            public double tap_hold_s;
            public bool execution_supported;
        }
        [Serializable] private sealed class ManifestDocument
        {
            public string entity_id;
            public ManifestScene entity;
            public ManifestAction[] actions;
        }
        [Serializable] private sealed class RequestedAction
        {
            public string name;
            public string control;
            public double requested_hold_s;
        }
        [Serializable] private sealed class ReloadRequest { public RequestedAction action; }
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
            public string scene_path;
            public HostAction action;
            public string requested_at_utc;
            public string completed_at_utc;
            public double game_time_s;
            public double real_time_s;
            public int frame;
        }

        [UnityTest]
        public IEnumerator StageSevenVerifiesNamedActionAndReload()
        {
            string output = Environment.GetEnvironmentVariable("UPA_VERIFY_OUTPUT");
            if (string.IsNullOrEmpty(output))
            {
                // Opt-in: the full run needs a wired game and a saved manifest/calibration pair.
                Assert.Throws<ArgumentException>(() => BridgeActionCatalog.FromDefinitions(
                    new[] { new BridgeActionDefinition { Name = "missing_control" } }));
                yield break;
            }
            string python = Environment.GetEnvironmentVariable("UPA_VERIFY_PYTHON");
            string manifestPath = Environment.GetEnvironmentVariable("UPA_VERIFY_MANIFEST");
            string calibrationPath = Environment.GetEnvironmentVariable("UPA_VERIFY_CALIBRATION");
            string actionName = Environment.GetEnvironmentVariable("UPA_VERIFY_ACTION");
            string requestPath = Environment.GetEnvironmentVariable("UPA_VERIFY_RELOAD_REQUEST");
            string donePath = Environment.GetEnvironmentVariable("UPA_VERIFY_RELOAD_DONE");
            string repo = Environment.GetEnvironmentVariable("UPA_REPO_ROOT");
            Assert.That(File.Exists(python), Is.True, "the Python executable is missing");
            Assert.That(File.Exists(manifestPath), Is.True, "the preflight manifest is missing");
            Assert.That(File.Exists(calibrationPath), Is.True, "the calibration record is missing");
            Assert.That(Directory.Exists(repo), Is.True, "the repository path is missing");
            Assert.That(string.IsNullOrEmpty(actionName), Is.False, "the action to verify is missing");
            Assert.That(string.IsNullOrEmpty(requestPath), Is.False, "the reload request path is missing");
            Assert.That(string.IsNullOrEmpty(donePath), Is.False, "the reload done path is missing");

            string project = Path.GetFullPath(Path.GetDirectoryName(Application.dataPath));
            output = OutsideProject(output, project);
            requestPath = OutsideProject(requestPath, project);
            donePath = OutsideProject(donePath, project);
            Assert.That(File.Exists(output) || File.Exists(requestPath) || File.Exists(donePath), Is.False,
                        "stage 7 outputs and handshake files must be new");

            ManifestDocument document = JsonUtility.FromJson<ManifestDocument>(File.ReadAllText(manifestPath));
            Assert.That(document, Is.Not.Null);
            Assert.That(document.entity_id, Is.Not.Null.And.Not.Empty);
            Assert.That(document.entity, Is.Not.Null);
            Assert.That(document.entity.scene_path, Is.Not.Null.And.Not.Empty);
            Assert.That(document.actions, Is.Not.Null);
            ManifestAction[] matches = document.actions.Where(item => item != null && item.name == actionName).ToArray();
            Assert.That(matches.Length, Is.EqualTo(1), "the requested name must match exactly one manifest action");
            ManifestAction action = matches[0];
            Assert.That(action.inputs, Is.Not.Null);
            Assert.That(action.inputs.Length, Is.EqualTo(1), "stage 7 hosts exactly one button");
            // This caller-named preflight measurement may establish evidence for an unproven
            // declaration. It does not offer that declaration as an S1 choice.
            Assert.That(action.inputs[0].device, Is.EqualTo("Keyboard"), "stage 7 measures one keyboard button");
            string control = action.inputs[0].control;
            Assert.That(string.IsNullOrEmpty(control), Is.False, "the selected action names no physical control");
            double lease = action.hold_s > 0 ? action.hold_s : action.tap_hold_s;
            Assert.That(lease, Is.GreaterThan(0).And.LessThanOrEqualTo(3600),
                        "the selected action has no tested requested hold");

            double timeoutS;
            if (!double.TryParse(Environment.GetEnvironmentVariable("UPA_VERIFY_TIMEOUT_S"),
                    NumberStyles.Float, CultureInfo.InvariantCulture, out timeoutS) || timeoutS < 30 || timeoutS > 4200)
            {
                // A long configured hold must not be cut off by the client's watchdog.
                timeoutS = Math.Min(4200, Math.Max(420, lease + 300));
            }

            Scene loaded = default(Scene);
            System.Diagnostics.Process process = null;
            bool observingErrors = false;
            try
            {
                string scenePath = SceneUtility.GetScenePathByBuildIndex(0);
                Assert.That(scenePath, Is.Not.Null.And.Not.Empty, "an enabled scene is required");
                Assert.That(string.Equals(scenePath, document.entity.scene_path, StringComparison.OrdinalIgnoreCase),
                            Is.True, "the calibrated entity's scene must be the enabled scene");
                yield return Load(scenePath, scene => loaded = scene);
                yield return new WaitForSecondsRealtime(1f);

                SceneRuntimeErrors.Start();
                observingErrors = true;
                SceneKeyboardState.Start();
                LogAssert.ignoreFailingMessages = true;
                // The one-entry catalog is the exact name, physical control and lease from the manifest.
                BridgeEditorHost.StartForPreflight(BridgeActionCatalog.FromDefinitions(new[]
                {
                    new BridgeActionDefinition { Name = action.name, Control = control, DefaultLeaseS = lease },
                }));
                Assert.That(BridgeEditorHost.IsRunning, Is.True, BridgeEditorHost.LastError);

                var start = new System.Diagnostics.ProcessStartInfo
                {
                    FileName = python,
                    Arguments = "-m playtest.preflight.verify --project \"" + project + "\" --manifest \"" +
                                manifestPath + "\" --calibration \"" + calibrationPath + "\" --action \"" +
                                actionName + "\" --output \"" + output + "\" --reload-request \"" +
                                requestPath + "\" --reload-done \"" + donePath + "\"",
                    WorkingDirectory = repo,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                };
                process = System.Diagnostics.Process.Start(start);
                Assert.That(process, Is.Not.Null);

                DateTime deadline = DateTime.UtcNow.AddSeconds(timeoutS);
                bool reloaded = false;
                while (!process.HasExited && DateTime.UtcNow < deadline)
                {
                    if (!reloaded && File.Exists(requestPath))
                    {
                        ReloadRequest request = JsonUtility.FromJson<ReloadRequest>(File.ReadAllText(requestPath));
                        Assert.That(request, Is.Not.Null);
                        Assert.That(request.action, Is.Not.Null, "the reload request names no action");
                        Assert.That(request.action.name, Is.EqualTo(action.name));
                        Assert.That(request.action.control, Is.EqualTo(control));
                        Assert.That(request.action.requested_hold_s, Is.EqualTo(lease).Within(0.000001),
                                    "the client's lease differs from the hosted manifest action");
                        string requestedAt = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture);
                        yield return new WaitForSecondsRealtime(0.1f);
                        yield return Reload(loaded, scenePath, scene => loaded = scene);
                        LogAssert.ignoreFailingMessages = true;
                        yield return new WaitForSecondsRealtime(1f);
                        var done = new ReloadDone
                        {
                            reloaded = true,
                            scene_path = scenePath,
                            action = new HostAction { name = action.name, control = control, default_lease_s = lease },
                            requested_at_utc = requestedAt,
                            completed_at_utc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
                            game_time_s = Time.timeAsDouble,
                            real_time_s = Time.realtimeSinceStartupAsDouble,
                            frame = Time.frameCount,
                        };
                        string directory = Path.GetDirectoryName(donePath);
                        if (!string.IsNullOrEmpty(directory)) Directory.CreateDirectory(directory);
                        string writingPath = donePath + ".writing";
                        using (var writer = new StreamWriter(new FileStream(writingPath, FileMode.CreateNew, FileAccess.Write)))
                        {
                            writer.WriteLine(JsonUtility.ToJson(done, true));
                        }
                        File.Move(writingPath, donePath);
                        reloaded = true;
                    }
                    yield return null;
                }
                Assert.That(process.HasExited, Is.True, "the stage 7 client timed out");
                Assert.That(process.ExitCode, Is.EqualTo(0), "stage 7 verification failed; inspect its local report");
                Assert.That(reloaded, Is.True, "the stage 7 client never requested the scene reload");
                Assert.That(File.Exists(output), Is.True, "the stage 7 report was not written");
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
            try
            {
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
            finally { LogAssert.ignoreFailingMessages = false; }
        }

        private static string OutsideProject(string path, string project)
        {
            Assert.That(string.IsNullOrEmpty(path), Is.False);
            string full = Path.GetFullPath(path);
            string prefix = project.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            Assert.That(full.Equals(project, StringComparison.OrdinalIgnoreCase) ||
                        full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase), Is.False,
                        "stage 7 output and handshake files must be outside the game project");
            return full;
        }
    }
}
