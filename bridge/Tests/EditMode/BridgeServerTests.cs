// SPDX-License-Identifier: Apache-2.0
using System;
using System.IO;
using System.Net.Sockets;
using NUnit.Framework;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;

namespace Playtest.Bridge.Tests
{
    internal sealed class TestClient : IDisposable
    {
        public TestClient(BridgeServer server, int timeoutMs = 3000)
            : this(server.Port, timeoutMs)
        {
        }

        public TestClient(int port, int timeoutMs = 3000)
        {
            Tcp = new TcpClient();
            Tcp.Connect("127.0.0.1", port);
            Tcp.ReceiveTimeout = timeoutMs;
            Connection = new JsonLineConnection(Tcp.GetStream(), 4 * 1024 * 1024);
            Connection.Stream.ReadTimeout = timeoutMs;
        }

        public TcpClient Tcp { get; }

        public JsonLineConnection Connection { get; }

        public void Send(Envelope message)
        {
            Connection.WriteLine(BridgeProtocol.Serialize(message));
        }

        public void SendRaw(string line)
        {
            Connection.WriteLine(line);
        }

        public string ReadLine(int timeoutMs = 3000)
        {
            string line = Connection.ReadLine(timeoutMs);
            Assert.That(line, Is.Not.Null, "expected a reply before the timeout");
            return line;
        }

        public StatusMessage ReadStatus()
        {
            Assert.That(
                BridgeProtocol.TryReadStatus(ReadLine(), out StatusMessage status, out ProtocolError error),
                Is.True,
                error?.Message);
            return status;
        }

        public BridgeErrorMessage ReadError()
        {
            Assert.That(
                BridgeProtocol.TryReadBridgeError(ReadLine(), out BridgeErrorMessage error, out ProtocolError parseError),
                Is.True,
                parseError?.Message);
            return error;
        }

        public bool WaitForClose(int timeoutMs = 1000)
        {
            try
            {
                return Connection.ReadLine(timeoutMs) == null;
            }
            catch (IOException)
            {
                return true;
            }
        }

        public void Dispose()
        {
            Tcp.Close();
        }
    }

    internal static class BridgeTestSupport
    {
        public const string Token = "test-token-0123456789abcdef";

        public static HelloMessage Hello(
            string token = Token,
            string protocol = ProtocolVersion.Current,
            string session = "s-test",
            long seq = 1)
        {
            return new HelloMessage
            {
                Protocol = protocol,
                Session = session,
                Seq = seq,
                GameS = null,
                WallUtc = DateTime.UtcNow,
                Token = token,
                CoreVersion = "0.0.1",
            };
        }
    }

    internal sealed class FakeStatusSource : IBridgeStatusSource
    {
        public BridgeStatusSnapshot Status { get; set; } = new BridgeStatusSnapshot
        {
            BridgeVersion = "0.1.0",
            UnityVersion = "6000.5.4f1",
            Project = "Fake",
            InputBackends = new[] { "input_system" },
            ClockControl = false,
            Capture = false,
            CaptureSelfTest = "unknown",
            OwnerPid = 4242,
        };

        public BridgeStatusSnapshot GetStatus()
        {
            return Status;
        }
    }

    [TestFixture]
    public sealed class BridgeServerTests
    {
        private BridgeServer _server;
        private FakeStatusSource _status;

        [SetUp]
        public void SetUp()
        {
            _status = new FakeStatusSource();
            _server = new BridgeServer(
                new BridgeServerOptions
                {
                    Port = 0,
                    HandshakeTimeoutMs = 3000,
                    ReadTimeoutMs = 0,
                },
                _status,
                BridgeTestSupport.Token);
            _server.Start();
        }

        [TearDown]
        public void TearDown()
        {
            _server?.Dispose();
        }

        [Test]
        public void ValidHello_ReturnsStatusWithLockAndPort()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello(session: "s-1", seq: 7));

                StatusMessage status = client.ReadStatus();

                Assert.That(status.Session, Is.EqualTo("s-1"));
                Assert.That(status.Seq, Is.EqualTo(0));
                Assert.That(status.Protocol, Is.EqualTo(ProtocolVersion.Current));
                Assert.That(status.Type, Is.EqualTo("status"));
                Assert.That(status.BridgeVersion, Is.EqualTo("0.1.0"));
                Assert.That(status.UnityVersion, Is.EqualTo("6000.5.4f1"));
                Assert.That(status.Project, Is.EqualTo("Fake"));
                Assert.That(status.InputBackends, Is.EquivalentTo(new[] { "input_system" }));
                Assert.That(status.Capture, Is.False);
                Assert.That(status.CaptureSelfTest, Is.EqualTo("unknown"));
                Assert.That(status.Lock.Held, Is.True);
                Assert.That(status.Lock.OwnerPid, Is.EqualTo(4242));
                Assert.That(status.Lock.Port, Is.EqualTo(_server.Port));
                Assert.That(_server.Port, Is.GreaterThan(0));
            }
        }

        [Test]
        public void WrongToken_ReturnsAuthAndCloses()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello(token: "not-the-token"));

                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Auth));
                Assert.That(error.Session, Is.EqualTo("s-test"));
                Assert.That(error.InReplyTo, Is.EqualTo(1));
                Assert.That(client.WaitForClose(), Is.True, "server should close after an auth failure");
            }
        }

        [Test]
        public void UnsupportedProtocol_ReturnsProtocol()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello(protocol: "upa/9"));

                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Protocol));
                Assert.That(error.Message, Does.Contain("upa/9"));
                Assert.That(client.WaitForClose(), Is.True);
            }
        }

        [Test]
        public void MalformedJson_ReturnsInvalid()
        {
            using (var client = new TestClient(_server))
            {
                client.SendRaw("{not json");

                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
                Assert.That(error.Session, Is.EqualTo("unknown"));
                Assert.That(client.WaitForClose(), Is.True);
            }
        }

        [Test]
        public void UnknownField_ReturnsInvalid()
        {
            using (var client = new TestClient(_server))
            {
                string hello = BridgeProtocol.Serialize(BridgeTestSupport.Hello());
                hello = hello.Substring(0, hello.Length - 1) + ",\"surprise\":true}";
                client.SendRaw(hello);

                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
                Assert.That(error.Message, Does.Contain("surprise").IgnoreCase);
            }
        }

        [Test]
        public void SecondAuthenticatedCaller_GetsBusyImmediately()
        {
            using (var first = new TestClient(_server))
            {
                first.Send(BridgeTestSupport.Hello(session: "s-first"));
                first.ReadStatus();

                using (var second = new TestClient(_server))
                {
                    DateTime started = DateTime.UtcNow;
                    second.Send(BridgeTestSupport.Hello(session: "s-second"));
                    BridgeErrorMessage error = second.ReadError();
                    TimeSpan elapsed = DateTime.UtcNow - started;

                    Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Busy));
                    Assert.That(error.Session, Is.EqualTo("s-second"));
                    Assert.That(elapsed, Is.LessThan(TimeSpan.FromSeconds(2)), "busy must not wait for the first session");

                    // The first session survives the rejected second caller.
                    first.SendRaw("{\"type\":\"wat\"}");
                    Assert.That(first.ReadError().Code, Is.EqualTo(BridgeErrorCode.Invalid));
                }
            }
        }

        [Test]
        public void SecondCallerWithWrongToken_GetsAuthNotBusy()
        {
            using (var first = new TestClient(_server))
            {
                first.Send(BridgeTestSupport.Hello(session: "s-first"));
                first.ReadStatus();

                using (var second = new TestClient(_server))
                {
                    second.Send(BridgeTestSupport.Hello(token: "wrong", session: "s-second"));
                    Assert.That(second.ReadError().Code, Is.EqualTo(BridgeErrorCode.Auth));
                }
            }
        }

        [Test]
        public void Disconnect_ReleasesSessionForNextCaller()
        {
            using (var first = new TestClient(_server))
            {
                first.Send(BridgeTestSupport.Hello(session: "s-first"));
                first.ReadStatus();
            }

            using (var next = new TestClient(_server))
            {
                next.Send(BridgeTestSupport.Hello(session: "s-next"));
                Assert.That(next.ReadStatus().Session, Is.EqualTo("s-next"));
            }
        }

        [Test]
        public void HelloAfterHandshake_ReturnsInvalid()
        {
            using (var client = new TestClient(_server))
            {
                client.Send(BridgeTestSupport.Hello(session: "s-1"));
                client.ReadStatus();

                client.Send(BridgeTestSupport.Hello(session: "s-1", seq: 2));
                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
                Assert.That(error.InReplyTo, Is.EqualTo(2));
            }
        }
    }
}
