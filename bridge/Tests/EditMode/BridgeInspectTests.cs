using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using NUnit.Framework;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;

namespace Playtest.Bridge.Tests
{
    internal sealed class FakeInspectionSource : IBridgeInspectionSource
    {
        public Func<string, BridgeInspectionSnapshot> InspectImpl { get; set; }

        public List<string> Stages { get; } = new List<string>();

        public BridgeInspectionSnapshot Inspect(string stage)
        {
            Stages.Add(stage);
            return InspectImpl != null ? InspectImpl(stage) : Snapshot(stage);
        }

        public static BridgeInspectionSnapshot Snapshot(string stage)
        {
            return new BridgeInspectionSnapshot
            {
                Data = new Dictionary<string, object>
                {
                    ["stage"] = stage,
                    ["count"] = 2,
                },
                Caps = new Dictionary<string, InspectionCap>
                {
                    ["records"] = new InspectionCap
                    {
                        Limit = 100,
                        Observed = 2,
                        Returned = 2,
                        Truncated = false,
                    },
                },
                Errors = new List<string>(),
            };
        }
    }

    [TestFixture]
    public sealed class BridgeInspectTests
    {
        private FakeStatusSource _status;
        private FakeInspectionSource _source;
        private BridgeServer _server;

        [SetUp]
        public void SetUp()
        {
            _status = new FakeStatusSource();
            _source = new FakeInspectionSource();
            _server = StartServer(_source);
        }

        [TearDown]
        public void TearDown()
        {
            _server?.Dispose();
        }

        [Test]
        public void Inspect_ReturnsResultBoundToRequestSequence()
        {
            using (TestClient client = Connect(_server))
            {
                client.Send(Inspect("inventory", seq: 2));

                InspectionResultMessage result = ReadInspection(client);

                Assert.That(result.Type, Is.EqualTo("inspection"));
                Assert.That(result.Session, Is.EqualTo("s-inspect"));
                Assert.That(result.Stage, Is.EqualTo("inventory"));
                Assert.That(result.InReplyTo, Is.EqualTo(2));
                Assert.That(result.GameS, Is.Null, "Editor inspection has no game clock");
                Assert.That(_source.Stages, Is.EqualTo(new[] { "inventory" }));
                Assert.That(result.Data.ContainsKey("count"), Is.True);
                Assert.That(JToken.FromObject(result.Data["count"]).Value<int>(), Is.EqualTo(2));
                Assert.That(result.Caps.ContainsKey("records"), Is.True);
                Assert.That(result.Caps["records"].Limit, Is.EqualTo(100));
                Assert.That(result.Caps["records"].Observed, Is.EqualTo(2));
                Assert.That(result.Caps["records"].Returned, Is.EqualTo(2));
                Assert.That(result.Caps["records"].Truncated, Is.False);
                Assert.That(result.Errors, Is.Empty);
            }
        }

        [Test]
        public void Inspect_DiscoveryStageIsPassedThrough()
        {
            using (TestClient client = Connect(_server))
            {
                client.Send(Inspect("discovery", seq: 3));

                InspectionResultMessage result = ReadInspection(client);

                Assert.That(result.Stage, Is.EqualTo("discovery"));
                Assert.That(result.InReplyTo, Is.EqualTo(3));
                Assert.That(_source.Stages, Is.EqualTo(new[] { "discovery" }));
            }
        }

        [Test]
        public void Inspect_FieldsStageIsPassedThrough()
        {
            using (TestClient client = Connect(_server))
            {
                client.Send(Inspect("fields", seq: 4));

                InspectionResultMessage result = ReadInspection(client);

                Assert.That(result.Stage, Is.EqualTo("fields"));
                Assert.That(result.InReplyTo, Is.EqualTo(4));
                Assert.That(_source.Stages, Is.EqualTo(new[] { "fields" }));
            }
        }

        [Test]
        public void Inspect_WithoutSource_ReturnsInternal()
        {
            _server.Dispose();
            _server = StartServer(null);

            using (TestClient client = Connect(_server))
            {
                client.Send(Inspect("inventory", seq: 2));

                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Internal));
                Assert.That(error.InReplyTo, Is.EqualTo(2));
                Assert.That(error.Message, Does.Contain("inspection source"));
            }
        }

        [Test]
        public void Inspect_WithInvalidStage_ReturnsInvalid()
        {
            using (TestClient client = Connect(_server))
            {
                string validStage = BridgeProtocol.InspectionStages[0];
                string json = BridgeProtocol.Serialize(Inspect(validStage, seq: 2));
                json = json.Replace("\"stage\":\"" + validStage + "\"", "\"stage\":\"runtime\"");
                client.SendRaw(json);

                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
                Assert.That(error.InReplyTo, Is.EqualTo(2));
                Assert.That(error.Message, Does.Contain("stage"));
                Assert.That(_source.Stages, Is.Empty, "an invalid stage must not reach the source");
            }
        }

        [Test]
        public void Inspect_WhenSourceThrows_ReturnsInternalWithoutData()
        {
            _source.InspectImpl = _ => throw new InvalidOperationException("boom");

            using (TestClient client = Connect(_server))
            {
                client.Send(Inspect("inventory", seq: 2));

                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Internal));
                Assert.That(error.InReplyTo, Is.EqualTo(2));
                Assert.That(error.Message, Does.Contain("inspection source failed"));
            }
        }

        [Test]
        public void Inspect_RejectsARequestFromAnotherSession()
        {
            using (TestClient client = Connect(_server))
            {
                InspectRequestMessage request = Inspect("discovery", seq: 2);
                request.Session = "another-session";
                client.Send(request);
                BridgeErrorMessage error = client.ReadError();
                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Protocol));
                Assert.That(error.InReplyTo, Is.EqualTo(2));
                Assert.That(_source.Stages, Is.Empty);
            }
        }

        [Test]
        public void Inspect_RejectsAnUnsupportedProtocol()
        {
            using (TestClient client = Connect(_server))
            {
                InspectRequestMessage request = Inspect("discovery", seq: 2);
                request.Protocol = "upa/99";
                client.Send(request);
                BridgeErrorMessage error = client.ReadError();
                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Protocol));
                Assert.That(error.InReplyTo, Is.EqualTo(2));
                Assert.That(_source.Stages, Is.Empty);
            }
        }

        [Test]
        public void Inspect_WhenStageIsUnavailable_ReturnsTheReason()
        {
            _source.InspectImpl = _ => throw new BridgeInspectionUnavailableException("the requested stage requires Play mode");
            using (TestClient client = Connect(_server))
            {
                client.Send(Inspect("discovery", seq: 2));
                BridgeErrorMessage error = client.ReadError();
                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
                Assert.That(error.InReplyTo, Is.EqualTo(2));
                Assert.That(error.Message, Does.Contain("requires Play mode"));
            }
        }

        [Test]
        public void Inspect_WhenSourceReturnsNoSnapshot_ReturnsInternal()
        {
            _source.InspectImpl = _ => null;

            using (TestClient client = Connect(_server))
            {
                client.Send(Inspect("inventory", seq: 2));

                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Internal));
                Assert.That(error.InReplyTo, Is.EqualTo(2));
                Assert.That(error.Message, Does.Contain("no snapshot"));
            }
        }

        [Test]
        public void Inspect_WhenSourceReturnsInvalidCaps_ReturnsInternal()
        {
            _source.InspectImpl = _ => new BridgeInspectionSnapshot
            {
                Caps = new Dictionary<string, InspectionCap>
                {
                    ["records"] = new InspectionCap
                    {
                        Limit = 10,
                        Observed = 20,
                        Returned = 11,
                        Truncated = true,
                    },
                },
            };

            using (TestClient client = Connect(_server))
            {
                client.Send(Inspect("inventory", seq: 2));

                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Internal));
                Assert.That(error.InReplyTo, Is.EqualTo(2));
                Assert.That(error.Message, Does.Contain("invalid caps"));
                Assert.That(error.Message, Does.Contain("limit"));
            }
        }

        private BridgeServer StartServer(IBridgeInspectionSource inspection)
        {
            var server = new BridgeServer(
                new BridgeServerOptions
                {
                    Port = 0,
                    HandshakeTimeoutMs = 3000,
                    ReadTimeoutMs = 0,
                },
                _status,
                null,
                null,
                null,
                null,
                inspection,
                BridgeTestSupport.Token);
            server.Start();
            return server;
        }

        private static TestClient Connect(BridgeServer server)
        {
            var client = new TestClient(server);
            client.Send(BridgeTestSupport.Hello(session: "s-inspect"));
            client.ReadStatus();
            return client;
        }

        private static InspectRequestMessage Inspect(string stage, long seq)
        {
            return new InspectRequestMessage
            {
                Protocol = ProtocolVersion.Current,
                Session = "s-inspect",
                Seq = seq,
                GameS = null,
                WallUtc = DateTime.UtcNow,
                Stage = stage,
            };
        }

        private static InspectionResultMessage ReadInspection(TestClient client)
        {
            string line = client.ReadLine();
            Assert.That(
                BridgeProtocol.TryReadInspectionResult(line, out InspectionResultMessage result, out ProtocolError error),
                Is.True,
                error?.Message);
            return result;
        }
    }
}
