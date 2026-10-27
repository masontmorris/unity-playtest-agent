using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
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
    /// <summary>Runs the explicitly requested calibration and records a scene-reload experiment.</summary>
    public sealed class CalibrationPlayModeTests
    {
        [Serializable] private sealed class EntityReference { public string entity_id; public string status; }
        [Serializable] private sealed class FieldSample
        {
            public string id;
            public string state;
            public string kind;
            public string value;
            public bool comparable;
        }
        [Serializable] private sealed class ResetEvidence
        {
            public string method = "scene_reload";
            public string scene_path;
            public string entity_id;
            public float[] initial_position;
            public float[] before_reload_position;
            public float[] after_reload_position;
            public double initial_game_s;
            public double before_reload_game_s;
            public double after_reload_game_s;
            public double initial_real_s;
            public double before_reload_real_s;
            public double after_reload_real_s;
            public FieldSample[] initial_fields;
            public FieldSample[] before_reload_fields;
            public FieldSample[] after_reload_fields;
        }

        [UnityTest]
        public IEnumerator CalibrationEntryPointChecksBoundsOrRunsRequestedProbe()
        {
            string output = Environment.GetEnvironmentVariable("UPA_CALIBRATION_OUTPUT");
            if (string.IsNullOrEmpty(output))
            {
                Assert.Throws<ArgumentOutOfRangeException>(() => SceneTransformCollector.Collect(0));
                yield break;
            }
            string python = Environment.GetEnvironmentVariable("UPA_CALIBRATION_PYTHON");
            string entityPath = Environment.GetEnvironmentVariable("UPA_CALIBRATION_ENTITY");
            string resetPath = Environment.GetEnvironmentVariable("UPA_CALIBRATION_RESET_OUTPUT");
            string repo = Environment.GetEnvironmentVariable("UPA_REPO_ROOT");
            Assert.That(File.Exists(python), Is.True, "the calibration Python executable is missing");
            Assert.That(File.Exists(entityPath), Is.True, "a stage 2 entity record is required");
            Assert.That(Directory.Exists(repo), Is.True, "the repository path is missing");
            string project = Path.GetFullPath(Path.GetDirectoryName(Application.dataPath));
            output = OutsideProject(output, project);
            resetPath = OutsideProject(resetPath, project);
            Assert.That(File.Exists(output) || File.Exists(resetPath), Is.False, "output files must be new");
            EntityReference selected = JsonUtility.FromJson<EntityReference>(File.ReadAllText(entityPath));
            Assert.That(selected, Is.Not.Null);
            Assert.That(selected.status, Is.EqualTo("identified"));
            Assert.That(selected.entity_id, Is.Not.Null.And.Not.Empty);
            int separator = selected.entity_id.IndexOf("::", StringComparison.Ordinal);
            Assert.That(separator, Is.GreaterThan(0));
            string sceneId = selected.entity_id.Substring(0, separator);

            Scene loaded = default(Scene);
            System.Diagnostics.Process process = null;
            bool observingErrors = false;
            try
            {
                string scenePath = SceneUtility.GetScenePathByBuildIndex(0);
                Assert.That(scenePath, Is.Not.Null.And.Not.Empty, "an enabled scene is required");
                yield return Load(scenePath, scene => loaded = scene);
                yield return new WaitForSecondsRealtime(1f);
                var evidence = new ResetEvidence { scene_path = scenePath, entity_id = selected.entity_id };
                evidence.initial_position = Position(selected.entity_id);
                evidence.initial_fields = Fields(sceneId);
                evidence.initial_game_s = Time.timeAsDouble;
                evidence.initial_real_s = Time.realtimeSinceStartupAsDouble;

                SceneRuntimeErrors.Start();
                observingErrors = true;
                SceneKeyboardState.Start();
                LogAssert.ignoreFailingMessages = true;

                var definitions = new List<BridgeActionDefinition>();
                var added = new HashSet<string>(StringComparer.Ordinal);
                foreach (Key key in Enum.GetValues(typeof(Key)))
                {
                    if (key == Key.None) continue;
                    string control = key.ToString().ToLowerInvariant();
                    string name = "preflight_key_" + control;
                    if (added.Add(name)) definitions.Add(new BridgeActionDefinition { Name = name, Control = control, DefaultLeaseS = 0.4 });
                }
                BridgeEditorHost.StartForPreflight(BridgeActionCatalog.FromDefinitions(definitions));
                Assert.That(BridgeEditorHost.IsRunning, Is.True, BridgeEditorHost.LastError);
                var start = new System.Diagnostics.ProcessStartInfo
                {
                    FileName = python,
                    Arguments = "-m playtest.preflight.calibrate --project \"" + project + "\" --entity \"" +
                                entityPath + "\" --output \"" + output + "\"",
                    WorkingDirectory = repo,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                };
                process = System.Diagnostics.Process.Start(start);
                Assert.That(process, Is.Not.Null);
                DateTime deadline = DateTime.UtcNow.AddSeconds(360);
                while (!process.HasExited && DateTime.UtcNow < deadline) yield return null;
                Assert.That(process.HasExited, Is.True, "the calibration client timed out");
                Assert.That(process.ExitCode, Is.EqualTo(0), "calibration failed; inspect its local output");
                Assert.That(File.Exists(output), Is.True);
                BridgeEditorHost.Stop();
                LogAssert.ignoreFailingMessages = false;
                SceneRuntimeErrors.Stop();
                SceneKeyboardState.Stop();
                observingErrors = false;
                yield return new WaitForSecondsRealtime(0.75f);
                evidence.before_reload_position = Position(selected.entity_id);
                evidence.before_reload_fields = Fields(sceneId);
                evidence.before_reload_game_s = Time.timeAsDouble;
                evidence.before_reload_real_s = Time.realtimeSinceStartupAsDouble;

                for (int i = 0; i < SceneManager.sceneCount; i++)
                {
                    Scene other = SceneManager.GetSceneAt(i);
                    if (other.IsValid() && other != loaded) { SceneManager.SetActiveScene(other); break; }
                }
                AsyncOperation unloading = SceneManager.UnloadSceneAsync(loaded);
                Assert.That(unloading, Is.Not.Null);
                deadline = DateTime.UtcNow.AddSeconds(30);
                while (!unloading.isDone && DateTime.UtcNow < deadline) yield return null;
                Assert.That(unloading.isDone, Is.True, "the scene did not unload");
                loaded = default(Scene);
                yield return Load(scenePath, scene => loaded = scene);
                yield return new WaitForSecondsRealtime(1f);
                evidence.after_reload_position = Position(selected.entity_id);
                evidence.after_reload_fields = Fields(sceneId);
                evidence.after_reload_game_s = Time.timeAsDouble;
                evidence.after_reload_real_s = Time.realtimeSinceStartupAsDouble;
                Directory.CreateDirectory(Path.GetDirectoryName(resetPath));
                using (var writer = new StreamWriter(new FileStream(resetPath, FileMode.CreateNew, FileAccess.Write)))
                    writer.WriteLine(JsonUtility.ToJson(evidence, true));
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

        private static string OutsideProject(string path, string project)
        {
            Assert.That(path, Is.Not.Null.And.Not.Empty);
            string full = Path.GetFullPath(path);
            string prefix = project.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            Assert.That(full.Equals(project, StringComparison.OrdinalIgnoreCase) ||
                        full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase), Is.False,
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

        private static float[] Position(string objectId)
        {
            InspectionDocument snapshot = SceneTransformCollector.Collect(SceneTransformCollector.ObjectLimit);
            Assert.That(snapshot.Errors, Is.Empty);
            Assert.That(snapshot.Caps["game_objects"].Truncated, Is.False);
            foreach (var item in (List<object>)snapshot.Data["game_objects"])
            {
                var row = (Dictionary<string, object>)item;
                if ((string)row["id"] == objectId)
                {
                    Assert.That(row["position_state"], Is.EqualTo("present"));
                    return (float[])row["position"];
                }
            }
            Assert.Fail("the entity no longer resolves: " + objectId);
            return null;
        }

        private static FieldSample[] Fields(string sceneId)
        {
            SceneDiscoveryResult snapshot = SceneDiscoveryCollector.Collect(4096, 30000, 10000);
            Assert.That(snapshot.Errors, Is.Empty);
            Assert.That(snapshot.Caps["fields"].Truncated, Is.False);
            var result = new List<FieldSample>();
            foreach (var item in (List<object>)snapshot.Data["fields"])
            {
                var row = (Dictionary<string, object>)item;
                if (!Equals(row["scene_id"], sceneId)) continue;
                string state = row["value_state"] as string;
                string kind = row["value_kind"] as string;
                bool comparable = state == "null" && Equals(row["is_null"], true);
                object value = row["value"];
                if (state == "present" && (kind == "number" || kind == "integer" || kind == "boolean" || kind == "enum" || kind == "string") &&
                    value != null && !Equals(row["value_truncated"], true) && row["read_error"] == null)
                    comparable = true;
                if (value is float f && (float.IsNaN(f) || float.IsInfinity(f))) comparable = false;
                if (value is double d && (double.IsNaN(d) || double.IsInfinity(d))) comparable = false;
                string text = null;
                if (comparable && value != null)
                    text = value is IFormattable formatted ? formatted.ToString(null, CultureInfo.InvariantCulture) : value.ToString();
                result.Add(new FieldSample { id = (string)row["id"], state = state, kind = kind, value = text, comparable = comparable });
            }
            return result.ToArray();
        }
    }
}
