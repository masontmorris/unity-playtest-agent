using System;
using System.Collections.Generic;
using System.IO;
using NUnit.Framework;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Tests
{
    [TestFixture]
    public sealed class ActFixtureTests
    {
        private static readonly string[] Contracts = { "act_request", "execution_record" };

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
                Assert.That(accepted, Is.True, error?.Message);
                Assert.That(message, Is.Not.Null);
                string once = BridgeProtocol.Serialize(message);
                Assert.That(TryRead(contract, once, out Envelope second, out ProtocolError secondError), Is.True, secondError?.Message);
                Assert.That(BridgeProtocol.Serialize(second), Is.EqualTo(once), contract + " changed between parse and serialize");
            }
            else
            {
                Assert.That(accepted, Is.False, contract + " should reject " + path);
                Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid));
            }
        }

        private static bool TryRead(string contract, string json, out Envelope message, out ProtocolError error)
        {
            switch (contract)
            {
                case "act_request":
                {
                    bool accepted = BridgeProtocol.TryReadActRequest(json, out ActRequestMessage request, out error);
                    message = request;
                    return accepted;
                }

                case "execution_record":
                {
                    bool accepted = BridgeProtocol.TryReadExecutionRecord(json, out ExecutionRecordMessage record, out error);
                    message = record;
                    return accepted;
                }

                default:
                    throw new ArgumentOutOfRangeException(nameof(contract), contract, "not an act contract");
            }
        }
    }
}
