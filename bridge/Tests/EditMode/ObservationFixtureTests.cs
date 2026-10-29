// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.IO;
using NUnit.Framework;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Contract conformance for the observation side of the shared fixtures. A fixture the Python
    /// models accept must parse here, a fixture they reject must be rejected here with an
    /// <c>invalid</c> error, and every valid message must survive a two-pass round trip unchanged.
    /// </summary>
    [TestFixture]
    public sealed class ObservationFixtureTests
    {
        private const string ObservationContract = "observation";
        private const string ObserveRequestContract = "observe_request";

        private const string ValidPrefix = "valid-";
        private const string InvalidPrefix = "invalid-";
        private const string InvalidCode = "invalid";

        private static readonly string[] Contracts =
        {
            ObservationContract, ObserveRequestContract,
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
                case ObservationContract:
                {
                    bool accepted = BridgeProtocol.TryReadObservation(json, out ObservationMessage observation, out error);
                    message = observation;
                    return accepted;
                }

                case ObserveRequestContract:
                {
                    bool accepted = BridgeProtocol.TryReadObserveRequest(json, out ObserveRequestMessage request, out error);
                    message = request;
                    return accepted;
                }

                default:
                    throw new ArgumentOutOfRangeException(
                        nameof(contract), contract, "not an observation wire contract");
            }
        }

        private static string WireType(string contract)
        {
            return contract == ObserveRequestContract ? "observe" : "observation";
        }
    }
}
