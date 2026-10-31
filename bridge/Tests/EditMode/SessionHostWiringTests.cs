// SPDX-License-Identifier: Apache-2.0
using System;
using System.IO;
using NUnit.Framework;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The seam between the host, the supervisor and the two launchers is written in C# and PowerShell,
    /// so the strings and the order of their steps are checked in source. Behaviour is measured by the
    /// other session tests; this keeps the wiring from drifting silently.
    /// </summary>
    [TestFixture]
    public sealed class SessionHostWiringTests
    {
        private const string HostRelativePath = "bridge/Editor/PlayModeSessionHost.cs";
        private const string SupervisorRelativePath = "scripts/unity/play-session-supervisor.ps1";
        private const string SessionScriptRelativePath = "scripts/unity/run-session.ps1";
        private const string RestartScriptRelativePath = "scripts/unity/test-session-restart.ps1";

        private static string RepoRoot
        {
            get { return Path.GetFullPath(Path.Combine(TestPaths.Contracts, "..", "..")); }
        }

        [Test]
        public void TheHostStartsPlayModeAndReachesReadyOnlyFromMeasuredState()
        {
            string host = Read(HostRelativePath);
            string initial = Section(host, "private static void StepInitialStartup(DateTime now)",
                "private static void StepRestart(DateTime now)");
            string restart = Section(host, "private static void StepRestartPlayInstance",
                "private static bool EnsureFreshSceneLoaded");

            Assert.That(host, Does.Contain("\"Playtest.Bridge.Editor.PlayModeSessionHost.Start\""));
            Assert.That(host, Does.Contain("EditorApplication.EnterPlaymode()"));
            Assert.That(host, Does.Contain("EditorApplication.ExitPlaymode()"));
            Assert.That(host, Does.Contain("BridgeSessionCatalog.LoadExported(_config.CatalogPath, _config.CatalogSha256)"),
                "the pinned manifest catalog is verified, with no fallback");

            // Initial startup: the catalog and scene are prepared, the bridge starts, then the host
            // publishes readiness. Readiness is never the reason the bridge was started.
            Assert.That(initial.IndexOf("PrepareEditMode();", StringComparison.Ordinal),
                Is.LessThan(initial.IndexOf("EditorApplication.EnterPlaymode();", StringComparison.Ordinal)));
            Assert.That(initial.IndexOf("BridgeEditorHost.StartForPreflight(catalog, generation);", StringComparison.Ordinal),
                Is.LessThan(initial.IndexOf("_machine.MarkInitialReady(DateTime.UtcNow)", StringComparison.Ordinal)));

            // Restart: a new bridge with its own generation, then the transition records readiness.
            Assert.That(restart.IndexOf("BridgeEditorHost.StartForPreflight(catalog, generation);", StringComparison.Ordinal),
                Is.LessThan(restart.IndexOf("_machine.MarkBridgeStarted(generation, DateTime.UtcNow)", StringComparison.Ordinal)));
            Assert.That(restart.IndexOf("_machine.MintGenerationForPlayInstance()", StringComparison.Ordinal),
                Is.LessThan(restart.IndexOf("BridgeEditorHost.StartForPreflight(catalog, generation);", StringComparison.Ordinal)),
                "a generation is on record before the bridge that serves it starts");
        }

        [Test]
        public void ThePlayModeCallbackRestoresTheSavedSessionBeforeItHandlesTheEvent()
        {
            string host = Read(HostRelativePath);
            string callback = Section(host, "private static void OnPlayModeStateChanged",
                "private static void BeginShutdown");

            int restore = callback.IndexOf("Resume();", StringComparison.Ordinal);
            Assert.That(restore, Is.GreaterThanOrEqualTo(0),
                "the callback resumes the persisted session itself, before the first Editor update");
            Assert.That(
                restore,
                Is.LessThan(callback.IndexOf("if (_machine == null || _exitRequested)", StringComparison.Ordinal)),
                "a transition raised before the first update is not dropped");
            Assert.That(callback, Does.Contain("SessionState.GetBool(BegunKey, false)"),
                "only a session this process already began is resumed");
            Assert.That(callback, Does.Contain("PlayModeSessionEvents.Apply(_machine, observed, now)"),
                "the event rules are shared with the pure session tests");
        }

        [Test]
        public void TheHostNeverInventsARequestOrPublishesOnEveryPoll()
        {
            string host = Read(HostRelativePath);

            Assert.That(host, Does.Not.Contain("new RestartRequestRecord"),
                "a request comes only from the mailbox");
            Assert.That(host, Does.Not.Contain("Write(_control.RequestPath"),
                "the host writes no request of its own");
            Assert.That(host, Does.Contain("if (_machine.Revision == _publishedRevision)"),
                "one publication per transition, not one per poll");
            Assert.That(host, Does.Contain("MailboxPollSeconds = 0.25"));

            string lower = host.ToLowerInvariant();
            foreach (string word in new[] { "stagnat", "threshold", "score", "heuristic" })
            {
                Assert.That(lower, Does.Not.Contain(word),
                    "no code path may turn a measurement into a restart or a help request");
            }
        }

        [Test]
        public void TheSupervisorLaunchesTheHostAndWaitsForMeasuredReadiness()
        {
            string supervisor = Read(SupervisorRelativePath);

            Assert.That(supervisor, Does.Contain("$EntryPoint = 'Playtest.Bridge.Editor.PlayModeSessionHost.Start'"));
            Assert.That(supervisor, Does.Contain("'-executeMethod', $EntryPoint"));
            Assert.That(supervisor, Does.Contain("$ControlVariable = 'UPA_SESSION_CONTROL_DIRECTORY'"));
            Assert.That(supervisor, Does.Contain("$HostStateName = 'host-state.json'"));
            Assert.That(supervisor, Does.Contain("$ShutdownName = 'shutdown'"));
            Assert.That(supervisor, Does.Contain("Start-Process -FilePath $unityFull"));

            Assert.That(supervisor, Does.Not.Contain("'-runTests'"));
            Assert.That(supervisor, Does.Not.Contain("'-quit'"));
            Assert.That(supervisor, Does.Not.Contain("'-testPlatform'"));

            int ready = supervisor.IndexOf("Wait-InitialReady -Path $hostStatePath", StringComparison.Ordinal);
            int client = supervisor.IndexOf("Start-OwnedClient -FilePath $PythonPath", StringComparison.Ordinal);
            int shutdown = supervisor.IndexOf("$shutdownPath = Join-Path $control $ShutdownName",
                StringComparison.Ordinal);
            Assert.That(ready, Is.GreaterThanOrEqualTo(0));
            Assert.That(client, Is.GreaterThanOrEqualTo(0));
            Assert.That(shutdown, Is.GreaterThanOrEqualTo(0));
            Assert.That(ready, Is.LessThan(client),
                "the client starts only after the host's own readiness record");
            Assert.That(client, Is.LessThan(shutdown),
                "the shutdown marker is published only after the client ends");
            Assert.That(supervisor, Does.Contain("'failed' {"), "a failed host ends the run, it is not retried");
        }

        [Test]
        public void TheSupervisorOwnsBothProcessesAndTheAbsoluteDeadline()
        {
            string supervisor = Read(SupervisorRelativePath);

            // A synchronous '& $PythonPath ...' would block readiness, host-exit and deadline checks.
            Assert.That(supervisor, Does.Not.Contain("& $PythonPath"));
            Assert.That(supervisor, Does.Contain("$deadlineUtc = ConvertTo-UtcInstant $config.deadline_utc"),
                "the deadline is validated as an absolute instant, never defaulted");
            Assert.That(supervisor, Does.Contain("while (-not $clientProcess.HasExited)"));
            Assert.That(supervisor, Does.Contain("if ($unityProcess.HasExited)"),
                "the host is watched while the client runs");
            Assert.That(supervisor, Does.Contain("$now -ge $deadlineUtc"),
                "the client's lifetime is bounded by the absolute deadline");
            Assert.That(supervisor, Does.Contain("Stop-Process -Id $Process.Id"),
                "cleanup stops only the process object this run started");
            Assert.That(supervisor, Does.Contain("$ClientStdoutName = 'client.stdout.log'"));
            Assert.That(supervisor, Does.Contain("$ClientStderrName = 'client.stderr.log'"));
        }

        [Test]
        public void TheSessionScriptWritesTheConfigAndDelegatesSupervision()
        {
            string script = Read(SessionScriptRelativePath);

            Assert.That(script, Does.Contain("'play-session-supervisor.ps1'"));
            Assert.That(script, Does.Contain("$controlDirectory = Join-Path $output 'control'"));
            Assert.That(script, Does.Contain("(Join-Path $controlDirectory 'config.json')"));
            Assert.That(script, Does.Contain("$runId = [guid]::NewGuid().ToString('D')"));
            foreach (string key in new[] { "run_id", "project_path", "catalog_path", "catalog_sha256", "deadline_utc" })
            {
                Assert.That(script, Does.Contain(key));
            }

            Assert.That(script, Does.Not.Contain("'-runTests'"));
            Assert.That(script, Does.Not.Contain("'-testPlatform'"));
            Assert.That(script, Does.Not.Contain("'-TestFilter'"));
            Assert.That(script, Does.Contain("& powershell.exe @runnerArgs"),
                "the launcher still runs its supervisor as a child PowerShell process");
        }

        [Test]
        public void TheRestartSmokeMakesNoPaidCallAndUsesTheSameHost()
        {
            string script = Read(RestartScriptRelativePath);

            Assert.That(script, Does.Contain("'playtest.runner.restart_smoke'"));
            Assert.That(script, Does.Contain("'play-session-supervisor.ps1'"));
            Assert.That(script, Does.Contain("(Join-Path $controlDirectory 'config.json')"));
            Assert.That(script, Does.Contain("'restart.json'"));

            foreach (string forbidden in new[] { "API_KEY", "ApprovalPath", "MaxCalls", "UPA_RUN_APPROVAL" })
            {
                Assert.That(script, Does.Not.Contain(forbidden),
                    "the free restart smoke has no credentials and no paid call to cap");
            }
        }

        private static string Read(string relativePath)
        {
            string path = Path.Combine(RepoRoot, relativePath.Replace('/', Path.DirectorySeparatorChar));
            Assert.That(File.Exists(path), Is.True, "missing source file: " + path);
            return File.ReadAllText(path);
        }

        private static string Section(string text, string start, string end)
        {
            int from = text.IndexOf(start, StringComparison.Ordinal);
            Assert.That(from, Is.GreaterThanOrEqualTo(0), "missing section start: " + start);
            int to = end == null ? text.Length : text.IndexOf(end, from, StringComparison.Ordinal);
            Assert.That(to, Is.GreaterThan(from), "missing section end: " + end);
            return text.Substring(from, to - from);
        }
    }
}
