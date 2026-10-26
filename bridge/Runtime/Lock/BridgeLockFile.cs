using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Playtest.Bridge.Lock
{
    /// <summary>
    /// One-Editor-per-project lock file. The file holds the owner's pid, port,
    /// token and start time as bridge-local JSON. Creation is atomic under a
    /// race (temp file plus a no-overwrite move); a second caller whose owner
    /// is alive gets <see cref="BridgeLockBusyException"/>. No Unity API.
    /// </summary>
    public sealed class BridgeLockFile : IDisposable
    {
        private static readonly Encoding Utf8NoBom = new UTF8Encoding(false);

        private bool _released;

        private BridgeLockFile(string path, BridgeLockInfo info)
        {
            Path = path;
            Info = info;
        }

        public string Path { get; }

        public BridgeLockInfo Info { get; }

        /// <summary>
        /// Acquires the lock at <paramref name="path"/>. Throws
        /// <see cref="BridgeLockBusyException"/> when a live process owns it.
        /// A same-pid caller replaces the lock (a previous holder lost its
        /// socket); a dead, uninspectable or corrupt owner is treated as stale
        /// and taken over.
        /// </summary>
        public static BridgeLockFile Acquire(string path, int pid, int port, string token)
        {
            if (string.IsNullOrEmpty(path))
            {
                throw new ArgumentException("Lock path is required.", nameof(path));
            }

            if (pid <= 0)
            {
                throw new ArgumentOutOfRangeException(nameof(pid), "Pid must be positive.");
            }

            if (port < 0 || port > 65535)
            {
                throw new ArgumentOutOfRangeException(nameof(port), "Port must be 0-65535.");
            }

            if (string.IsNullOrEmpty(token))
            {
                throw new ArgumentException("Token is required.", nameof(token));
            }

            string fullPath = System.IO.Path.GetFullPath(path);
            EnsureParentDirectory(fullPath);

            var info = new BridgeLockInfo
            {
                Protocol = "upa/0",
                Pid = pid,
                Port = port,
                Token = token,
                StartedUtc = DateTime.UtcNow,
            };

            if (TryClaim(fullPath, info))
            {
                return new BridgeLockFile(fullPath, info);
            }

            BridgeLockInfo existing = Read(fullPath);
            if (existing == null)
            {
                TakeOverStale(fullPath, info);
                return new BridgeLockFile(fullPath, info);
            }

            if (existing.Pid == pid)
            {
                Replace(fullPath, info);
                return new BridgeLockFile(fullPath, info);
            }

            if (IsAlive(existing))
            {
                throw new BridgeLockBusyException(existing, fullPath);
            }

            TakeOverStale(fullPath, info);
            return new BridgeLockFile(fullPath, info);
        }

        /// <summary>Reads the lock file. Returns null when missing or corrupt.</summary>
        public static BridgeLockInfo Read(string path)
        {
            if (string.IsNullOrEmpty(path))
            {
                return null;
            }

            string text;
            try
            {
                if (!File.Exists(path))
                {
                    return null;
                }

                text = File.ReadAllText(path, Encoding.UTF8);
            }
            catch (IOException)
            {
                return null;
            }
            catch (UnauthorizedAccessException)
            {
                return null;
            }

            if (string.IsNullOrWhiteSpace(text))
            {
                return null;
            }

            try
            {
                return Parse(text);
            }
            catch (JsonException)
            {
                return null;
            }
            catch (FormatException)
            {
                return null;
            }
            catch (InvalidCastException)
            {
                return null;
            }
            catch (OverflowException)
            {
                return null;
            }
            catch (ArgumentException)
            {
                return null;
            }
        }

        /// <summary>Atomically rewrites the file with a new port. Keeps pid, token and start time.</summary>
        public void UpdatePort(int port)
        {
            if (port < 0 || port > 65535)
            {
                throw new ArgumentOutOfRangeException(nameof(port), "Port must be 0-65535.");
            }

            if (_released)
            {
                throw new InvalidOperationException("This lock instance has been released.");
            }

            Info.Port = port;
            Replace(Path, Info);
        }

        /// <summary>
        /// Deletes the file only when this instance still owns it (pid and
        /// token match). Idempotent; swallows file-system errors.
        /// </summary>
        public void Release()
        {
            if (_released)
            {
                return;
            }

            _released = true;

            try
            {
                BridgeLockInfo current = Read(Path);
                if (current == null)
                {
                    return;
                }

                if (current.Pid != Info.Pid)
                {
                    return;
                }

                if (!string.Equals(current.Token, Info.Token, StringComparison.Ordinal))
                {
                    return;
                }

                File.Delete(Path);
            }
            catch (FileNotFoundException)
            {
            }
            catch (DirectoryNotFoundException)
            {
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
        }

        public void Dispose()
        {
            Release();
            GC.SuppressFinalize(this);
        }

        private static BridgeLockInfo Parse(string text)
        {
            var obj = JObject.Parse(text);

            JToken pidToken = obj["pid"];
            JToken portToken = obj["port"];
            JToken tokenToken = obj["token"];
            JToken startedToken = obj["started_utc"];

            if (pidToken == null || portToken == null || tokenToken == null || startedToken == null)
            {
                return null;
            }

            if (pidToken.Type != JTokenType.Integer || portToken.Type != JTokenType.Integer)
            {
                return null;
            }

            if (tokenToken.Type != JTokenType.String)
            {
                return null;
            }

            string token = (string)tokenToken;
            if (string.IsNullOrEmpty(token))
            {
                return null;
            }

            DateTime startedUtc;
            if (startedToken.Type == JTokenType.Date)
            {
                startedUtc = ((DateTime)startedToken).ToUniversalTime();
            }
            else if (startedToken.Type == JTokenType.String)
            {
                if (!DateTime.TryParse(
                    (string)startedToken,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal,
                    out DateTime parsed))
                {
                    return null;
                }

                startedUtc = parsed.Kind == DateTimeKind.Utc ? parsed : parsed.ToUniversalTime();
            }
            else
            {
                return null;
            }

            JToken protocolToken = obj["protocol"];
            string protocol = protocolToken != null && protocolToken.Type == JTokenType.String
                ? (string)protocolToken
                : "upa/0";

            return new BridgeLockInfo
            {
                Protocol = protocol,
                Pid = (int)pidToken,
                Port = (int)portToken,
                Token = token,
                StartedUtc = startedUtc,
            };
        }

        private static string Serialize(BridgeLockInfo info)
        {
            DateTime started = info.StartedUtc.Kind == DateTimeKind.Utc
                ? info.StartedUtc
                : info.StartedUtc.ToUniversalTime();

            var obj = new JObject
            {
                ["protocol"] = info.Protocol ?? "upa/0",
                ["pid"] = info.Pid,
                ["port"] = info.Port,
                ["token"] = info.Token ?? string.Empty,
                ["started_utc"] = started,
            };
            return obj.ToString(Formatting.None);
        }

        private static void TakeOverStale(string fullPath, BridgeLockInfo info)
        {
            TryDelete(fullPath);
            if (TryClaim(fullPath, info))
            {
                return;
            }

            BridgeLockInfo winner = Read(fullPath);
            if (winner != null && winner.Pid != info.Pid && IsAlive(winner))
            {
                throw new BridgeLockBusyException(winner, fullPath);
            }

            throw new IOException("Could not acquire the project lock at '" + fullPath + "' (contention).");
        }

        /// <summary>Writes a temp file in the same directory, then moves it into place without overwriting.</summary>
        private static bool TryClaim(string fullPath, BridgeLockInfo info)
        {
            string temp = TempPathFor(fullPath, info.Pid);
            try
            {
                File.WriteAllText(temp, Serialize(info), Utf8NoBom);
            }
            catch (IOException)
            {
                return false;
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }

            try
            {
                File.Move(temp, fullPath);
                return true;
            }
            catch (IOException)
            {
                TryDelete(temp);
                return false;
            }
            catch (UnauthorizedAccessException)
            {
                TryDelete(temp);
                return false;
            }
        }

        /// <summary>Atomic rewrite of an existing file: temp file plus an atomic replace, never a missing window.</summary>
        private static void Replace(string fullPath, BridgeLockInfo info)
        {
            string temp = TempPathFor(fullPath, info.Pid);
            File.WriteAllText(temp, Serialize(info), Utf8NoBom);
            try
            {
                try
                {
                    File.Replace(temp, fullPath, null);
                }
                catch (PlatformNotSupportedException)
                {
                    File.Copy(temp, fullPath, true);
                }
                catch (IOException)
                {
                    // Some file systems cannot replace in place. The copy is still bounded to this
                    // writer; readers only ever see the old or new body on the supported platforms.
                    File.Copy(temp, fullPath, true);
                }
            }
            finally
            {
                TryDelete(temp);
            }
        }

        private static bool IsAlive(BridgeLockInfo info)
        {
            Process process;
            try
            {
                process = Process.GetProcessById(info.Pid);
            }
            catch (ArgumentException)
            {
                return false;
            }
            catch (InvalidOperationException)
            {
                return false;
            }
            catch (NotSupportedException)
            {
                return false;
            }

            using (process)
            {
                try
                {
                    if (process.HasExited)
                    {
                        return false;
                    }

                    DateTime processStartUtc = process.StartTime.ToUniversalTime();
                    if (processStartUtc > info.StartedUtc)
                    {
                        return false;
                    }

                    return true;
                }
                catch (InvalidOperationException)
                {
                    return false;
                }
                catch (NotSupportedException)
                {
                    return false;
                }
                catch (System.ComponentModel.Win32Exception)
                {
                    return false;
                }
            }
        }

        private static void EnsureParentDirectory(string fullPath)
        {
            string directory = System.IO.Path.GetDirectoryName(fullPath);
            if (!string.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }
        }

        private static string TempPathFor(string fullPath, int pid)
        {
            string directory = System.IO.Path.GetDirectoryName(fullPath);
            if (string.IsNullOrEmpty(directory))
            {
                directory = System.IO.Path.GetTempPath();
            }

            return System.IO.Path.Combine(
                directory,
                System.IO.Path.GetFileName(fullPath) + "." + pid + "." + Guid.NewGuid().ToString("N") + ".tmp");
        }

        private static void TryDelete(string path)
        {
            try
            {
                File.Delete(path);
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
        }
    }
}
