using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using Playtest.Bridge.Transport;
using UnityEngine;

namespace Playtest.Bridge.Unity
{
    /// <summary>
    /// Editor-side status for the <c>status</c> reply. The Unity facts are read once, on the main
    /// thread, when the source is constructed. <see cref="GetStatus"/> only copies those cached
    /// values, so the transport thread may call it at any time.
    /// Missing facts stay <see cref="Unknown"/>; nothing here is inferred as zero or success.
    /// </summary>
    public sealed class UnityStatusSource : IBridgeStatusSource
    {
        /// <summary>Package version reported as <c>bridge_version</c>; matches package.json.</summary>
        public const string PackageVersion = "0.1.0";

        public const string InputBackendInputSystem = "input_system";

        public const string InputBackendLegacy = "legacy";

        /// <summary>Reported when the Editor does not expose a fact.</summary>
        public const string Unknown = "unknown";

        private readonly int _ownerPid;
        private readonly string _unityVersion;
        private readonly string _project;
        private readonly string[] _inputBackends;
        private int _port;

        public UnityStatusSource(int ownerPid)
        {
            _ownerPid = ownerPid;
            _unityVersion = ReadUnityVersion();
            _project = ReadProject();
            _inputBackends = ReadInputBackends();
        }

        /// <summary>The bridge has a clock controller; the host may override this.</summary>
        public bool ClockControl { get; set; } = true;

        /// <summary>Capture arrives in #18; it stays false until then.</summary>
        public bool Capture { get; set; }

        public string CaptureSelfTest { get; set; } = Unknown;

        /// <summary>Loopback port the server bound. Written on the main thread after start.</summary>
        public int Port
        {
            get { return Volatile.Read(ref _port); }
            set { Volatile.Write(ref _port, value); }
        }

        public int OwnerPid
        {
            get { return _ownerPid; }
        }

        public BridgeStatusSnapshot GetStatus()
        {
            return new BridgeStatusSnapshot
            {
                BridgeVersion = PackageVersion,
                UnityVersion = _unityVersion,
                Project = _project,
                InputBackends = Array.AsReadOnly((string[])_inputBackends.Clone()),
                ClockControl = this.ClockControl,
                Capture = this.Capture,
                CaptureSelfTest = this.CaptureSelfTest,
                OwnerPid = _ownerPid,
            };
        }

        private static string ReadUnityVersion()
        {
            try
            {
                string version = Application.unityVersion;
                return string.IsNullOrEmpty(version) ? Unknown : version;
            }
            catch (Exception)
            {
                return Unknown;
            }
        }

        private static string ReadProject()
        {
            string product = null;
            try
            {
                product = Application.productName;
            }
            catch (Exception)
            {
            }

            if (!string.IsNullOrWhiteSpace(product))
            {
                return product;
            }

            try
            {
                string dataPath = Application.dataPath;
                if (!string.IsNullOrEmpty(dataPath))
                {
                    dataPath = dataPath.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                    DirectoryInfo parent = Directory.GetParent(dataPath);
                    if (parent != null && !string.IsNullOrWhiteSpace(parent.Name))
                    {
                        return parent.Name;
                    }
                }
            }
            catch (Exception)
            {
            }

            return Unknown;
        }

        private static string[] ReadInputBackends()
        {
            var backends = new List<string>(2);
#if ENABLE_INPUT_SYSTEM
            backends.Add(InputBackendInputSystem);
#endif
#if ENABLE_LEGACY_INPUT_MANAGER
            backends.Add(InputBackendLegacy);
#endif
            return backends.ToArray();
        }
    }
}
