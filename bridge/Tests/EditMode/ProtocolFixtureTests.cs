using System;
using System.Collections.Generic;
using System.IO;
using NUnit.Framework;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Contract conformance against the fixtures the Python suite uses. A fixture the models accept
    /// must parse here, a fixture they reject must be rejected here, and a valid message must survive
    /// a parse/serialize/parse/serialize round trip unchanged. Drift on either side fails the suite.
    /// </summary>
    [TestFixture]
    public sealed class ProtocolFixtureTests
    {
        private const string HelloContract = "hello";
        private const string StatusContract = "status";
        private const string ErrorContract = "bridge_error";

        private const string ValidPrefix = "valid-";
        private const string InvalidPrefix = "invalid-";
        private const string InvalidCode = "invalid";

        private static readonly string[] Contracts =
        {
            HelloContract, StatusContract, ErrorContract,
        };

        public static IEnumerable<TestCaseData> AllFixtures()
        {
            return Cases("classify", null);
        }

        public static IEnumerable<TestCaseData> ValidFixtures()
        {
            return Cases("roundtrip", ValidPrefix);
        }

        [TestCaseSource(nameof(AllFixtures))]
        public void Fixture_MatchesItsLabel(string contract, string path)
        {
            string json = File.ReadAllText(path);
            string name = Path.GetFileNameWithoutExtension(path);
            bool labelledValid = name.StartsWith(ValidPrefix, StringComparison.Ordinal);
            bool labelledInvalid = name.StartsWith(InvalidPrefix, StringComparison.Ordinal);

            if (!labelledValid && !labelledInvalid)
            {
                Assert.Fail("unlabelled fixture (expected valid-*.json or invalid-*.json): " + path);
            }

            bool accepted = TryRead(contract, json, out Envelope message, out ProtocolError error);

            if (labelledValid)
            {
                Assert.That(accepted, Is.True, contract + " should accept " + path + ": " + error?.Message);
                Assert.That(message, Is.Not.Null);
                Assert.That(message.Type, Is.EqualTo(WireType(contract)), "wire type of " + path);
                return;
            }

            Assert.That(accepted, Is.False, contract + " should reject " + path);
            Assert.That(error, Is.Not.Null, "a rejection must carry an error");
            Assert.That(error.Code, Is.EqualTo(InvalidCode), "a malformed message is an 'invalid' error");
        }

        [TestCaseSource(nameof(ValidFixtures))]
        public void ValidFixture_RoundTripsUnchanged(string contract, string path)
        {
            string json = File.ReadAllText(path);

            Assert.That(
                TryRead(contract, json, out Envelope first, out ProtocolError firstError),
                Is.True,
                firstError?.Message);
            string once = BridgeProtocol.Serialize(first);

            Assert.That(
                TryRead(contract, once, out Envelope second, out ProtocolError secondError),
                Is.True,
                "serialized " + contract + " must parse again: " + secondError?.Message);
            string twice = BridgeProtocol.Serialize(second);

            Assert.That(
                twice,
                Is.EqualTo(once),
                contract + " changed between parse and serialize (a field was dropped or renamed)");
        }

        [Test]
        public void EveryContractHasAtLeastOneValidFixture()
        {
            foreach (string contract in Contracts)
            {
                string directory = Path.Combine(TestPaths.Contracts, contract);
                Assert.That(
                    Directory.GetFiles(directory, ValidPrefix + "*.json").Length,
                    Is.GreaterThan(0),
                    "no valid fixture for " + contract);
            }
        }

        [Test]
        public void Hello_WithUnknownMember_IsRejected()
        {
            string json = HelloJson(extraMember: ",\n  \"surprise\": true");

            Assert.That(BridgeProtocol.TryReadHello(json, out HelloMessage _, out ProtocolError error), Is.False);
            Assert.That(error.Code, Is.EqualTo(InvalidCode));
            Assert.That(error.Message, Does.Contain("surprise"));
        }

        [Test]
        public void Hello_WhoseTypeIsStatus_IsRejected()
        {
            string json = HelloJson(type: StatusContract);

            Assert.That(BridgeProtocol.TryReadHello(json, out HelloMessage _, out ProtocolError error), Is.False);
            Assert.That(error.Code, Is.EqualTo(InvalidCode));
        }

        [Test]
        public void Status_WithUnknownInputBackend_IsRejected()
        {
            string json = StatusJson(backends: "\"input_system\", \"unknown_backend\"");

            Assert.That(BridgeProtocol.TryReadStatus(json, out StatusMessage _, out ProtocolError error), Is.False);
            Assert.That(error.Code, Is.EqualTo(InvalidCode));
            Assert.That(error.Message, Does.Contain("unknown_backend"));
        }

        [Test]
        public void Status_WithUnknownInputBackendAmongKnownOnes_IsRejected()
        {
            string json = StatusJson(backends: "\"legacy\", \"unknown_backend\"");

            Assert.That(BridgeProtocol.TryReadStatus(json, out StatusMessage _, out ProtocolError error), Is.False);
            Assert.That(error.Code, Is.EqualTo(InvalidCode));
        }

        [Test]
        public void Error_WithUnknownCode_IsRejected()
        {
            string json = ErrorJson(code: "timeout");

            Assert.That(BridgeProtocol.TryReadBridgeError(json, out BridgeErrorMessage _, out ProtocolError error), Is.False);
            Assert.That(error.Code, Is.EqualTo(InvalidCode));
        }

        [Test]
        public void Hello_WithUnsupportedButWellFormedProtocol_Parses()
        {
            string json = HelloJson(protocol: "upa/9");

            Assert.That(
                BridgeProtocol.TryReadHello(json, out HelloMessage hello, out ProtocolError error),
                Is.True,
                "the parser must report facts, not decide support: " + error?.Message);
            Assert.That(hello.Protocol, Is.EqualTo("upa/9"));

            Assert.That(ProtocolVersion.IsWellFormed("upa/9"), Is.True);
            Assert.That(ProtocolVersion.IsSupported("upa/9"), Is.False);
            Assert.That(ProtocolVersion.IsWellFormed(ProtocolVersion.Current), Is.True);
            Assert.That(ProtocolVersion.IsSupported(ProtocolVersion.Current), Is.True);
        }

        [Test]
        public void Hello_WithoutType_ParsesWithSchemaDefault()
        {
            string json = HelloJson(type: null);

            Assert.That(
                BridgeProtocol.TryReadHello(json, out HelloMessage hello, out ProtocolError error),
                Is.True,
                "a missing 'type' takes its schema default: " + error?.Message);
            Assert.That(hello.Type, Is.EqualTo(HelloContract));
        }

        [Test]
        public void Hello_WithWrongExplicitType_IsRejected()
        {
            string json = HelloJson(type: "nonsense");

            Assert.That(BridgeProtocol.TryReadHello(json, out HelloMessage _, out ProtocolError error), Is.False);
            Assert.That(error.Code, Is.EqualTo(InvalidCode));
        }

        [Test]
        public void Hello_WithTypeOfWrongJsonKind_IsRejected()
        {
            string json = HelloJson();
            json = json.Replace("\"type\": \"hello\"", "\"type\": 7");

            Assert.That(BridgeProtocol.TryReadHello(json, out HelloMessage _, out ProtocolError error), Is.False);
            Assert.That(error.Code, Is.EqualTo(InvalidCode));
        }

        private static IEnumerable<TestCaseData> Cases(string label, string prefix)
        {
            foreach (string contract in Contracts)
            {
                string directory = Path.Combine(TestPaths.Contracts, contract);
                if (!Directory.Exists(directory))
                {
                    throw new DirectoryNotFoundException("missing fixture directory: " + directory);
                }

                string[] files = Directory.GetFiles(directory, "*.json");
                Array.Sort(files, StringComparer.Ordinal);
                foreach (string file in files)
                {
                    string name = Path.GetFileNameWithoutExtension(file);
                    if (prefix != null && !name.StartsWith(prefix, StringComparison.Ordinal))
                    {
                        continue;
                    }

                    yield return new TestCaseData(contract, file).SetName(label + " " + contract + "/" + name);
                }
            }
        }

        private static bool TryRead(string contract, string json, out Envelope message, out ProtocolError error)
        {
            switch (contract)
            {
                case HelloContract:
                {
                    bool accepted = BridgeProtocol.TryReadHello(json, out HelloMessage hello, out error);
                    message = hello;
                    return accepted;
                }

                case StatusContract:
                {
                    bool accepted = BridgeProtocol.TryReadStatus(json, out StatusMessage status, out error);
                    message = status;
                    return accepted;
                }

                case ErrorContract:
                {
                    bool accepted = BridgeProtocol.TryReadBridgeError(json, out BridgeErrorMessage bridgeError, out error);
                    message = bridgeError;
                    return accepted;
                }

                default:
                    throw new ArgumentOutOfRangeException(
                        nameof(contract), contract, "not a bridge wire contract");
            }
        }

        private static string WireType(string contract)
        {
            return contract == ErrorContract ? "error" : contract;
        }

        private static string HelloJson(
            string protocol = "upa/0",
            string type = HelloContract,
            string extraMember = "")
        {
            string typeMember = type == null ? string.Empty : "  \"type\": \"" + type + "\",\n";
            return "{\n"
                + "  \"protocol\": \"" + protocol + "\",\n"
                + "  \"session\": \"s-1\",\n"
                + "  \"seq\": 1,\n"
                + "  \"game_s\": 12.5,\n"
                + "  \"wall_utc\": \"2026-10-26T18:00:00Z\",\n"
                + typeMember
                + "  \"token\": \"local-token\",\n"
                + "  \"core_version\": \"0.0.1\""
                + extraMember
                + "\n}";
        }

        private static string StatusJson(string backends = "\"input_system\"")
        {
            return "{\n"
                + "  \"protocol\": \"upa/0\",\n"
                + "  \"session\": \"s-1\",\n"
                + "  \"seq\": 1,\n"
                + "  \"wall_utc\": \"2026-10-26T18:00:00Z\",\n"
                + "  \"type\": \"status\",\n"
                + "  \"bridge_version\": \"0.0.1\",\n"
                + "  \"unity_version\": \"6000.5.4f1\",\n"
                + "  \"project\": \"Example\",\n"
                + "  \"input_backends\": [" + backends + "],\n"
                + "  \"clock_control\": true,\n"
                + "  \"capture\": true,\n"
                + "  \"capture_self_test\": \"passed\",\n"
                + "  \"lock\": { \"held\": true }\n"
                + "}";
        }

        private static string ErrorJson(string code = "busy")
        {
            return "{\n"
                + "  \"protocol\": \"upa/0\",\n"
                + "  \"session\": \"s-1\",\n"
                + "  \"seq\": 1,\n"
                + "  \"wall_utc\": \"2026-10-26T18:00:00Z\",\n"
                + "  \"type\": \"error\",\n"
                + "  \"code\": \"" + code + "\",\n"
                + "  \"message\": \"project locked by pid 4242\"\n"
                + "}";
        }
    }
}
