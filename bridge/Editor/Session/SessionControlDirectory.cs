// SPDX-License-Identifier: Apache-2.0
using System;
using System.IO;
using System.Text;

namespace Playtest.Bridge.Editor.Session
{
    /// <summary>
    /// The per-run control directory is outside the game project, holds only host control
    /// records (never gameplay input, a token or a key), and every publication is a same-directory
    /// temp file followed by an atomic replace, so a reader never sees a partial record.
    /// </summary>
    public sealed class SessionControlDirectory
    {
        /// <summary>Environment variable naming the directory; set on both the Unity and Python processes.</summary>
        public const string VariableName = "UPA_SESSION_CONTROL_DIRECTORY";

        /// <summary>Out-of-band supervisor cleanup marker. Not an S1 choice and not a mailbox record.</summary>
        public const string ShutdownFileName = "shutdown";

        private static readonly Encoding Utf8NoBom = new UTF8Encoding(false);

        private SessionControlDirectory(string root)
        {
            Root = root;
        }

        public string Root { get; }

        public static SessionControlDirectory Open(string root)
        {
            if (string.IsNullOrWhiteSpace(root))
            {
                throw new ArgumentException("A control directory is required.", nameof(root));
            }

            string full = Path.GetFullPath(root);
            if (!Directory.Exists(full))
            {
                throw new DirectoryNotFoundException("the session control directory does not exist: " + full);
            }

            return new SessionControlDirectory(full);
        }

        public static SessionControlDirectory FromEnvironment()
        {
            string root = Environment.GetEnvironmentVariable(VariableName);
            if (string.IsNullOrWhiteSpace(root))
            {
                throw new InvalidOperationException(VariableName + " is not set");
            }

            return Open(root);
        }

        public string ConfigPath
        {
            get { return PathFor(SessionConfig.FileName); }
        }

        public string HostStatePath
        {
            get { return PathFor(HostStateRecord.FileName); }
        }

        public string RequestPath
        {
            get { return PathFor(RestartRequestRecord.FileName); }
        }

        public string ResultPath
        {
            get { return PathFor(RestartResultRecord.FileName); }
        }

        public string CancelPath
        {
            get { return PathFor(RestartCancelRecord.FileName); }
        }

        public string ShutdownPath
        {
            get { return PathFor(ShutdownFileName); }
        }

        public string PathFor(string fileName)
        {
            return Path.Combine(Root, fileName);
        }

        public bool ShutdownRequested
        {
            get { return File.Exists(ShutdownPath); }
        }

        public string ReadConfigText()
        {
            return File.ReadAllText(ConfigPath, Encoding.UTF8);
        }

        /// <summary>Reads a control file; a missing or transiently unreadable file is "not there yet".</summary>
        public bool TryReadText(string path, out string text)
        {
            text = null;
            try
            {
                if (!File.Exists(path))
                {
                    return false;
                }

                text = File.ReadAllText(path, Encoding.UTF8);
                return !string.IsNullOrWhiteSpace(text);
            }
            catch (IOException)
            {
                return false;
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }
        }

        public void WriteHostState(HostStateRecord state)
        {
            Write(HostStatePath, state.ToJson());
        }

        public void WriteResult(RestartResultRecord result)
        {
            Write(ResultPath, result.ToJson());
        }

        public void Write(string path, string text)
        {
            WriteAtomic(path, text);
        }

        /// <summary>Same-directory temp file, then an atomic replace: readers see the old or new body.</summary>
        public static void WriteAtomic(string path, string text)
        {
            string full = Path.GetFullPath(path);
            string directory = Path.GetDirectoryName(full);
            if (!string.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }

            string temporary = full + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                File.WriteAllText(temporary, text, Utf8NoBom);
                if (File.Exists(full))
                {
                    try
                    {
                        File.Replace(temporary, full, null);
                    }
                    catch (PlatformNotSupportedException)
                    {
                        File.Copy(temporary, full, true);
                    }
                    catch (IOException)
                    {
                        // Some file systems cannot replace in place; the copy is bounded to this writer.
                        File.Copy(temporary, full, true);
                    }
                }
                else
                {
                    File.Move(temporary, full);
                }
            }
            finally
            {
                TryDelete(temporary);
            }
        }

        private static void TryDelete(string path)
        {
            try
            {
                if (File.Exists(path))
                {
                    File.Delete(path);
                }
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
