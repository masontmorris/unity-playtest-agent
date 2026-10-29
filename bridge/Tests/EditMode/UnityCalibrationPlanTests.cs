using System;
using System.IO;
using NUnit.Framework;
using Playtest.Bridge.Editor.Preflight;

namespace Playtest.Bridge.Tests
{
    public sealed class UnityCalibrationPlanTests
    {
        private string _directory;

        [SetUp]
        public void SetUp()
        {
            _directory = Path.Combine(Path.GetTempPath(), "upa-calibration-plan-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(_directory);
        }

        [TearDown]
        public void TearDown()
        {
            if (Directory.Exists(_directory)) Directory.Delete(_directory, true);
        }

        private string Write(string text)
        {
            string path = Path.Combine(_directory, "calibration.plan.json");
            File.WriteAllText(path, text);
            return path;
        }

        [Test]
        public void ThePlanFileSitsNextToTheCalibrationOutput()
        {
            string output = Path.Combine(_directory, "calibration.json");
            Assert.That(CalibrationPlan.PathFor(output), Is.EqualTo(Path.Combine(_directory, "calibration.plan.json")));
            Assert.Throws<ArgumentException>(() => CalibrationPlan.PathFor(""));
        }

        [Test]
        public void ADeclaredBudgetIsReadAndAMissingPlanIsNotAnError()
        {
            double budget;
            string error;
            Assert.That(CalibrationPlan.TryRead(Path.Combine(_directory, "absent.plan.json"), out budget, out error), Is.False);
            Assert.That(error, Is.Null, "a plan that has not been written yet is not a failure");

            string path = Write("{\"schema_version\":1,\"planned_controls\":23,\"planned_trials\":276,\"wall_budget_s\":1210.5}");
            Assert.That(CalibrationPlan.TryRead(path, out budget, out error), Is.True, error);
            Assert.That(budget, Is.EqualTo(1210.5));
        }

        [TestCase("{\"schema_version\":1,\"wall_budget_s\":0}")]
        [TestCase("{\"schema_version\":1,\"wall_budget_s\":-3}")]
        [TestCase("{\"schema_version\":1}")]
        [TestCase("{\"schema_version\":1,\"wall_budget_s\":14401}")]
        [TestCase("{\"schema_version\":2,\"wall_budget_s\":60}")]
        [TestCase("not json at all")]
        public void AnUnusablePlanIsRefusedWithAReasonInsteadOfBecomingALimit(string text)
        {
            double budget;
            string error;
            Assert.That(CalibrationPlan.TryRead(Write(text), out budget, out error), Is.False);
            Assert.That(error, Is.Not.Null.And.Not.Empty);
            Assert.That(budget, Is.EqualTo(0));
        }

        [Test]
        public void TheTestTimeoutCoversTimeAlreadySpentPlusTheWorkStillToCome()
        {
            Assert.That(CalibrationPlan.TestTimeoutMs(12.5, 1000), Is.EqualTo(1012500));
            Assert.That(CalibrationPlan.TestTimeoutMs(0, 0.0004), Is.EqualTo(1), "a fraction of a millisecond rounds up, never down");
            Assert.That(CalibrationPlan.TestTimeoutMs(1e9, 1e9), Is.EqualTo(int.MaxValue));
            Assert.Throws<ArgumentOutOfRangeException>(() => CalibrationPlan.TestTimeoutMs(-1, 5));
            Assert.Throws<ArgumentOutOfRangeException>(() => CalibrationPlan.TestTimeoutMs(1, double.NaN));
            Assert.Throws<ArgumentOutOfRangeException>(() => CalibrationPlan.TestTimeoutMs(double.PositiveInfinity, 1));
        }
    }
}
