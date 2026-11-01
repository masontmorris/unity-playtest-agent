// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.IO;
using NUnit.Framework;
using Playtest.Bridge.Actions;
using Playtest.Bridge.Protocol;
using Playtest.Bridge.Transport;

namespace Playtest.Bridge.Tests
{
    /// <summary>A placement source whose answer each test controls; it records what it was asked.</summary>
    internal sealed class FakePlacementSource : IBridgePlacementSource
    {
        public BridgePlacementRequest LastRequest;
        public int Calls;
        public Func<BridgePlacementRequest, BridgePlacementResult> Respond;
        public Action<BridgePlacementRequest> BeforeRespond;

        public BridgePlacementResult Place(BridgePlacementRequest request)
        {
            Calls++;
            LastRequest = request;
            BeforeRespond?.Invoke(request);
            if (Respond != null)
            {
                return Respond(request);
            }

            return new BridgePlacementResult
            {
                Placed = true,
                PositionAfterSettle = new List<double>(request.Position),
                MovedDuringSettle = 0,
            };
        }
    }

    [TestFixture]
    public sealed class PlaceFixtureTests
    {
        private static readonly string[] Contracts = { "place_request", "place_result" };

        public static IEnumerable<TestCaseData> Fixtures()
        {
            foreach (string contract in Contracts)
            {
                string directory = Path.Combine(TestPaths.Contracts, contract);
                Assert.That(Directory.Exists(directory), Is.True, "missing fixture directory: " + directory);
                string[] files = Directory.GetFiles(directory, "*.json");
                Array.Sort(files, StringComparer.Ordinal);
                foreach (string file in files)
                {
                    yield return new TestCaseData(contract, file)
                        .SetName(contract + "/" + Path.GetFileNameWithoutExtension(file));
                }
            }
        }

        [TestCaseSource(nameof(Fixtures))]
        public void Fixture_MatchesItsLabel(string contract, string path)
        {
            string json = File.ReadAllText(path);
            bool valid = Path.GetFileNameWithoutExtension(path).StartsWith("valid-", StringComparison.Ordinal);
            bool accepted = TryRead(contract, json, out Envelope message, out ProtocolError error);

            if (valid)
            {
                Assert.That(accepted, Is.True, error?.Message);
                Assert.That(message, Is.Not.Null);
                string once = BridgeProtocol.Serialize(message);
                Assert.That(
                    TryRead(contract, once, out Envelope second, out ProtocolError secondError),
                    Is.True,
                    "serialized " + contract + " must parse again: " + secondError?.Message);
                Assert.That(
                    BridgeProtocol.Serialize(second),
                    Is.EqualTo(once),
                    contract + " changed between parse and serialize");
            }
            else
            {
                Assert.That(accepted, Is.False, contract + " should reject " + path);
                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
            }
        }

        [Test]
        public void Request_ParsesPositionAndSettle()
        {
            string json = RequestJson(position: "[-11.7, 1.5, 0.0]", settleS: "0.25");

            Assert.That(
                BridgeProtocol.TryReadPlaceRequest(json, out PlaceRequestMessage request, out ProtocolError error),
                Is.True,
                error?.Message);
            Assert.That(request.Type, Is.EqualTo("place"));
            Assert.That(request.Position, Is.EqualTo(new[] { -11.7, 1.5, 0.0 }));
            Assert.That(request.SettleS, Is.EqualTo(0.25).Within(1e-9));
        }

        [TestCase("0.0")]
        [TestCase("-0.25")]
        public void Request_WithANonPositiveSettle_IsRejected(string settleS)
        {
            string json = RequestJson(settleS: settleS);

            Assert.That(BridgeProtocol.TryReadPlaceRequest(json, out PlaceRequestMessage _, out ProtocolError error), Is.False);
            Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
            Assert.That(error.Message, Does.Contain("settle_s"));
        }

        [TestCase("[1.0, 2.0]")]
        [TestCase("[1.0, 2.0, 3.0, 4.0]")]
        [TestCase("[1.0, \"2\", 3.0]")]
        [TestCase("null")]
        public void Request_WithAMalformedPosition_IsRejected(string position)
        {
            string json = RequestJson(position: position);

            Assert.That(BridgeProtocol.TryReadPlaceRequest(json, out PlaceRequestMessage _, out ProtocolError error), Is.False);
            Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
            Assert.That(error.Message, Does.Contain("position"));
        }

        [Test]
        public void Result_PlacedCarriesItsMeasurement()
        {
            string json = ResultJson(placed: true, position: "[1.0, 2.0, 3.0]", moved: "0.5");

            Assert.That(
                BridgeProtocol.TryReadPlaceResult(json, out PlaceResultMessage result, out ProtocolError error),
                Is.True,
                error?.Message);
            Assert.That(result.Type, Is.EqualTo("placed"));
            Assert.That(result.Placed, Is.True);
            Assert.That(result.PositionAfterSettle, Is.EqualTo(new[] { 1.0, 2.0, 3.0 }));
            Assert.That(result.MovedDuringSettle, Is.EqualTo(0.5).Within(1e-9));
            Assert.That(result.Reason, Is.Null);
        }

        [Test]
        public void Result_UnplacedCarriesOnlyItsReason()
        {
            string json = ResultJson(placed: false, reason: "the project is not playing");

            Assert.That(
                BridgeProtocol.TryReadPlaceResult(json, out PlaceResultMessage result, out ProtocolError error),
                Is.True,
                error?.Message);
            Assert.That(result.Placed, Is.False);
            Assert.That(result.PositionAfterSettle, Is.Null);
            Assert.That(result.MovedDuringSettle, Is.Null);
            Assert.That(result.Reason, Is.EqualTo("the project is not playing"));
        }

        [Test]
        public void Result_PlacedWithAReason_IsRejected()
        {
            string json = ResultJson(placed: true, position: "[1.0, 2.0, 3.0]", moved: "0.0", reason: "not really");

            Assert.That(BridgeProtocol.TryReadPlaceResult(json, out PlaceResultMessage _, out ProtocolError error), Is.False);
            Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
        }

        [Test]
        public void Result_WithANegativeMovement_IsRejected()
        {
            string json = ResultJson(placed: true, position: "[1.0, 2.0, 3.0]", moved: "-0.5");

            Assert.That(BridgeProtocol.TryReadPlaceResult(json, out PlaceResultMessage _, out ProtocolError error), Is.False);
            Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
            Assert.That(error.Message, Does.Contain("moved_during_settle"));
        }

        [Test]
        public void Result_WithoutPlaced_IsRejected()
        {
            string json = "{\n"
                + "  \"protocol\": \"" + ProtocolVersion.Current + "\",\n"
                + "  \"session\": \"s-1\",\n"
                + "  \"seq\": 1,\n"
                + "  \"wall_utc\": \"2026-10-26T18:00:00Z\",\n"
                + "  \"type\": \"placed\",\n"
                + "  \"reason\": \"the project is not playing\"\n"
                + "}";

            Assert.That(BridgeProtocol.TryReadPlaceResult(json, out PlaceResultMessage _, out ProtocolError error), Is.False);
            Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
            Assert.That(error.Message, Does.Contain("placed"));
        }

        private static bool TryRead(string contract, string json, out Envelope message, out ProtocolError error)
        {
            switch (contract)
            {
                case "place_request":
                {
                    bool accepted = BridgeProtocol.TryReadPlaceRequest(json, out PlaceRequestMessage request, out error);
                    message = request;
                    return accepted;
                }

                case "place_result":
                {
                    bool accepted = BridgeProtocol.TryReadPlaceResult(json, out PlaceResultMessage result, out error);
                    message = result;
                    return accepted;
                }

                default:
                    throw new ArgumentOutOfRangeException(nameof(contract), contract, "not a place contract");
            }
        }

        private static string RequestJson(string position = "[1.0, 2.0, 3.0]", string settleS = "0.25")
        {
            return "{\n"
                + "  \"protocol\": \"" + ProtocolVersion.Current + "\",\n"
                + "  \"session\": \"s-1\",\n"
                + "  \"seq\": 1,\n"
                + "  \"wall_utc\": \"2026-10-26T18:00:00Z\",\n"
                + "  \"type\": \"place\",\n"
                + "  \"position\": " + position + ",\n"
                + "  \"settle_s\": " + settleS + "\n"
                + "}";
        }

        private static string ResultJson(
            bool placed,
            string position = "null",
            string moved = "null",
            string reason = "null")
        {
            return "{\n"
                + "  \"protocol\": \"" + ProtocolVersion.Current + "\",\n"
                + "  \"session\": \"s-1\",\n"
                + "  \"seq\": 1,\n"
                + "  \"wall_utc\": \"2026-10-26T18:00:00Z\",\n"
                + "  \"type\": \"placed\",\n"
                + "  \"placed\": " + (placed ? "true" : "false") + ",\n"
                + "  \"position_after_settle\": " + position + ",\n"
                + "  \"moved_during_settle\": " + moved + ",\n"
                + "  \"reason\": " + reason + "\n"
                + "}";
        }
    }

    [TestFixture]
    public sealed class BridgePlaceTests
    {
        private BridgeServer _server;
        private FakeStatusSource _status;
        private ActFakeObservationSource _source;
        private FakeInputBackend _input;
        private FakePlacementSource _placement;

        [SetUp]
        public void SetUp()
        {
            _status = new FakeStatusSource();
            _source = new ActFakeObservationSource();
            _input = new FakeInputBackend();
            _placement = new FakePlacementSource();
            _server = new BridgeServer(
                new BridgeServerOptions
                {
                    Port = 0,
                    HandshakeTimeoutMs = 3000,
                    ReadTimeoutMs = 0,
                    ReleaseVerifyTimeoutMs = 100,
                },
                _status,
                _source,
                Catalog(),
                _input,
                null,
                null,
                _placement,
                BridgeTestSupport.Token);
            _server.Start();
        }

        [TearDown]
        public void TearDown()
        {
            _server?.Dispose();
        }

        [Test]
        public void Place_IsAnsweredWithTheMeasuredPosition()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                client.Send(Place(new[] { 4.0, -1.5, 2.0 }, 0.3));

                PlaceResultMessage result = ReadPlaced(client);

                Assert.That(result.Placed, Is.True);
                Assert.That(result.PositionAfterSettle, Is.EqualTo(new[] { 4.0, -1.5, 2.0 }));
                Assert.That(result.MovedDuringSettle, Is.EqualTo(0.0).Within(1e-9));
                Assert.That(result.Reason, Is.Null);
                Assert.That(result.Session, Is.EqualTo("s-place"));
                Assert.That(_placement.Calls, Is.EqualTo(1));
                Assert.That(_placement.LastRequest.Position, Is.EqualTo(new[] { 4.0, -1.5, 2.0 }));
                Assert.That(_placement.LastRequest.SettleS, Is.EqualTo(0.3).Within(1e-9));
            }
        }

        [Test]
        public void Place_ReleasesAHeldActBeforeActing()
        {
            int releasesWhenAsked = -1;
            _placement.BeforeRespond = _ => releasesWhenAsked = _input.Releases;

            using (var client = new TestClient(_server))
            {
                Handshake(client);
                ObservationMessage observation = ObserveOnce(client);
                client.Send(Act("a-held", observation.Seq, leaseS: 5.0));
                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Running));

                client.Send(Place(new[] { 1.0, 2.0, 3.0 }, 0.2));

                Assert.That(ReadExecution(client).Status, Is.EqualTo(BridgeExecStatus.Cancelled));
                Assert.That(ReadPlaced(client).Placed, Is.True);
                Assert.That(releasesWhenAsked, Is.GreaterThanOrEqualTo(1), "held input must be released first");
                Assert.That(_input.Released, Does.Contain("move"));
            }
        }

        [Test]
        public void Place_WhenTheReleaseIsUnverified_IsUnplacedAndTouchesNothing()
        {
            _input.VerifyRelease = false;

            using (var client = new TestClient(_server))
            {
                Handshake(client);
                client.Send(Place(new[] { 1.0, 2.0, 3.0 }, 0.2));

                PlaceResultMessage result = ReadPlaced(client);

                Assert.That(result.Placed, Is.False);
                Assert.That(result.Reason, Does.Contain("released"));
                Assert.That(result.PositionAfterSettle, Is.Null);
                Assert.That(_placement.Calls, Is.Zero, "an unverified release must not move anything");
            }
        }

        [Test]
        public void Place_UnplacedSourceResultKeepsItsReason()
        {
            _placement.Respond = _ => BridgePlacementResult.Unplaced("the project is not playing");

            using (var client = new TestClient(_server))
            {
                Handshake(client);
                client.Send(Place(new[] { 1.0, 2.0, 3.0 }, 0.2));

                PlaceResultMessage result = ReadPlaced(client);

                Assert.That(result.Placed, Is.False);
                Assert.That(result.Reason, Is.EqualTo("the project is not playing"));
                Assert.That(result.PositionAfterSettle, Is.Null);
                Assert.That(result.MovedDuringSettle, Is.Null);
            }
        }

        [Test]
        public void Place_WithAnInvalidResult_IsAnInternalError()
        {
            _placement.Respond = _ => new BridgePlacementResult { Placed = true };

            using (var client = new TestClient(_server))
            {
                Handshake(client);
                client.Send(Place(new[] { 1.0, 2.0, 3.0 }, 0.2));

                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Internal));
                Assert.That(error.Message, Does.Contain("invalid result"));
            }
        }

        [Test]
        public void Place_WhenTheSourceFails_IsAnInternalError()
        {
            _placement.Respond = _ => throw new BridgePlacementUnavailableException("the Editor is not answering");

            using (var client = new TestClient(_server))
            {
                Handshake(client);
                client.Send(Place(new[] { 1.0, 2.0, 3.0 }, 0.2));

                BridgeErrorMessage error = client.ReadError();

                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Internal));
                Assert.That(error.Message, Does.Contain("not answering"));
            }
        }

        [Test]
        public void Place_WithoutAPlacementSource_IsAnInternalError()
        {
            using (var server = new BridgeServer(
                new BridgeServerOptions { Port = 0, HandshakeTimeoutMs = 3000, ReadTimeoutMs = 0 },
                _status,
                _source,
                Catalog(),
                _input,
                null,
                null,
                null,
                BridgeTestSupport.Token))
            {
                server.Start();
                using (var client = new TestClient(server))
                {
                    Handshake(client);
                    client.Send(Place(new[] { 1.0, 2.0, 3.0 }, 0.2));

                    BridgeErrorMessage error = client.ReadError();

                    Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Internal));
                    Assert.That(error.Message, Does.Contain("placement source"));
                    Assert.That(_placement.Calls, Is.Zero);
                }
            }
        }

        [Test]
        public void Place_WithAnInvalidRequest_IsRejectedAndTheSessionSurvives()
        {
            using (var client = new TestClient(_server))
            {
                Handshake(client);
                client.SendRaw(
                    "{\"protocol\":\"" + ProtocolVersion.Current + "\",\"session\":\"s-place\",\"seq\":1,"
                    + "\"wall_utc\":\"2026-10-26T18:00:00Z\",\"type\":\"place\","
                    + "\"position\":[1.0,2.0,3.0],\"settle_s\":0}");

                BridgeErrorMessage error = client.ReadError();
                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
                Assert.That(_placement.Calls, Is.Zero);

                client.Send(Place(new[] { 1.0, 2.0, 3.0 }, 0.2));
                Assert.That(ReadPlaced(client).Placed, Is.True, "the session must survive a rejected place");
            }
        }

        private static BridgeActionCatalog Catalog()
        {
            return BridgeActionCatalog.FromDefinitions(new[]
            {
                new BridgeActionDefinition
                {
                    Name = "move",
                    Backend = "input_system",
                    Device = "Keyboard",
                    Control = "w",
                    DefaultLeaseS = 0.2,
                },
            });
        }

        private static void Handshake(TestClient client, string session = "s-place")
        {
            client.Send(BridgeTestSupport.Hello(session: session));
            client.ReadStatus();
        }

        private static ObservationMessage ObserveOnce(TestClient client)
        {
            client.Send(new ObserveRequestMessage
            {
                Session = "s-place",
                Seq = 1,
                WallUtc = DateTime.UtcNow,
                Mode = "once",
                WithFrame = false,
            });
            string line = client.ReadLine();
            Assert.That(
                BridgeProtocol.TryReadObservation(line, out ObservationMessage observation, out ProtocolError error),
                Is.True,
                error?.Message);
            return observation;
        }

        private static ExecutionRecordMessage ReadExecution(TestClient client)
        {
            string line = client.ReadLine();
            Assert.That(
                BridgeProtocol.TryReadExecutionRecord(line, out ExecutionRecordMessage record, out ProtocolError error),
                Is.True,
                error?.Message);
            return record;
        }

        private static PlaceResultMessage ReadPlaced(TestClient client)
        {
            string line = client.ReadLine();
            Assert.That(
                BridgeProtocol.TryReadPlaceResult(line, out PlaceResultMessage result, out ProtocolError error),
                Is.True,
                error?.Message);
            return result;
        }

        private static PlaceRequestMessage Place(IEnumerable<double> position, double settleS)
        {
            return new PlaceRequestMessage
            {
                Session = "s-place",
                Seq = 1,
                WallUtc = DateTime.UtcNow,
                Position = new List<double>(position),
                SettleS = settleS,
            };
        }

        private static ActRequestMessage Act(string actionId, long basedOnSeq, double leaseS)
        {
            return new ActRequestMessage
            {
                Session = "s-place",
                Seq = 1,
                WallUtc = DateTime.UtcNow,
                ActionId = actionId,
                ChoiceId = "c1",
                Action = "move",
                Params = new Dictionary<string, object>(),
                BasedOnSeq = basedOnSeq,
                MaxAgeS = 1.0,
                DependsOn = new List<string>(),
                LeaseS = leaseS,
            };
        }
    }
}
