using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;
using NUnit.Framework;
using Playtest.Bridge.Editor;
using Playtest.Bridge.Lock;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;
using Playtest.Bridge.Unity;
using UnityEngine;

namespace Playtest.Bridge.Tests
{
    [TestFixture]
    public sealed class UnityStatusSourceTests
    {
        [Test]
        public void GetStatus_ReportsEditorFacts()
        {
            var source = new UnityStatusSource(CurrentProcessId());
            source.Port = 43210;

            BridgeStatusSnapshot snapshot = source.GetStatus();

            Assert.That(snapshot.BridgeVersion, Is.EqualTo(UnityStatusSource.PackageVersion));
            Assert.That(snapshot.UnityVersion, Is.EqualTo(Application.unityVersion));
            Assert.That(snapshot.Project, Is.Not.Null.And.Not.Empty);
            if (!string.IsNullOrEmpty(Application.productName))
            {
                Assert.That(snapshot.Project, Is.EqualTo(Application.productName));
            }

            Assert.That(snapshot.OwnerPid, Is.EqualTo(CurrentProcessId()));
            Assert.That(snapshot.ClockControl, Is.True);
            Assert.That(snapshot.Capture, Is.False);
            Assert.That(snapshot.CaptureSelfTest, Is.EqualTo(UnityStatusSource.Unknown));
            Assert.That(source.Port, Is.EqualTo(43210));
            Assert.That(source.OwnerPid, Is.EqualTo(CurrentProcessId()));
        }

        [Test]
        public void GetStatus_ReportsTheBackendsOfTheActiveCompileSymbols()
        {
            var expected = new List<string>();
#if ENABLE_INPUT_SYSTEM
            expected.Add(UnityStatusSource.InputBackendInputSystem);
#endif
#if ENABLE_LEGACY_INPUT_MANAGER
            expected.Add(UnityStatusSource.InputBackendLegacy);
#endif

            BridgeStatusSnapshot snapshot = new UnityStatusSource(CurrentProcessId()).GetStatus();

            Assert.That(snapshot.InputBackends, Is.EquivalentTo(expected));
        }

        [Test]
        public void GetStatus_ReturnsAnIndependentSnapshotEachCall()
        {
            var source = new UnityStatusSource(CurrentProcessId());
            BridgeStatusSnapshot first = source.GetStatus();
            first.Project = "changed-by-test";

            BridgeStatusSnapshot second = source.GetStatus();

            Assert.That(second, Is.Not.SameAs(first));
            Assert.That(second.Project, Is.Not.EqualTo("changed-by-test"));
        }

        [Test]
        public void GetStatus_IsSafeOffTheMainThread()
        {
            var source = new UnityStatusSource(CurrentProcessId());
            source.Port = 43211;

            BridgeStatusSnapshot snapshot = Task.Run(() => source.GetStatus()).GetAwaiter().GetResult();

            Assert.That(snapshot.OwnerPid, Is.EqualTo(CurrentProcessId()));
            Assert.That(snapshot.Project, Is.Not.Null.And.Not.Empty);
            Assert.That(source.Port, Is.EqualTo(43211));
        }

        private static int CurrentProcessId()
        {
            using (Process process = Process.GetCurrentProcess())
            {
                return process.Id;
            }
        }
    }

    [TestFixture]
    public sealed class UnityBridgeEditorHostTests
    {
        [SetUp]
        public void SetUp()
        {
            RemoveLeftovers();
        }

        [TearDown]
        public void TearDown()
        {
            RemoveLeftovers();
        }

        [Test]
        public void Start_StartsTheServerAndWritesTheProjectLock()
        {
            BridgeEditorHost.Start();

            Assert.That(BridgeEditorHost.IsRunning, Is.True, BridgeEditorHost.LastError);
            Assert.That(BridgeEditorHost.LastError, Is.Null);
            Assert.That(BridgeEditorHost.Port, Is.GreaterThan(0));
            Assert.That(BridgeEditorHost.LockOwnerPid, Is.EqualTo(CurrentProcessId()));
            Assert.That(File.Exists(BridgeEditorHost.LockPath), Is.True, "the lock must exist while the bridge runs");

            string dataPath = Application.dataPath.TrimEnd(
                Path.DirectorySeparatorChar,
                Path.AltDirectorySeparatorChar);
            string projectRoot = Path.GetDirectoryName(dataPath);
            string expected = Path.GetFullPath(
                Path.Combine(projectRoot, "Temp", "playtest-bridge.lock"));
            Assert.That(BridgeEditorHost.LockPath, Is.EqualTo(expected));

            BridgeLockInfo info = BridgeLockFile.Read(BridgeEditorHost.LockPath);
            Assert.That(info, Is.Not.Null, "the lock file must be readable while the bridge runs");
            Assert.That(info.Pid, Is.EqualTo(CurrentProcessId()));
            Assert.That(info.Port, Is.EqualTo(BridgeEditorHost.Port));
        }

        [Test]
        public void Host_AnswersHelloWithTheEditorStatusAndLockToken()
        {
            BridgeEditorHost.Start();
            Assert.That(BridgeEditorHost.IsRunning, Is.True, BridgeEditorHost.LastError);

            BridgeLockInfo lockInfo = BridgeLockFile.Read(BridgeEditorHost.LockPath);
            Assert.That(lockInfo, Is.Not.Null, "the running host must have written the lock");
            Assert.That(lockInfo.Token, Is.Not.Null.And.Not.Empty);

            using (var client = new TestClient(BridgeEditorHost.Port))
            {
                client.Send(new HelloMessage
                {
                    Protocol = ProtocolVersion.Current,
                    Session = "s-host",
                    Seq = 1,
                    GameS = null,
                    WallUtc = DateTime.UtcNow,
                    Token = lockInfo.Token,
                    CoreVersion = "0.0.1",
                });

                StatusMessage status = client.ReadStatus();

                Assert.That(status.Session, Is.EqualTo("s-host"));
                Assert.That(status.Type, Is.EqualTo("status"));
                Assert.That(status.Project, Is.Not.Null.And.Not.Empty);
                Assert.That(status.UnityVersion, Is.EqualTo(Application.unityVersion));
                Assert.That(status.Lock.Held, Is.True);
                Assert.That(status.Lock.OwnerPid, Is.EqualTo(CurrentProcessId()));
                Assert.That(status.Lock.Port, Is.EqualTo(BridgeEditorHost.Port));
                Assert.That(status.Capture, Is.False);
                Assert.That(status.CaptureSelfTest, Is.EqualTo(UnityCapture.Failed));
            }
        }

        [Test]
        public void Start_IsIdempotent()
        {
            BridgeEditorHost.Start();
            int port = BridgeEditorHost.Port;

            BridgeEditorHost.Start();

            Assert.That(BridgeEditorHost.IsRunning, Is.True);
            Assert.That(BridgeEditorHost.Port, Is.EqualTo(port));
            Assert.That(File.Exists(BridgeEditorHost.LockPath), Is.True);
        }

        [Test]
        public void Stop_StopsTheServerAndRemovesTheLock()
        {
            BridgeEditorHost.Start();
            Assert.That(BridgeEditorHost.IsRunning, Is.True, BridgeEditorHost.LastError);

            BridgeEditorHost.Stop();

            Assert.That(BridgeEditorHost.IsRunning, Is.False);
            Assert.That(BridgeEditorHost.Port, Is.Zero);
            Assert.That(BridgeEditorHost.LockOwnerPid, Is.Zero);
            Assert.That(File.Exists(BridgeEditorHost.LockPath), Is.False, "the lock must be removed when stopped");
        }

        [Test]
        public void Stop_IsIdempotent()
        {
            BridgeEditorHost.Stop();
            BridgeEditorHost.Stop();

            Assert.That(BridgeEditorHost.IsRunning, Is.False);
            Assert.That(BridgeEditorHost.Port, Is.Zero);
        }

        private static void RemoveLeftovers()
        {
            BridgeEditorHost.Stop();
            if (File.Exists(BridgeEditorHost.LockPath))
            {
                // Only one Editor may own a project, so a file seen here is from a prior run.
                File.Delete(BridgeEditorHost.LockPath);
            }
        }

        private static int CurrentProcessId()
        {
            using (Process process = Process.GetCurrentProcess())
            {
                return process.Id;
            }
        }
    }
}
