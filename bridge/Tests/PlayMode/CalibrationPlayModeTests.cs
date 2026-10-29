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
using UnityEngine.InputSystem;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>Runs the explicitly requested calibration and records a scene-reload experiment.</summary>
    public sealed class CalibrationPlayModeTests
    {
        // Each trial moves the entity with a held key, then compares it before and after a scene reload.
        private const int ResetTrials = 3;
        private const double ResetHoldS = 1.2;

        [Serializable] private sealed class Candidate { public string control; }
        [Serializable] private sealed class EntityReference
        {
            public string entity_id;
            public string status;
            public Candidate[] candidates;
        }
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
            public ResetTrial[] trials;
        }
        [Serializable] private sealed class ResetTrial
        {
            public string key;
            public double requested_hold_s;
            public double applied_hold_s;
            public bool release_verified;
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
            string movementKey = selected.candidates != null && selected.candidates.Length > 0 ? selected.candidates[0].control : null;
            Assert.That(movementKey, Is.Not.Null.And.Not.Empty, "the stage 2 record names no movement key");
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
                var trials = new List<ResetTrial> { BeginTrial(selected.entity_id, sceneId) };

                SceneRuntimeErrors.Start();
                observingErrors = true;
                SceneKeyboardState.Start();
                var definitions = new List<BridgeActionDefinition>();
                foreach (Key key in Enum.GetValues(typeof(Key)))
                {
                    if (key == Key.None) continue;
                    string control = key.ToString().ToLowerInvariant();
                    string name = "preflight_key_" + control;
                    definitions.Add(new BridgeActionDefinition { Name = name, Control = control, DefaultLeaseS = 0.4 });
                }
                AddInputProbes(definitions, "Mouse", new[] { "delta", "leftButton", "rightButton", "middleButton" });
                AddInputProbes(definitions, "Gamepad", new[]
                {
                    "leftStick", "rightStick", "dpad", "leftTrigger", "rightTrigger", "buttonSouth", "buttonEast",
                    "buttonWest", "buttonNorth", "leftShoulder", "rightShoulder", "leftStickPress", "rightStickPress",
                    "start", "select",
                });
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
                DateTime deadline = DateTime.UtcNow.AddSeconds(420);
                while (!process.HasExited && DateTime.UtcNow < deadline) yield return null;
                Assert.That(process.HasExited, Is.True, "the calibration client timed out");
                Assert.That(process.ExitCode, Is.EqualTo(0), "calibration failed; inspect its local output");
                Assert.That(File.Exists(output), Is.True);
                BridgeEditorHost.Stop();
                LogAssert.ignoreFailingMessages = false;
                SceneRuntimeErrors.Stop();
                SceneKeyboardState.Stop();
                observingErrors = false;
                for (int index = 0; index < ResetTrials; index++)
                {
                    ResetTrial trial = trials[index];
                    trial.key = movementKey;
                    trial.requested_hold_s = ResetHoldS;
                    yield return Displace(movementKey, trial);
                    yield return new WaitForSecondsRealtime(0.75f);
                    trial.before_reload_position = Position(selected.entity_id);
                    trial.before_reload_fields = Fields(sceneId);
                    trial.before_reload_game_s = Time.timeAsDouble;
                    trial.before_reload_real_s = Time.realtimeSinceStartupAsDouble;

                    yield return Reload(loaded, scenePath, scene => loaded = scene);
                    yield return new WaitForSecondsRealtime(1f);
                    trial.after_reload_position = Position(selected.entity_id);
                    trial.after_reload_fields = Fields(sceneId);
                    trial.after_reload_game_s = Time.timeAsDouble;
                    trial.after_reload_real_s = Time.realtimeSinceStartupAsDouble;

                    // The freshly reloaded scene is the next trial's starting state.
                    if (index + 1 < ResetTrials)
                        trials.Add(new ResetTrial
                        {
                            initial_position = trial.after_reload_position,
                            initial_fields = trial.after_reload_fields,
                            initial_game_s = trial.after_reload_game_s,
                            initial_real_s = trial.after_reload_real_s,
                        });
                }
                evidence.trials = trials.ToArray();
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

        private static void AddInputProbes(List<BridgeActionDefinition> definitions, string device, IEnumerable<string> controls)
        {
            foreach (string control in controls)
            {
                double? x = null;
                double? y = null;
                if (device == "Mouse" && control == "delta") { x = 1; y = 0; }
                else if (device == "Gamepad" && (control == "leftStick" || control == "rightStick" || control == "dpad")) { x = 1; y = 0; }
                else if (device == "Gamepad" && (control == "leftTrigger" || control == "rightTrigger")) { x = 1; y = 0; }
                definitions.Add(new BridgeActionDefinition
                {
                    Name = "preflight_" + device.ToLowerInvariant() + "_" + control.ToLowerInvariant(),
                    Inputs = { new BridgeInputBinding(device, control, x, y) },
                    DefaultLeaseS = 0.4,
                });
            }
        }

        private static ResetTrial BeginTrial(string entityId, string sceneId)
        {
            return new ResetTrial
            {
                initial_position = Position(entityId),
                initial_fields = Fields(sceneId),
                initial_game_s = Time.timeAsDouble,
                initial_real_s = Time.realtimeSinceStartupAsDouble,
            };
        }

        // Holds the key through the input backend the bridge uses, then checks that it was released.
        private static IEnumerator Displace(string key, ResetTrial trial)
        {
            var action = new BridgeActionDefinition { Name = "preflight_key_" + key, Control = key, DefaultLeaseS = ResetHoldS };
            using (var input = new InputSystemInputBackend())
            {
                string error;
                Assert.That(input.TryApply(action, out error), Is.True, error);
                double started = Time.realtimeSinceStartupAsDouble;
                while (Time.realtimeSinceStartupAsDouble - started < ResetHoldS) yield return null;
                trial.applied_hold_s = Time.realtimeSinceStartupAsDouble - started;
                bool released;
                Assert.That(input.TryRelease(out released, out error), Is.True, error);
                yield return null;
                trial.release_verified = input.TryVerifyRelease(out error);
                Assert.That(trial.release_verified, Is.True, error);
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
                bool readable = state == "present" && value != null && !Equals(row["value_truncated"], true) &&
                                row["read_error"] == null;
                if (readable && (kind == "number" || kind == "integer" || kind == "boolean" || kind == "enum" || kind == "string"))
                    comparable = true;
                string text = null;
                if (readable && kind != null && VectorKinds.Contains(kind))
                {
                    text = ComponentsText(value);
                    comparable = text != null;
                }
                else
                {
                    if (value is float f && (float.IsNaN(f) || float.IsInfinity(f))) comparable = false;
                    if (value is double d && (double.IsNaN(d) || double.IsInfinity(d))) comparable = false;
                    if (comparable && value != null)
                        text = value is IFormattable formatted ? formatted.ToString(null, CultureInfo.InvariantCulture) : value.ToString();
                }
                result.Add(new FieldSample { id = (string)row["id"], state = state, kind = kind, value = text, comparable = comparable });
            }
            return result.ToArray();
        }

        private static readonly HashSet<string> VectorKinds = new HashSet<string>(StringComparer.Ordinal)
            { "vector2", "vector3", "vector4", "quaternion", "vector2_int", "vector3_int" };

        // Round-trip text for a component array, or null when it is not an array of finite numbers.
        private static string ComponentsText(object value)
        {
            int[] whole = value as int[];
            if (whole != null) return string.Join(",", whole.Select(item => item.ToString(CultureInfo.InvariantCulture)));
            float[] parts = value as float[];
            if (parts == null) return null;
            foreach (float item in parts)
                if (float.IsNaN(item) || float.IsInfinity(item)) return null;
            return string.Join(",", parts.Select(item => item.ToString("R", CultureInfo.InvariantCulture)));
        }
    }
}
