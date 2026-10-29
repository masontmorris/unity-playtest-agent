using System;
using System.Collections.Generic;
using NUnit.Framework;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// The per-input receipts of an execution record. The reader is strict: a receipt with an
    /// unknown member, an unsupported device or a guessed measurement is invalid, never repaired.
    /// Python owns the contract; this is the bridge's local check until the shared fixtures arrive.
    /// </summary>
    [TestFixture]
    public sealed class AppliedInputReceiptProtocolTests
    {
        private const string Valid =
            "{\"device\":\"Keyboard\",\"control\":\"w\",\"step_index\":0,\"x\":null,\"y\":null,"
            + "\"input_start_wall_s\":1.5,\"input_end_wall_s\":2.0,\"applied_duration_s\":0.5,"
            + "\"applied_delta_x\":null,\"applied_delta_y\":null,\"release_verified\":true}";

        public static IEnumerable<TestCaseData> InvalidReceipts()
        {
            yield return Invalid("unknown member", Valid.Replace("\"device\"", "\"extra\":1,\"device\""));
            yield return Invalid("missing device", Valid.Replace("\"device\":\"Keyboard\",", string.Empty));
            yield return Invalid("unsupported device", Valid.Replace("Keyboard", "Touchscreen"));
            yield return Invalid("missing control", Valid.Replace("\"control\":\"w\",", string.Empty));
            yield return Invalid("empty control", Valid.Replace("\"control\":\"w\"", "\"control\":\"\""));
            yield return Invalid("missing step index", Valid.Replace("\"step_index\":0,", string.Empty));
            yield return Invalid("negative step index", Valid.Replace("\"step_index\":0", "\"step_index\":-1"));
            yield return Invalid("fractional step index", Valid.Replace("\"step_index\":0", "\"step_index\":0.5"));
            yield return Invalid("negative duration", Valid.Replace("\"applied_duration_s\":0.5", "\"applied_duration_s\":-0.5"));
            yield return Invalid("text duration", Valid.Replace("\"applied_duration_s\":0.5", "\"applied_duration_s\":\"0.5\""));
            yield return Invalid("text delta", Valid.Replace("\"applied_delta_x\":null", "\"applied_delta_x\":\"3\""));
            yield return Invalid("text declared x", Valid.Replace("\"x\":null", "\"x\":\"1\""));
            yield return Invalid("declared value outside normalized range", Valid.Replace("\"x\":null", "\"x\":1.1").Replace("\"y\":null", "\"y\":0"));
            yield return Invalid("only one declared vector value", Valid.Replace("\"x\":null", "\"x\":0.5"));
            yield return Invalid("keyboard cannot carry axis values", Valid.Replace("\"x\":null", "\"x\":0.5").Replace("\"y\":null", "\"y\":0"));
            yield return Invalid("end before start", Valid.Replace("\"input_end_wall_s\":2.0", "\"input_end_wall_s\":1.0"));
            yield return Invalid("release flag as text", Valid.Replace("\"release_verified\":true", "\"release_verified\":\"yes\""));
            yield return Invalid("receipt is not an object", "7");
        }

        [Test]
        public void Receipts_RoundTripWithEveryUnmeasuredFieldWrittenAsNull()
        {
            var record = new ExecutionRecordMessage
            {
                Session = "s",
                Seq = 3,
                ActionId = "a1",
                Status = BridgeExecStatus.Running,
                AppliedInputs = new List<AppliedInputReceipt>
                {
                    new AppliedInputReceipt { StepIndex = 0, Device = "Gamepad", Control = "leftStick", X = 0.25, Y = -1, InputStartWallS = 1.5 },
                },
            };

            string json = BridgeProtocol.Serialize(record);

            Assert.That(json, Does.Contain("\"applied_inputs\":[{"));
            foreach (string field in new[] { "input_end_wall_s", "applied_duration_s", "applied_delta_x", "applied_delta_y", "release_verified" })
            {
                Assert.That(json, Does.Contain("\"" + field + "\":null"), field + " is explicitly null, not omitted");
            }

            Assert.That(BridgeProtocol.TryReadExecutionRecord(json, out ExecutionRecordMessage again, out ProtocolError error), Is.True, error?.Message);
            Assert.That(again.AppliedInputs.Count, Is.EqualTo(1));
            Assert.That(again.AppliedInputs[0].X, Is.EqualTo(0.25));
            Assert.That(again.AppliedInputs[0].Y, Is.EqualTo(-1));
            Assert.That(again.AppliedInputs[0].AppliedDurationS, Is.Null, "null stays null, never zero");
            Assert.That(BridgeProtocol.Serialize(again), Is.EqualTo(json));
        }

        [Test]
        public void ARecordWithoutReceiptsIsStillValidAndWritesAnEmptyList()
        {
            string withoutField = Record("completed", null);
            Assert.That(BridgeProtocol.TryReadExecutionRecord(withoutField, out ExecutionRecordMessage record, out ProtocolError error), Is.True, error?.Message);
            Assert.That(record.AppliedInputs, Is.Empty);
            Assert.That(BridgeProtocol.Serialize(record), Does.Contain("\"applied_inputs\":[]"));
        }

        [Test]
        public void AValidReceiptIsAccepted()
        {
            Assert.That(
                BridgeProtocol.TryReadExecutionRecord(Record("completed", "[" + Valid + "]"), out ExecutionRecordMessage record, out ProtocolError error),
                Is.True,
                error?.Message);
            Assert.That(record.AppliedInputs[0].AppliedDurationS, Is.EqualTo(0.5));
            Assert.That(record.AppliedInputs[0].ReleaseVerified, Is.True);
        }

        [TestCaseSource(nameof(InvalidReceipts))]
        public void AnInvalidReceiptRejectsTheWholeRecord(string description, string receipt)
        {
            string json = Record("completed", "[" + receipt + "]");

            Assert.That(
                BridgeProtocol.TryReadExecutionRecord(json, out ExecutionRecordMessage _, out ProtocolError error),
                Is.False,
                description);
            Assert.That(error.Code, Is.EqualTo(BridgeErrorCode.Invalid), description);
        }

        [Test]
        public void ReceiptsThatAreNotAnArrayAreRejected()
        {
            Assert.That(BridgeProtocol.TryReadExecutionRecord(Record("completed", "{}"), out _, out ProtocolError error), Is.False);
            Assert.That(error.Message, Does.Contain("applied_inputs"));
        }

        [Test]
        public void ReceiptsAreRejectedOnOutOfOrderStepsOrARejectedExecution()
        {
            string second = Valid.Replace("\"step_index\":0", "\"step_index\":1");
            string reverse = Record("completed", "[" + second + "," + Valid + "]");
            Assert.That(BridgeProtocol.TryReadExecutionRecord(reverse, out _, out ProtocolError orderError), Is.False);
            Assert.That(orderError.Message, Does.Contain("execution order"));

            string rejected = Record("rejected", "[" + Valid + "]").Replace("\"status\":\"rejected\"", "\"status\":\"rejected\",\"reject_reason\":\"invalid_params\"");
            Assert.That(BridgeProtocol.TryReadExecutionRecord(rejected, out _, out ProtocolError rejectedError), Is.False);
            Assert.That(rejectedError.Message, Does.Contain("cannot have applied-input receipts"));
        }

        private static TestCaseData Invalid(string description, string receipt)
        {
            return new TestCaseData(description, receipt).SetName("invalid receipt: " + description);
        }

        private static string Record(string status, string appliedInputs)
        {
            string tail = appliedInputs == null ? string.Empty : ",\"applied_inputs\":" + appliedInputs;
            return "{\"protocol\":\"" + ProtocolVersion.Current + "\",\"session\":\"s\",\"seq\":1,"
                + "\"wall_utc\":\"2026-10-28T12:00:00Z\",\"type\":\"execution\",\"action_id\":\"a1\","
                + "\"status\":\"" + status + "\"" + tail + "}";
        }
    }
}
