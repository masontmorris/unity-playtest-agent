// SPDX-License-Identifier: Apache-2.0
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using NUnit.Framework;
using Playtest.Bridge.Lock;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Tests
{
    [TestFixture]
    public sealed class BridgeLockFileTests
    {
        private string _dir;
        private int _selfPid;
        private Process _sleeper;

        private string LockPath
        {
            get { return Path.Combine(_dir, "bridge.lock"); }
        }

        [SetUp]
        public void SetUp()
        {
            _dir = Path.Combine(Path.GetTempPath(), "upa-lock-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(_dir);
            using (Process current = Process.GetCurrentProcess())
            {
                _selfPid = current.Id;
            }
        }

        [TearDown]
        public void TearDown()
        {
            StopSleeper();
            try
            {
                if (Directory.Exists(_dir))
                {
                    Directory.Delete(_dir, true);
                }
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
        }

        [Test]
        public void Acquire_CreatesFile_ReadReturnsSameValues()
        {
            using (BridgeLockFile held = BridgeLockFile.Acquire(LockPath, _selfPid, 18080, "token-1"))
            {
                Assert.That(File.Exists(LockPath), Is.True);

                BridgeLockInfo read = BridgeLockFile.Read(LockPath);
                Assert.That(read, Is.Not.Null);
                Assert.That(read.Protocol, Is.EqualTo(ProtocolVersion.Current));
                Assert.That(read.Pid, Is.EqualTo(_selfPid));
                Assert.That(read.Port, Is.EqualTo(18080));
                Assert.That(read.Token, Is.EqualTo("token-1"));
                Assert.That(read.StartedUtc.Kind, Is.EqualTo(DateTimeKind.Utc));
                Assert.That(Math.Abs((DateTime.UtcNow - read.StartedUtc).TotalMinutes), Is.LessThan(5));

                Assert.That(held.Info.Pid, Is.EqualTo(_selfPid));
                Assert.That(held.Info.Port, Is.EqualTo(18080));
                Assert.That(held.Info.Token, Is.EqualTo("token-1"));

                byte[] bytes = File.ReadAllBytes(LockPath);
                Assert.That(HasUtf8Bom(bytes), Is.False, "lock file must be UTF-8 without BOM");
                Assert.That(Encoding.UTF8.GetString(bytes), Does.Contain("started_utc"));
            }
        }

        [Test]
        public void SamePid_Reacquire_ReplacesExistingLock()
        {
            using (BridgeLockFile first = BridgeLockFile.Acquire(LockPath, _selfPid, 18081, "token-a"))
            using (BridgeLockFile second = BridgeLockFile.Acquire(LockPath, _selfPid, 18082, "token-b"))
            {
                BridgeLockInfo read = BridgeLockFile.Read(LockPath);
                Assert.That(read, Is.Not.Null);
                Assert.That(read.Pid, Is.EqualTo(_selfPid));
                Assert.That(read.Port, Is.EqualTo(18082));
                Assert.That(read.Token, Is.EqualTo("token-b"));

                first.Release();
                Assert.That(File.Exists(LockPath), Is.True, "stale instance must not delete the replaced lock");
            }

            Assert.That(File.Exists(LockPath), Is.False);
        }

        [Test]
        public void LiveForeignPid_YieldsBusyWithOwner()
        {
            Process sleeper = StartSleeper();
            using (BridgeLockFile owner = BridgeLockFile.Acquire(LockPath, sleeper.Id, 18083, "owner-token"))
            {
                BridgeLockBusyException thrown = Assert.Throws<BridgeLockBusyException>(
                    () => BridgeLockFile.Acquire(LockPath, _selfPid, 18084, "second-token"));
                Assert.That(thrown.Owner, Is.Not.Null);
                Assert.That(thrown.Owner.Pid, Is.EqualTo(sleeper.Id));
                Assert.That(thrown.Owner.Port, Is.EqualTo(18083));
                Assert.That(thrown.Owner.Token, Is.EqualTo("owner-token"));

                BridgeLockInfo read = BridgeLockFile.Read(LockPath);
                Assert.That(read.Token, Is.EqualTo("owner-token"));
                Assert.That(read.Pid, Is.EqualTo(sleeper.Id));
            }
        }

        [Test]
        public void DeadPid_IsTakenOver()
        {
            File.WriteAllText(
                LockPath,
                "{\"protocol\":\"" + ProtocolVersion.Current + "\",\"pid\":2147483647,\"port\":18085,\"token\":\"ghost\",\"started_utc\":\"2020-01-01T00:00:00Z\"}",
                new UTF8Encoding(false));

            using (BridgeLockFile held = BridgeLockFile.Acquire(LockPath, _selfPid, 18086, "takeover"))
            {
                BridgeLockInfo read = BridgeLockFile.Read(LockPath);
                Assert.That(read.Pid, Is.EqualTo(_selfPid));
                Assert.That(read.Port, Is.EqualTo(18086));
                Assert.That(read.Token, Is.EqualTo("takeover"));
            }
        }

        [Test]
        public void ExitedPid_IsTakenOver()
        {
            Process sleeper = StartSleeper();
            int deadPid = sleeper.Id;
            StopSleeper();
            Assert.That(deadPid, Is.Not.EqualTo(_selfPid));

            using (BridgeLockFile held = BridgeLockFile.Acquire(LockPath, deadPid, 18087, "exited"))
            {
                Assert.That(File.Exists(LockPath), Is.True);
            }

            using (BridgeLockFile held = BridgeLockFile.Acquire(LockPath, _selfPid, 18088, "takeover"))
            {
                BridgeLockInfo read = BridgeLockFile.Read(LockPath);
                Assert.That(read.Pid, Is.EqualTo(_selfPid));
                Assert.That(read.Token, Is.EqualTo("takeover"));
            }
        }

        [Test]
        public void CorruptFile_IsStaleForAcquireAndNullForRead([Values("", "not json{", "{\"pid\":1}")] string body)
        {
            File.WriteAllText(LockPath, body, new UTF8Encoding(false));
            Assert.That(BridgeLockFile.Read(LockPath), Is.Null);

            using (BridgeLockFile held = BridgeLockFile.Acquire(LockPath, _selfPid, 18089, "after-corrupt"))
            {
                BridgeLockInfo read = BridgeLockFile.Read(LockPath);
                Assert.That(read, Is.Not.Null);
                Assert.That(read.Pid, Is.EqualTo(_selfPid));
                Assert.That(read.Token, Is.EqualTo("after-corrupt"));
            }
        }

        [Test]
        public void MissingFile_ReadReturnsNull()
        {
            Assert.That(BridgeLockFile.Read(LockPath), Is.Null);
        }

        [Test]
        public void Release_DeletesFile_AndSecondReleaseIsNoOp()
        {
            BridgeLockFile held = BridgeLockFile.Acquire(LockPath, _selfPid, 18090, "release-me");
            Assert.That(File.Exists(LockPath), Is.True);

            held.Release();
            Assert.That(File.Exists(LockPath), Is.False);

            Assert.DoesNotThrow(() => held.Release());
            held.Dispose();
        }

        [Test]
        public void Release_AfterAnotherOwnerReplaced_DoesNotDelete()
        {
            using (BridgeLockFile first = BridgeLockFile.Acquire(LockPath, _selfPid, 18091, "token-a"))
            using (BridgeLockFile second = BridgeLockFile.Acquire(LockPath, _selfPid, 18092, "token-b"))
            {
                first.Release();
                Assert.That(File.Exists(LockPath), Is.True);
                Assert.That(BridgeLockFile.Read(LockPath).Token, Is.EqualTo("token-b"));
            }
        }

        [Test]
        public void UpdatePort_ChangesOnlyPort()
        {
            using (BridgeLockFile held = BridgeLockFile.Acquire(LockPath, _selfPid, 18093, "port-token"))
            {
                BridgeLockInfo before = BridgeLockFile.Read(LockPath);
                held.UpdatePort(18094);

                Assert.That(held.Info.Port, Is.EqualTo(18094));

                BridgeLockInfo after = BridgeLockFile.Read(LockPath);
                Assert.That(after, Is.Not.Null);
                Assert.That(after.Port, Is.EqualTo(18094));
                Assert.That(after.Pid, Is.EqualTo(before.Pid));
                Assert.That(after.Token, Is.EqualTo(before.Token));
                Assert.That(after.Protocol, Is.EqualTo(before.Protocol));
                Assert.That(after.StartedUtc, Is.EqualTo(before.StartedUtc));
            }
        }

        private Process StartSleeper()
        {
            if (_sleeper != null && !_sleeper.HasExited)
            {
                return _sleeper;
            }

            var start = new ProcessStartInfo();
            if (Environment.OSVersion.Platform == PlatformID.Win32NT)
            {
                start.FileName = "powershell";
                start.Arguments = "-NoProfile -NonInteractive -Command \"Start-Sleep -Seconds 120\"";
            }
            else
            {
                start.FileName = "/bin/sh";
                start.Arguments = "-c \"sleep 120\"";
            }

            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;

            _sleeper = Process.Start(start);
            return _sleeper;
        }

        private void StopSleeper()
        {
            Process sleeper = _sleeper;
            _sleeper = null;
            if (sleeper == null)
            {
                return;
            }

            try
            {
                if (!sleeper.HasExited)
                {
                    sleeper.Kill();
                    sleeper.WaitForExit(5000);
                }
            }
            catch (InvalidOperationException)
            {
            }
            catch (System.ComponentModel.Win32Exception)
            {
            }
            finally
            {
                sleeper.Dispose();
            }
        }

        private static bool HasUtf8Bom(byte[] bytes)
        {
            return bytes.Length >= 3 && bytes[0] == 0xEF && bytes[1] == 0xBB && bytes[2] == 0xBF;
        }
    }
}
