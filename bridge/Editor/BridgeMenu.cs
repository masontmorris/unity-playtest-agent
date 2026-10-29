// SPDX-License-Identifier: Apache-2.0
using System.Text;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Unity;
using UnityEditor;
using UnityEngine;

namespace Playtest.Bridge.Editor
{
    /// <summary>
    /// <c>Tools/Playtest Bridge</c> menu. It exposes the bridge lifecycle and facts; it never
    /// decides when a core should connect and never prints the token.
    /// </summary>
    internal static class BridgeMenu
    {
        [MenuItem("Tools/Playtest Bridge/Start", false, 100)]
        private static void StartBridge()
        {
            BridgeEditorHost.Start();
        }

        [MenuItem("Tools/Playtest Bridge/Start", true)]
        private static bool CanStartBridge()
        {
            return !BridgeEditorHost.IsRunning;
        }

        [MenuItem("Tools/Playtest Bridge/Stop", false, 101)]
        private static void StopBridge()
        {
            BridgeEditorHost.Stop();
        }

        [MenuItem("Tools/Playtest Bridge/Stop", true)]
        private static bool CanStopBridge()
        {
            return BridgeEditorHost.IsRunning;
        }

        [MenuItem("Tools/Playtest Bridge/Status", false, 102)]
        private static void LogStatus()
        {
            int port = BridgeEditorHost.Port;
            string lastError = BridgeEditorHost.LastError ?? "none";

            var report = new StringBuilder();
            report.AppendLine(BridgeEditorHost.LogPrefix + " status");
            report.AppendLine("  running: " + (BridgeEditorHost.IsRunning ? "yes" : "no"));
            report.AppendLine("  protocol: " + ProtocolVersion.Current);
            report.AppendLine("  bridge version: " + UnityStatusSource.PackageVersion);
            report.AppendLine("  port: " + (port > 0 ? port.ToString() : "none"));
            report.AppendLine("  pid: " + CurrentProcessId());
            report.AppendLine("  lock: " + BridgeEditorHost.LockPath);
            report.AppendLine("  last error: " + lastError);

            Debug.Log(report.ToString());
        }

        private static int CurrentProcessId()
        {
            using (System.Diagnostics.Process process = System.Diagnostics.Process.GetCurrentProcess())
            {
                return process.Id;
            }
        }
    }
}
