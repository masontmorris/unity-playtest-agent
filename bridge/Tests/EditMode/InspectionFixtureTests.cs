using System;
using System.Collections.Generic;
using System.IO;
using NUnit.Framework;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Shared inspector fixtures. The same files the Python contract suite validates must parse
    /// here, and every invalid fixture must fail on its stated rule rather than on a missing field
    /// or a wrong JSON type.
    /// </summary>
    [TestFixture]
    public sealed class InspectionFixtureTests
    {
        private const string RequestContract = "inspect_request";
        private const string ResultContract = "inspection_result";

        private static readonly string[] Contracts = { RequestContract, ResultContract };

        public static IEnumerable<TestCaseData> Fixtures()
        {
            foreach (string contract in Contracts)
            {
                string directory = Path.Combine(TestPaths.Contracts, contract);
                string[] files = Directory.GetFiles(directory, "*.json");
                Array.Sort(files, StringComparer.Ordinal);
                foreach (string file in files)
                {
                    yield return new TestCaseData(contract, file).SetName(contract + "/" + Path.GetFileNameWithoutExtension(file));
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
                Assert.That(accepted, Is.True, contract + " should accept " + path + ": " + error?.Message);
                Assert.That(message, Is.Not.Null);
                Assert.That(message.Type, Is.EqualTo(WireType(contract)));

                string once = BridgeProtocol.Serialize(message);
                Assert.That(
                    TryRead(contract, once, out Envelope second, out ProtocolError secondError),
                    Is.True,
                    "serialized " + contract + " must parse again: " + secondError?.Message);
                Assert.That(
                    BridgeProtocol.Serialize(second),
                    Is.EqualTo(once),
                    contract + " changed between parse and serialize");
                return;
            }

            Assert.That(accepted, Is.False, contract + " should reject " + path);
            Assert.That(error, Is.Not.Null, "a rejection must carry an error");
            Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
        }

        [TestCase("invalid-returned-over-limit.json", "returned must not exceed limit")]
        [TestCase("invalid-observed-below-returned.json", "observed must be at least returned")]
        [TestCase("invalid-truncated-false-below-observed.json", "truncated is false")]
        [TestCase("invalid-truncated-true-complete.json", "truncated is true")]
        public void CapFixture_FailsForItsIntendedRule(string file, string reason)
        {
            string path = Path.Combine(TestPaths.Contracts, ResultContract, file);

            bool accepted = BridgeProtocol.TryReadInspectionResult(
                File.ReadAllText(path),
                out InspectionResultMessage _,
                out ProtocolError error);

            Assert.That(accepted, Is.False, file + " should be rejected");
            Assert.That(error.Message, Does.Contain(reason), file + " failed for the wrong reason: " + error.Message);
        }

        private static bool TryRead(string contract, string json, out Envelope message, out ProtocolError error)
        {
            switch (contract)
            {
                case RequestContract:
                {
                    bool accepted = BridgeProtocol.TryReadInspectRequest(json, out InspectRequestMessage request, out error);
                    message = request;
                    return accepted;
                }

                case ResultContract:
                {
                    bool accepted = BridgeProtocol.TryReadInspectionResult(json, out InspectionResultMessage result, out error);
                    message = result;
                    return accepted;
                }

                default:
                    throw new ArgumentOutOfRangeException(nameof(contract), contract, "not an inspection contract");
            }
        }

        private static string WireType(string contract)
        {
            return contract == RequestContract ? "inspect" : "inspection";
        }
    }
}
