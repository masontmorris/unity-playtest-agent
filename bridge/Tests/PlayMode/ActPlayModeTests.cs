// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;
using Playtest.Bridge.Unity;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.TestTools;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Acceptance measurement for <c>act</c> against a real running project. The first enabled
    /// <see cref="CharacterController"/> in the loaded scene is the engine-generic body under
    /// observation: the test reports how far it moved for a completed action, that a stale
    /// observation is rejected without moving it, and that a new lease supersedes a held one and
    /// releases both. The test executes only the actions its action catalog names, chooses nothing
    /// and sends no <c>stop</c>.
    /// </summary>
    [TestFixture]
    public sealed class ActPlayModeTests
    {
        private const string SessionId = "s-playmode-act";

        // The catalog is project data, written for this run and deleted afterwards. The names are
        // generic directions and keyboard keys; no game control or object name is compiled in.
        private const string ForwardAction = "move_forward";
        private const string BackAction = "move_back";
        private const string ForwardControl = "w";
        private const string BackControl = "s";
        private const double DefaultLeaseS = 0.4;

        private const double HeldLeaseS = 2.0;
        private const double ReplaceLeaseS = 0.3;

        private const double FreshMaxAgeS = 1.0;
        private const double StaleMaxAgeS = 0.01;

        // The stale act must be older than its max_age_s before it is sent, and the window that
        // follows the rejection is long enough that a wrongly applied lease would show movement.
        private const double StaleDelaySeconds = 0.1;
        private const double StaleWindowSeconds = 0.2;

        private const double HeldObservationSeconds = 0.2;

        // The release is a queued Input System state event, applied on the next input update, so a
        // lease can move the body for one frame after its terminal record. The reference position
        // for the quiet-window checks is therefore taken after that boundary.
        private const double ReleaseSettleSeconds = 0.1;
        private const double PostLeaseSettleSeconds = 0.3;
        private const double StabilitySeconds = 0.3;

        /// <summary>Movement the acceptance asks for; at or below it the body counts as not moved.</summary>
        private const float MovementThresholdM = 0.05f;

        private const float StillToleranceM = 0.002f;
        private const double StillQuietSeconds = 0.2;
        private const double StillTimeoutSeconds = 5.0;

        private const double SceneSettleSeconds = 0.5;
        private const double SceneLoadTimeoutSeconds = 30.0;
        private const double ReplyTimeoutSeconds = 10.0;
        private const double RunningRecordTimeoutSeconds = 5.0;
        private const double TerminalRecordTimeoutSeconds = 15.0;

        [UnityTest]
        public IEnumerator Act_MovesSupersedesHeldInputAndRejectsStaleObservation()
        {
            BridgeServer server = null;
            ActClient client = null;
            InputSystemInputBackend input = null;
            UnityObservationSource observationSource = null;
            Scene loadedScene = default(Scene);
            string catalogPath = null;
            try
            {
                string scenePath = FirstEnabledScenePath();

                // The scene is the project under test. A graphics-less batch run can log errors from
                // its renderers or shaders, which are not this test's subject; only loading is
                // exempt, and every bridge assertion below still runs with normal log handling.
                LogAssert.ignoreFailingMessages = true;
                try
                {
                    AsyncOperation loading = SceneManager.LoadSceneAsync(scenePath, LoadSceneMode.Additive);
                    Assert.That(loading, Is.Not.Null, "SceneManager refused to load " + scenePath);
                    DateTime loadDeadline = DateTime.UtcNow.AddSeconds(SceneLoadTimeoutSeconds);
                    while (!loading.isDone && DateTime.UtcNow < loadDeadline)
                    {
                        yield return null;
                    }

                    Assert.That(
                        loading.isDone,
                        Is.True,
                        $"the scene did not finish loading within {SceneLoadTimeoutSeconds:0} s: {scenePath}");

                    // One frame so the scene's own Start callbacks have run.
                    yield return null;
                    loadedScene = FindLoadedScene(scenePath);
                    Assert.That(loadedScene.IsValid(), Is.True, "the loaded scene is not known to the scene manager: " + scenePath);
                    Assert.That(loadedScene.isLoaded, Is.True, "the scene is not loaded: " + scenePath);
                    SceneManager.SetActiveScene(loadedScene);
                }
                finally
                {
                    LogAssert.ignoreFailingMessages = false;
                }

                DateTime settleDeadline = DateTime.UtcNow.AddSeconds(SceneSettleSeconds);
                while (DateTime.UtcNow < settleDeadline)
                {
                    yield return null;
                }

                CharacterController body = FindFirstEnabledCharacterController(loadedScene);
                Assert.That(
                    body,
                    Is.Not.Null,
                    "the loaded scene has no enabled CharacterController; the act measurement needs an engine-generic body to observe");

                // The baseline must be at rest, or movement could be attributed to the act when it
                // was still the scene settling.
                bool still = false;
                float largestStepM = 0f;
                yield return WaitForStill(
                    body,
                    StillToleranceM,
                    StillQuietSeconds,
                    StillTimeoutSeconds,
                    (rested, step) =>
                    {
                        still = rested;
                        largestStepM = step;
                    });

                Assert.That(
                    still,
                    Is.True,
                    $"the CharacterController never came to rest before the measurement; largest sampled move was {largestStepM.ToString("0.000", CultureInfo.InvariantCulture)} m");

                catalogPath = ActionCatalogPath();
                Directory.CreateDirectory(Path.GetDirectoryName(catalogPath));
                File.WriteAllText(catalogPath, ActionCatalogJson(), new UTF8Encoding(false));

                BridgeActionCatalog catalog = BridgeActionCatalog.Load(catalogPath);
                Assert.That(catalog.Count, Is.EqualTo(2), "the test action catalog did not load two actions: " + catalogPath);
                Assert.That(
                    catalog.TryGet(ForwardAction, out BridgeActionDefinition forwardDefinition),
                    Is.True,
                    "the test action catalog has no '" + ForwardAction + "' entry: " + catalogPath);
                Assert.That(forwardDefinition.Backend, Is.EqualTo("input_system"), "'" + ForwardAction + "' must use the input_system backend");
                Assert.That(forwardDefinition.Device, Is.EqualTo("Keyboard"), "'" + ForwardAction + "' must bind a Keyboard device");
                Assert.That(forwardDefinition.Control, Is.EqualTo(ForwardControl), "'" + ForwardAction + "' must bind '" + ForwardControl + "'");
                Assert.That(forwardDefinition.DefaultLeaseS, Is.EqualTo(DefaultLeaseS).Within(0.0001), "'" + ForwardAction + "' has the wrong default lease");
                Assert.That(
                    catalog.TryGet(BackAction, out BridgeActionDefinition backDefinition),
                    Is.True,
                    "the test action catalog has no '" + BackAction + "' entry: " + catalogPath);
                Assert.That(backDefinition.Control, Is.EqualTo(BackControl), "'" + BackAction + "' must bind '" + BackControl + "'");
                Assert.That(backDefinition.DefaultLeaseS, Is.EqualTo(DefaultLeaseS).Within(0.0001), "'" + BackAction + "' has the wrong default lease");

                string token = BridgeToken.Create();
                var statusSource = new UnityStatusSource(CurrentProcessId());
                observationSource = new UnityObservationSource();
                observationSource.Start();
                input = new InputSystemInputBackend();
                Assert.That(
                    input.IsAvailable,
                    Is.True,
                    "the Input System backend reports unavailable; a real keyboard state cannot be applied in this project");

                server = new BridgeServer(
                    new BridgeServerOptions
                    {
                        Port = 0,
                        HandshakeTimeoutMs = 5000,
                        ReadTimeoutMs = 0,
                    },
                    statusSource,
                    observationSource,
                    catalog,
                    input,
                    token);
                server.Log = message => Debug.Log("[playtest-bridge] " + message);
                server.Start();

                // The reader runs on its own thread so this coroutine yields every frame and the
                // Input System keeps updating while the lease runs.
                client = new ActClient(server.Port);
                client.Send(new HelloMessage
                {
                    Protocol = ProtocolVersion.Current,
                    Session = SessionId,
                    Seq = client.NextRequestSeq(),
                    GameS = null,
                    WallUtc = DateTime.UtcNow,
                    Token = token,
                    CoreVersion = "0.0.1",
                });

                DateTime handshakeDeadline = DateTime.UtcNow.AddSeconds(ReplyTimeoutSeconds);
                while (client.Status == null && DateTime.UtcNow < handshakeDeadline)
                {
                    yield return null;
                }

                Assert.That(client.Status, Is.Not.Null, "no status reply before the handshake timeout; " + client.Describe());
                Assert.That(client.Status.Session, Is.EqualTo(SessionId), "the status reply belongs to another session");
                Assert.That(
                    client.Status.InputBackends,
                    Does.Contain("input_system"),
                    "the bridge does not report the input_system backend; act cannot be measured; " + client.Describe());

                // Observations are only stored for act freshness after they are sent, so the first
                // seq the acts can bind to comes from an explicit observe.
                ObservationMessage firstObservation = null;
                yield return ObserveOnce(client, observation => firstObservation = observation);
                AssertPlaying(firstObservation, client, "before a-forward");

                Vector3 forwardStart = body.transform.position;
                client.Send(Act(client, "a-forward", "c-forward", ForwardAction, firstObservation.Seq, FreshMaxAgeS, DefaultLeaseS));

                ExecutionRecordMessage[] forwardRecords = null;
                yield return WaitForRecords(
                    client,
                    new[] { new RecordExpectation("a-forward", BridgeExecStatus.Terminal) },
                    TerminalRecordTimeoutSeconds,
                    records => forwardRecords = records);

                ExecutionRecordMessage forward = forwardRecords[0];
                Assert.That(forward, Is.Not.Null, "no terminal record for a-forward; " + client.Describe());
                Assert.That(forward.Status, Is.EqualTo(BridgeExecStatus.Completed), "a-forward did not complete: " + Describe(forward) + "; " + client.Describe());
                Assert.That(forward.ReleaseVerified, Is.True, "a-forward completed without a verified release: " + Describe(forward));
                Assert.That(
                    forward.ValidatedAgainstSeq,
                    Is.EqualTo(firstObservation.Seq),
                    "a-forward was not validated against the observation it was built from: " + Describe(forward));
                Assert.That(forward.AppliedDurationS, Is.Not.Null, "the completion record carries no measured duration: " + Describe(forward));
                Assert.That(forward.AppliedDurationS.Value, Is.GreaterThan(0.0), "the completion record reports a non-positive duration: " + Describe(forward));

                yield return WaitSeconds(PostLeaseSettleSeconds);
                float forwardDistanceM = Vector3.Distance(forwardStart, body.transform.position);
                Assert.That(
                    forwardDistanceM,
                    Is.GreaterThan(MovementThresholdM),
                    $"the body did not move more than {MovementThresholdM.ToString("0.000", CultureInfo.InvariantCulture)} m for a completed action; " +
                    "it moved " + forwardDistanceM.ToString("0.000", CultureInfo.InvariantCulture) + " m; " + Describe(forward) + "; " + client.Describe());

                // The controlled body must be at rest again before the stale act's baseline is taken.
                yield return WaitForStill(
                    body,
                    StillToleranceM,
                    StillQuietSeconds,
                    StillTimeoutSeconds,
                    (rested, step) =>
                    {
                        still = rested;
                        largestStepM = step;
                    });
                Assert.That(
                    still,
                    Is.True,
                    $"the CharacterController did not stop after a-forward; largest sampled move was {largestStepM.ToString("0.000", CultureInfo.InvariantCulture)} m");

                ObservationMessage staleObservation = null;
                yield return ObserveOnce(client, observation => staleObservation = observation);
                AssertPlaying(staleObservation, client, "before a-stale");

                yield return WaitSeconds(StaleDelaySeconds);

                Vector3 staleStart = body.transform.position;
                client.Send(Act(client, "a-stale", "c-stale", ForwardAction, staleObservation.Seq, StaleMaxAgeS, DefaultLeaseS));
                ExecutionRecordMessage[] staleRecords = null;
                yield return WaitForRecords(
                    client,
                    new[] { new RecordExpectation("a-stale", BridgeExecStatus.Terminal) },
                    TerminalRecordTimeoutSeconds,
                    records => staleRecords = records);

                ExecutionRecordMessage stale = staleRecords[0];
                Assert.That(stale, Is.Not.Null, "no terminal record for a-stale; " + client.Describe());
                Assert.That(stale.Status, Is.EqualTo(BridgeExecStatus.Rejected), "a-stale was not rejected: " + Describe(stale) + "; " + client.Describe());
                Assert.That(
                    stale.RejectReason,
                    Is.EqualTo(BridgeRejectReason.StaleAge),
                    "a-stale was rejected for the wrong reason: " + Describe(stale));
                Assert.That(
                    stale.ValidatedAgainstSeq,
                    Is.EqualTo(staleObservation.Seq),
                    "a-stale was not checked against the observation it named: " + Describe(stale));

                yield return WaitSeconds(StaleWindowSeconds);
                float staleDeltaM = Vector3.Distance(staleStart, body.transform.position);
                Assert.That(
                    staleDeltaM,
                    Is.LessThan(MovementThresholdM),
                    $"a rejected stale act moved the body {staleDeltaM.ToString("0.000", CultureInfo.InvariantCulture)} m; " +
                    "it must not apply input; " + Describe(stale) + "; " + client.Describe());

                // Held lease: apply a long lease, confirm movement, then supersede it.
                Vector3 heldStart = body.transform.position;
                ObservationMessage heldObservation = null;
                yield return ObserveOnce(client, observation => heldObservation = observation);
                AssertPlaying(heldObservation, client, "before a-held");

                client.Send(Act(client, "a-held", "c-held", ForwardAction, heldObservation.Seq, FreshMaxAgeS, HeldLeaseS));
                ExecutionRecordMessage[] heldStartRecords = null;
                yield return WaitForRecords(
                    client,
                    new[] { new RecordExpectation("a-held", new[] { BridgeExecStatus.Running }) },
                    RunningRecordTimeoutSeconds,
                    records => heldStartRecords = records);

                ExecutionRecordMessage heldRunning = heldStartRecords[0];
                Assert.That(heldRunning, Is.Not.Null, "a-held never reported that it was running; " + client.Describe());
                Assert.That(heldRunning.Status, Is.EqualTo(BridgeExecStatus.Running), "a-held did not start: " + Describe(heldRunning));

                yield return WaitSeconds(HeldObservationSeconds);

                ObservationMessage replaceObservation = null;
                yield return ObserveOnce(client, observation => replaceObservation = observation);
                AssertPlaying(replaceObservation, client, "before a-replace");

                Vector3 heldEnd = body.transform.position;
                Vector3 heldDelta = heldEnd - heldStart;
                Assert.That(
                    heldDelta.magnitude,
                    Is.GreaterThan(MovementThresholdM),
                    "the body did not move while a-held was running; it moved " +
                    heldDelta.magnitude.ToString("0.000", CultureInfo.InvariantCulture) + " m; " + Describe(heldRunning) + "; " + client.Describe());

                client.Send(Act(client, "a-replace", "c-replace", BackAction, replaceObservation.Seq, FreshMaxAgeS, ReplaceLeaseS));
                ExecutionRecordMessage[] replaceRecords = null;
                yield return WaitForRecords(
                    client,
                    new[]
                    {
                        new RecordExpectation("a-held", new[] { BridgeExecStatus.Cancelled }),
                        new RecordExpectation("a-replace", BridgeExecStatus.Terminal),
                    },
                    TerminalRecordTimeoutSeconds,
                    records => replaceRecords = records);

                ExecutionRecordMessage heldCancelled = replaceRecords[0];
                ExecutionRecordMessage replace = replaceRecords[1];
                Assert.That(heldCancelled, Is.Not.Null, "a-held was never cancelled by a-replace; " + client.Describe());
                Assert.That(heldCancelled.Status, Is.EqualTo(BridgeExecStatus.Cancelled), "a-held ended as something other than cancelled: " + Describe(heldCancelled));
                Assert.That(heldCancelled.ReleaseVerified, Is.True, "a-held was cancelled without a verified release: " + Describe(heldCancelled));
                Assert.That(replace, Is.Not.Null, "a-replace never reached a terminal state; " + client.Describe());
                Assert.That(replace.Status, Is.EqualTo(BridgeExecStatus.Completed), "a-replace did not complete: " + Describe(replace) + "; " + client.Describe());
                Assert.That(replace.ReleaseVerified, Is.True, "a-replace completed without a verified release: " + Describe(replace));
                Assert.That(replace.ValidatedAgainstSeq, Is.EqualTo(replaceObservation.Seq), "a-replace was not validated against a fresh observation: " + Describe(replace));

                // One frame of a queued release can still land after the terminal record.
                yield return WaitSeconds(ReleaseSettleSeconds);
                Vector3 replaceEnd = body.transform.position;
                Vector3 replaceDelta = replaceEnd - heldEnd;

                // "Moved the other way or stopped": whatever the game's forward direction is, the
                // body must not keep travelling along the superseded direction.
                float alongHeldM = Vector3.Dot(replaceDelta, heldDelta.normalized);
                Assert.That(
                    alongHeldM,
                    Is.LessThanOrEqualTo(MovementThresholdM),
                    "the superseded direction was not released: the body kept moving " +
                    alongHeldM.ToString("0.000", CultureInfo.InvariantCulture) + " m along it after a-replace; " +
                    Describe(heldCancelled) + "; " + Describe(replace) + "; " + client.Describe());

                yield return WaitSeconds(StabilitySeconds);
                float stabilityDeltaM = Vector3.Distance(replaceEnd, body.transform.position);
                Assert.That(
                    stabilityDeltaM,
                    Is.LessThan(MovementThresholdM),
                    $"the body did not stay still after a-replace completed; it moved {stabilityDeltaM.ToString("0.000", CultureInfo.InvariantCulture)} m in " +
                    $"{StabilitySeconds.ToString("0.0", CultureInfo.InvariantCulture)} s; " + Describe(replace) + "; " + client.Describe());

                string summary = string.Format(
                    CultureInfo.InvariantCulture,
                    "forward={0:0.000}m stale_delta={1:0.000}m held={2:0.000}m replace_along={3:0.000}m stable_delta={4:0.000}m forward_duration={5:0.000}s held_duration={6:0.000}s replace_duration={7:0.000}s",
                    forwardDistanceM,
                    staleDeltaM,
                    heldDelta.magnitude,
                    alongHeldM,
                    stabilityDeltaM,
                    forward.AppliedDurationS.Value,
                    heldCancelled.AppliedDurationS ?? -1.0,
                    replace.AppliedDurationS ?? -1.0);
                Debug.Log("[playtest-bridge] act: " + summary);

                List<BridgeErrorMessage> errors = client.ErrorsSnapshot();
                Assert.That(errors.Count, Is.EqualTo(0), "the bridge reported errors: " + client.Describe());
                Assert.That(
                    client.UnreadCount,
                    Is.EqualTo(0),
                    "the bridge sent lines that are not status, observation, execution or error: " + client.Describe());
            }
            finally
            {
                if (client != null)
                {
                    client.Dispose();
                }

                if (server != null)
                {
                    server.Dispose();
                }

                if (input != null)
                {
                    input.Dispose();
                }

                if (observationSource != null)
                {
                    observationSource.Dispose();
                }

                if (catalogPath != null)
                {
                    try
                    {
                        if (File.Exists(catalogPath))
                        {
                            File.Delete(catalogPath);
                        }
                    }
                    catch (IOException)
                    {
                    }
                    catch (UnauthorizedAccessException)
                    {
                    }
                }

                if (loadedScene.IsValid() && loadedScene.isLoaded)
                {
                    // Leave the runner's own scene active before removing the additive one.
                    for (int i = 0; i < SceneManager.sceneCount; i++)
                    {
                        Scene candidate = SceneManager.GetSceneAt(i);
                        if (candidate.IsValid() && candidate != loadedScene)
                        {
                            SceneManager.SetActiveScene(candidate);
                            break;
                        }
                    }

                    SceneManager.UnloadSceneAsync(loadedScene);
                }
            }
        }

        /// <summary>The Play mode test needs a scene; the first enabled Build Settings scene is the subject.</summary>
        private static string FirstEnabledScenePath()
        {
            string path = SceneUtility.GetScenePathByBuildIndex(0);
            Assert.That(
                string.IsNullOrEmpty(path),
                Is.False,
                "the project has no enabled scene in Build Settings; the Play mode measurement needs one");
            return path;
        }

        /// <summary>Finds the additively loaded scene by path, falling back to its name.</summary>
        private static Scene FindLoadedScene(string scenePath)
        {
            Scene scene = SceneManager.GetSceneByPath(scenePath);
            if (scene.IsValid() && scene.isLoaded)
            {
                return scene;
            }

            string sceneName = Path.GetFileNameWithoutExtension(scenePath);
            for (int i = 0; i < SceneManager.sceneCount; i++)
            {
                Scene candidate = SceneManager.GetSceneAt(i);
                if (candidate.IsValid() &&
                    candidate.isLoaded &&
                    string.Equals(candidate.path, scenePath, StringComparison.Ordinal))
                {
                    return candidate;
                }

                if (candidate.IsValid() &&
                    candidate.isLoaded &&
                    string.Equals(candidate.name, sceneName, StringComparison.Ordinal))
                {
                    return candidate;
                }
            }

            return default(Scene);
        }

        /// <summary>
        /// The first enabled CharacterController in the loaded scene. CharacterController is an
        /// engine component, so the test never names a game object, prefab or component.
        /// </summary>
        private static CharacterController FindFirstEnabledCharacterController(Scene scene)
        {
            GameObject[] roots = scene.GetRootGameObjects();
            for (int i = 0; i < roots.Length; i++)
            {
                GameObject root = roots[i];
                if (root == null)
                {
                    continue;
                }

                CharacterController[] controllers = root.GetComponentsInChildren<CharacterController>(true);
                for (int j = 0; j < controllers.Length; j++)
                {
                    CharacterController controller = controllers[j];
                    if (controller != null && controller.enabled && controller.gameObject.activeInHierarchy)
                    {
                        return controller;
                    }
                }
            }

            return null;
        }

        /// <summary><c>&lt;project&gt;/Temp/playtest-bridge-actions.json</c>, the path the Editor host also reads.</summary>
        private static string ActionCatalogPath()
        {
            string dataPath = Application.dataPath.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string projectRoot = Path.GetDirectoryName(dataPath);
            return Path.Combine(projectRoot, "Temp", "playtest-bridge-actions.json");
        }

        private static string ActionCatalogJson()
        {
            return "{\"actions\":{"
                + ActionEntry(ForwardAction, ForwardControl) + ","
                + ActionEntry(BackAction, BackControl)
                + "}}";
        }

        private static string ActionEntry(string name, string control)
        {
            return "\"" + name + "\":{\"backend\":\"input_system\",\"device\":\"Keyboard\",\"control\":\"" + control
                + "\",\"default_lease_s\":" + DefaultLeaseS.ToString("0.###", CultureInfo.InvariantCulture) + "}";
        }

        private static ActRequestMessage Act(
            ActClient client,
            string actionId,
            string choiceId,
            string action,
            long basedOnSeq,
            double maxAgeS,
            double? leaseS)
        {
            return new ActRequestMessage
            {
                Protocol = ProtocolVersion.Current,
                Session = SessionId,
                Seq = client.NextRequestSeq(),
                GameS = null,
                WallUtc = DateTime.UtcNow,
                ActionId = actionId,
                ChoiceId = choiceId,
                Action = action,
                Params = new Dictionary<string, object>(),
                BasedOnSeq = basedOnSeq,
                MaxAgeS = maxAgeS,
                DependsOn = new List<string>(),
                LeaseS = leaseS,
            };
        }

        private static IEnumerator ObserveOnce(ActClient client, Action<ObservationMessage> onFound)
        {
            int before = client.ObservationCount;
            client.Send(new ObserveRequestMessage
            {
                Protocol = ProtocolVersion.Current,
                Session = SessionId,
                Seq = client.NextRequestSeq(),
                GameS = null,
                WallUtc = DateTime.UtcNow,
                Mode = "once",
                WithFrame = false,
                RateHz = null,
            });

            ObservationMessage found = null;
            DateTime deadline = DateTime.UtcNow.AddSeconds(ReplyTimeoutSeconds);
            while (DateTime.UtcNow < deadline)
            {
                found = client.ObservationAt(before);
                if (found != null)
                {
                    break;
                }

                yield return null;
            }

            onFound(found);
        }

        private static void AssertPlaying(ObservationMessage observation, ActClient client, string what)
        {
            Assert.That(observation, Is.Not.Null, what + ": no observation arrived; " + client.Describe());
            Assert.That(
                observation.Playing,
                Is.True,
                what + ": the observation reports playing=false, so the act would be rejected as not_playing; " + client.Describe());
        }

        /// <summary>
        /// Yields frames until the body has stayed within <paramref name="toleranceM"/> of its
        /// anchor for <paramref name="quietSeconds"/>, or the timeout expires. Reports the largest
        /// sampled move so a failure shows what was observed.
        /// </summary>
        private static IEnumerator WaitForStill(
            CharacterController body,
            float toleranceM,
            double quietSeconds,
            double timeoutSeconds,
            Action<bool, float> onDone)
        {
            DateTime started = DateTime.UtcNow;
            DateTime deadline = started.AddSeconds(timeoutSeconds);
            Vector3 anchor = body.transform.position;
            DateTime anchorUtc = started;
            float largestStepM = 0f;
            while (DateTime.UtcNow < deadline)
            {
                Vector3 position = body.transform.position;
                float movedM = Vector3.Distance(anchor, position);
                if (movedM > largestStepM)
                {
                    largestStepM = movedM;
                }

                if (movedM > toleranceM)
                {
                    anchor = position;
                    anchorUtc = DateTime.UtcNow;
                }
                else if ((DateTime.UtcNow - anchorUtc).TotalSeconds >= quietSeconds)
                {
                    onDone(true, largestStepM);
                    yield break;
                }

                yield return null;
            }

            onDone(false, largestStepM);
        }

        private static IEnumerator WaitSeconds(double seconds)
        {
            DateTime deadline = DateTime.UtcNow.AddSeconds(seconds);
            while (DateTime.UtcNow < deadline)
            {
                yield return null;
            }
        }

        private static IEnumerator WaitForRecords(
            ActClient client,
            RecordExpectation[] expectations,
            double timeoutSeconds,
            Action<ExecutionRecordMessage[]> onDone)
        {
            var found = new ExecutionRecordMessage[expectations.Length];
            DateTime deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
            while (true)
            {
                bool complete = true;
                for (int i = 0; i < expectations.Length; i++)
                {
                    if (found[i] == null)
                    {
                        found[i] = client.FindRecord(expectations[i].ActionId, expectations[i].Statuses);
                        if (found[i] == null)
                        {
                            complete = false;
                        }
                    }
                }

                if (complete || DateTime.UtcNow >= deadline)
                {
                    onDone(found);
                    yield break;
                }

                yield return null;
            }
        }

        private static string Describe(ExecutionRecordMessage record)
        {
            if (record == null)
            {
                return "no record";
            }

            return string.Format(
                CultureInfo.InvariantCulture,
                "action_id={0} status={1} reject_reason={2} release_verified={3} validated_against_seq={4} applied_duration_s={5}",
                record.ActionId,
                record.Status,
                record.RejectReason ?? "none",
                record.ReleaseVerified.HasValue ? record.ReleaseVerified.Value.ToString() : "null",
                record.ValidatedAgainstSeq.HasValue
                    ? record.ValidatedAgainstSeq.Value.ToString(CultureInfo.InvariantCulture)
                    : "null",
                record.AppliedDurationS.HasValue
                    ? record.AppliedDurationS.Value.ToString("0.000", CultureInfo.InvariantCulture)
                    : "null");
        }

        private static int CurrentProcessId()
        {
            using (System.Diagnostics.Process process = System.Diagnostics.Process.GetCurrentProcess())
            {
                return process.Id;
            }
        }

        private sealed class RecordExpectation
        {
            public RecordExpectation(string actionId, IReadOnlyList<string> statuses)
            {
                ActionId = actionId;
                Statuses = statuses;
            }

            public string ActionId { get; }

            public IReadOnlyList<string> Statuses { get; }
        }

        /// <summary>
        /// Reads and parses the session on a background thread. The main thread only sends and
        /// polls, so it yields every frame while the lease and the Input System run.
        /// </summary>
        private sealed class ActClient : IDisposable
        {
            private readonly object _gate = new object();
            private readonly object _writeGate = new object();
            private readonly TcpClient _tcp;
            private readonly JsonLineConnection _connection;
            private readonly Thread _reader;
            private readonly List<ObservationMessage> _observations = new List<ObservationMessage>();
            private readonly List<ExecutionRecordMessage> _records = new List<ExecutionRecordMessage>();
            private readonly List<BridgeErrorMessage> _errors = new List<BridgeErrorMessage>();
            private readonly List<string> _unread = new List<string>();
            private StatusMessage _status;
            private long _requestSeq;
            private volatile bool _closed;

            public ActClient(int port)
            {
                _tcp = new TcpClient();
                _tcp.NoDelay = true;
                _tcp.Connect(IPAddress.Loopback, port);
                _connection = new JsonLineConnection(_tcp.GetStream(), 4 * 1024 * 1024);
                _reader = new Thread(ReadLoop)
                {
                    IsBackground = true,
                    Name = "playtest-act-reader",
                };
                _reader.Start();
            }

            public StatusMessage Status
            {
                get
                {
                    lock (_gate)
                    {
                        return _status;
                    }
                }
            }

            public int ObservationCount
            {
                get
                {
                    lock (_gate)
                    {
                        return _observations.Count;
                    }
                }
            }

            public int UnreadCount
            {
                get
                {
                    lock (_gate)
                    {
                        return _unread.Count;
                    }
                }
            }

            public long NextRequestSeq()
            {
                return Interlocked.Increment(ref _requestSeq);
            }

            public void Send(Envelope message)
            {
                string line = BridgeProtocol.Serialize(message);
                lock (_writeGate)
                {
                    _connection.WriteLine(line);
                }
            }

            /// <summary>The observation that arrived at <paramref name="index"/>, or null if none yet.</summary>
            public ObservationMessage ObservationAt(int index)
            {
                lock (_gate)
                {
                    return index >= 0 && index < _observations.Count ? _observations[index] : null;
                }
            }

            /// <summary>
            /// The first received record for <paramref name="actionId"/> whose status is in
            /// <paramref name="statuses"/>; a null list matches any status.
            /// </summary>
            public ExecutionRecordMessage FindRecord(string actionId, IReadOnlyList<string> statuses)
            {
                lock (_gate)
                {
                    for (int i = 0; i < _records.Count; i++)
                    {
                        ExecutionRecordMessage record = _records[i];
                        if (!string.Equals(record.ActionId, actionId, StringComparison.Ordinal))
                        {
                            continue;
                        }

                        if (statuses == null)
                        {
                            return record;
                        }

                        for (int j = 0; j < statuses.Count; j++)
                        {
                            if (string.Equals(record.Status, statuses[j], StringComparison.Ordinal))
                            {
                                return record;
                            }
                        }
                    }
                }

                return null;
            }

            public List<BridgeErrorMessage> ErrorsSnapshot()
            {
                lock (_gate)
                {
                    return new List<BridgeErrorMessage>(_errors);
                }
            }

            public string Describe()
            {
                lock (_gate)
                {
                    var builder = new StringBuilder();
                    builder.Append("status=").Append(_status == null ? "none" : "received");
                    builder.Append(" observations=").Append(_observations.Count);
                    if (_observations.Count > 0)
                    {
                        ObservationMessage last = _observations[_observations.Count - 1];
                        builder.Append(" last_observation=seq ").Append(last.Seq.ToString(CultureInfo.InvariantCulture));
                        builder.Append(" frame ").Append(last.Frame.ToString(CultureInfo.InvariantCulture));
                        builder.Append(" scene '").Append(last.Scene).Append("'");
                        builder.Append(" playing ").Append(last.Playing ? "true" : "false");
                    }

                    builder.Append(" records=");
                    if (_records.Count == 0)
                    {
                        builder.Append("none");
                    }
                    else
                    {
                        for (int i = 0; i < _records.Count; i++)
                        {
                            if (i > 0)
                            {
                                builder.Append("; ");
                            }

                            ExecutionRecordMessage record = _records[i];
                            builder.Append(record.ActionId).Append(":").Append(record.Status);
                            if (!string.IsNullOrEmpty(record.RejectReason))
                            {
                                builder.Append("(").Append(record.RejectReason).Append(")");
                            }
                        }
                    }

                    builder.Append(" errors=");
                    if (_errors.Count == 0)
                    {
                        builder.Append("none");
                    }
                    else
                    {
                        for (int i = 0; i < _errors.Count; i++)
                        {
                            if (i > 0)
                            {
                                builder.Append("; ");
                            }

                            builder.Append(_errors[i].Code).Append(": ").Append(_errors[i].Message);
                        }
                    }

                    builder.Append(" unread=").Append(_unread.Count == 0 ? "none" : _unread[0]);
                    return builder.ToString();
                }
            }

            public void Dispose()
            {
                _closed = true;
                try
                {
                    _tcp.Close();
                }
                catch (Exception)
                {
                }

                if (_reader != null && _reader.IsAlive && _reader != Thread.CurrentThread)
                {
                    _reader.Join(2000);
                }

                try
                {
                    _connection.Stream.Dispose();
                }
                catch (Exception)
                {
                }
            }

            private void ReadLoop()
            {
                try
                {
                    while (!_closed)
                    {
                        string line = _connection.ReadLine(0);
                        if (line == null)
                        {
                            return;
                        }

                        if (line.Length == 0)
                        {
                            continue;
                        }

                        if (BridgeProtocol.TryReadObservation(line, out ObservationMessage observation, out ProtocolError error))
                        {
                            lock (_gate)
                            {
                                _observations.Add(observation);
                            }

                            continue;
                        }

                        if (BridgeProtocol.TryReadExecutionRecord(line, out ExecutionRecordMessage record, out error))
                        {
                            lock (_gate)
                            {
                                _records.Add(record);
                            }

                            continue;
                        }

                        if (BridgeProtocol.TryReadStatus(line, out StatusMessage status, out error))
                        {
                            lock (_gate)
                            {
                                _status = status;
                            }

                            continue;
                        }

                        if (BridgeProtocol.TryReadBridgeError(line, out BridgeErrorMessage bridgeError, out error))
                        {
                            lock (_gate)
                            {
                                _errors.Add(bridgeError);
                            }

                            continue;
                        }

                        lock (_gate)
                        {
                            _unread.Add(line);
                        }
                    }
                }
                catch (IOException)
                {
                }
                catch (SocketException)
                {
                }
                catch (ObjectDisposedException)
                {
                }
                catch (InvalidDataException)
                {
                }
                catch (Exception ex)
                {
                    lock (_gate)
                    {
                        _unread.Add("reader failed: " + ex.GetType().Name + ": " + ex.Message);
                    }
                }
            }
        }
    }
}
