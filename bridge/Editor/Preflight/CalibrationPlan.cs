// SPDX-License-Identifier: Apache-2.0
using System;
using System.IO;
using UnityEngine;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>
    /// The wall-clock plan a calibration client declares before its first trial. The Unity host sizes
    /// its own limits from it, so they scale with the planned controls and trials instead of being a
    /// fixed number. The client owns the numbers; this class only reads them and turns them into a
    /// test timeout. Nothing here selects, ranks or reacts to what the game does.
    /// </summary>
    public static class CalibrationPlan
    {
        /// <summary>
        /// Largest budget the host accepts. A larger or non-finite value is a defect in the client,
        /// not a plan, and must not leave the Editor waiting for hours.
        /// </summary>
        public const double MaximumBudgetS = 4 * 3600;

        [Serializable]
        private sealed class PlanFile
        {
            public int schema_version;
            public int planned_controls;
            public int planned_trials;
            public double wall_budget_s;
        }

        /// <summary>The plan file that belongs next to a calibration output file.</summary>
        public static string PathFor(string calibrationOutput)
        {
            if (string.IsNullOrEmpty(calibrationOutput)) throw new ArgumentException("an output path is required", nameof(calibrationOutput));
            return Path.ChangeExtension(calibrationOutput, ".plan.json");
        }

        /// <summary>
        /// Reads the declared budget. Returns false with a null error while the plan has not been
        /// written yet, and false with a message when it exists but cannot be used.
        /// </summary>
        public static bool TryRead(string path, out double wallBudgetS, out string error)
        {
            wallBudgetS = 0;
            error = null;
            if (!File.Exists(path)) return false;
            PlanFile plan;
            try
            {
                plan = JsonUtility.FromJson<PlanFile>(File.ReadAllText(path));
            }
            catch (Exception ex)
            {
                error = "the calibration plan is unreadable: " + ex.Message;
                return false;
            }
            if (plan == null || plan.schema_version != 1)
            {
                error = "the calibration plan has an unsupported schema version";
                return false;
            }
            double budget = plan.wall_budget_s;
            if (double.IsNaN(budget) || double.IsInfinity(budget) || budget <= 0 || budget > MaximumBudgetS)
            {
                error = "the calibration plan declares a wall-clock budget outside (0, " + MaximumBudgetS + "] s";
                return false;
            }
            wallBudgetS = budget;
            return true;
        }

        /// <summary>
        /// Absolute test timeout, in milliseconds from the start of the test, that covers the time
        /// already spent plus the work still to come.
        /// </summary>
        public static int TestTimeoutMs(double elapsedS, double remainingS)
        {
            if (double.IsNaN(elapsedS) || double.IsInfinity(elapsedS) || elapsedS < 0)
                throw new ArgumentOutOfRangeException(nameof(elapsedS));
            if (double.IsNaN(remainingS) || double.IsInfinity(remainingS) || remainingS < 0)
                throw new ArgumentOutOfRangeException(nameof(remainingS));
            double milliseconds = Math.Ceiling((elapsedS + remainingS) * 1000.0);
            return milliseconds >= int.MaxValue ? int.MaxValue : (int)milliseconds;
        }
    }
}
