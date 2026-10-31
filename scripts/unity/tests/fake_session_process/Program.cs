// SPDX-License-Identifier: Apache-2.0
// A real process, used only by scripts/unity/test-play-session-supervisor.ps1.
//
// The supervisor passes the same arguments it would pass to Unity.exe or to the Python client.
// This program decides which role it is in from those arguments, reads its instructions from
// UPA_FAKE_* environment variables, and records its own pid in the control directory so the test
// can check which processes the supervisor did and did not stop. It is never used by a real run.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading;

namespace FakeSessionProcess
{
    internal static class Program
    {
        private const string ControlVariable = "UPA_SESSION_CONTROL_DIRECTORY";

        private static int Main(string[] args)
        {
            try
            {
                if (args.Contains("-executeMethod", StringComparer.Ordinal))
                {
                    return RunUnity(args);
                }

                return RunClient(args);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("fake-session-process: " + ex);
                return 90;
            }
        }

        private static string ControlDirectory()
        {
            string value = Environment.GetEnvironmentVariable(ControlVariable);
            if (string.IsNullOrWhiteSpace(value))
            {
                throw new InvalidOperationException(ControlVariable + " is not set");
            }

            return value;
        }

        private static string Mode(string name, string fallback)
        {
            string value = Environment.GetEnvironmentVariable(name);
            return string.IsNullOrWhiteSpace(value) ? fallback : value.Trim();
        }

        private static double Seconds(string name, double fallback)
        {
            string value = Environment.GetEnvironmentVariable(name);
            double parsed;
            return double.TryParse(value, System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out parsed) && parsed >= 0
                ? parsed
                : fallback;
        }

        private static int RunUnity(string[] args)
        {
            string control = ControlDirectory();
            string mode = Mode("UPA_FAKE_UNITY_MODE", "hold");
            File.WriteAllText(Path.Combine(control, "fake-unity.pid"), Environment.ProcessId.ToString());
            WriteUnityLog(args);

            if (mode == "exit-before-ready")
            {
                Console.Error.WriteLine("fake unity: exiting before readiness");
                return 1;
            }

            string runId = ReadRunId(Path.Combine(control, "config.json"));
            if (mode == "failed-state")
            {
                WriteHostState(control, runId, "failed", null, null, null, "fake startup failure");
                Console.Error.WriteLine("fake unity: failed startup");
                return 1;
            }

            string generation = Guid.NewGuid().ToString("D");
            WriteHostState(control, runId, "ready", generation, "Assets/Scenes/Fake.unity", 1, null);
            Console.Out.WriteLine("fake unity: ready for run " + runId);
            Console.Out.Flush();

            if (mode == "exit-after-ready")
            {
                Sleep(Seconds("UPA_FAKE_UNITY_EXIT_SECONDS", 0));
                WriteHostState(control, runId, "stopped", generation, "Assets/Scenes/Fake.unity", 1, null);
                return 0;
            }

            if (mode == "ignore-shutdown")
            {
                Sleep(600);
                return 0;
            }

            string shutdown = Path.Combine(control, "shutdown");
            var clock = Stopwatch.StartNew();
            while (clock.Elapsed.TotalSeconds < 600)
            {
                if (File.Exists(shutdown))
                {
                    Sleep(Seconds("UPA_FAKE_UNITY_SHUTDOWN_DELAY_SECONDS", 0));
                    WriteHostState(control, runId, "stopped", generation, "Assets/Scenes/Fake.unity", 1, null);
                    Console.Out.WriteLine("fake unity: shutdown observed; exiting cleanly");
                    Console.Out.Flush();
                    return 0;
                }

                Sleep(0.05);
            }

            return 1;
        }

        private static int RunClient(string[] args)
        {
            string control = ControlDirectory();
            string mode = Mode("UPA_FAKE_CLIENT_MODE", "exit0");
            File.WriteAllText(Path.Combine(control, "fake-client.pid"), Environment.ProcessId.ToString());
            File.WriteAllLines(Path.Combine(control, "fake-client.args"), args, new UTF8Encoding(false));

            Console.Out.WriteLine("fake client: mode " + mode);
            Console.Error.WriteLine("fake client stderr: mode " + mode);
            Console.Out.Flush();
            Console.Error.Flush();

            switch (mode)
            {
                case "exit1":
                    return 1;
                case "hang":
                    Sleep(Seconds("UPA_FAKE_CLIENT_HANG_SECONDS", 600));
                    return 0;
                case "hang-then-exit0":
                    Sleep(Seconds("UPA_FAKE_CLIENT_HANG_SECONDS", 30));
                    return 0;
                default:
                    return 0;
            }
        }

        private static void WriteUnityLog(string[] args)
        {
            int index = Array.IndexOf(args, "-logFile");
            if (index < 0 || index + 1 >= args.Length)
            {
                return;
            }

            try
            {
                File.AppendAllText(args[index + 1], "fake unity: launched" + Environment.NewLine);
            }
            catch (IOException)
            {
            }
        }

        private static string ReadRunId(string configPath)
        {
            using (JsonDocument document = JsonDocument.Parse(File.ReadAllText(configPath)))
            {
                JsonElement value;
                if (!document.RootElement.TryGetProperty("run_id", out value)
                    || value.ValueKind != JsonValueKind.String)
                {
                    throw new InvalidOperationException("config.json has no run_id");
                }

                return value.GetString();
            }
        }

        private static void WriteHostState(
            string control, string runId, string phase, string generation, string scenePath, int? handle,
            string error)
        {
            var state = new Dictionary<string, object>
            {
                ["version"] = 1,
                ["run_id"] = runId,
                ["phase"] = phase,
                ["generation"] = generation,
                ["scene_path"] = scenePath,
                ["scene_handle"] = handle,
                ["ready_utc"] = phase == "ready" ? DateTime.UtcNow.ToString("o") : null,
                ["error"] = error,
                ["capabilities"] = new[] { "restart_play_mode" },
            };

            string path = Path.Combine(control, "host-state.json");
            string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            File.WriteAllText(temporary, JsonSerializer.Serialize(state), new UTF8Encoding(false));
            File.Move(temporary, path, true);
        }

        private static void Sleep(double seconds)
        {
            if (seconds <= 0)
            {
                return;
            }

            Thread.Sleep(TimeSpan.FromSeconds(seconds));
        }
    }
}
