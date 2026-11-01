// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Transport
{
    /// <summary>A controlled, safe-to-report reason why a placement cannot run.</summary>
    public sealed class BridgePlacementUnavailableException : Exception
    {
        public BridgePlacementUnavailableException(string message) : base(message) { }
    }

    /// <summary>One placement request in the plain form the transport exchanges: no Unity types.</summary>
    public sealed class BridgePlacementRequest
    {
        /// <summary>Requested world position [x, y, z].</summary>
        public List<double> Position { get; set; } = new List<double>();

        /// <summary>Game seconds to wait after setting the pose.</summary>
        public double SettleS { get; set; }
    }

    /// <summary>
    /// What the placement measured. When it could not run, <see cref="Placed"/> is false, the
    /// measurement stays null and <see cref="Reason"/> says why; a position is never invented.
    /// </summary>
    public sealed class BridgePlacementResult
    {
        public bool Placed { get; set; }

        /// <summary>Measured position after the settle wait, or null when nothing was measured.</summary>
        public List<double> PositionAfterSettle { get; set; }

        /// <summary>Distance from the requested position after settle, or null when unknown.</summary>
        public double? MovedDuringSettle { get; set; }

        /// <summary>Why the place did not run; null when it was placed.</summary>
        public string Reason { get; set; }

        public static BridgePlacementResult Unplaced(string reason)
        {
            return new BridgePlacementResult { Placed = false, Reason = reason };
        }
    }

    /// <summary>
    /// Moves the one configured controlled entity and measures where it settled. Called from a
    /// transport thread, so an implementation must marshal to the Unity main thread itself, must
    /// act on no other object and must never invent a measurement. The Editor host supplies and
    /// owns this source; transport never calls Unity APIs.
    /// </summary>
    public interface IBridgePlacementSource
    {
        BridgePlacementResult Place(BridgePlacementRequest request);
    }

    /// <summary>
    /// The cross-field rules a result must satisfy before it can be sent. The exported schema
    /// cannot express them, so the DTO and every producer use this helper; a contradiction is an
    /// error, never a silently dropped or zeroed field.
    /// </summary>
    public static class BridgePlacementRules
    {
        public static bool TryValidate(PlaceResultMessage message, out string error)
        {
            if (message == null)
            {
                error = "the placement source returned no result";
                return false;
            }

            bool hasPosition = message.PositionAfterSettle != null;
            bool hasMoved = message.MovedDuringSettle.HasValue;
            if (message.Placed)
            {
                if (!hasPosition || !hasMoved)
                {
                    error = "a placed result carries position_after_settle and moved_during_settle";
                    return false;
                }

                if (!IsFiniteTriple(message.PositionAfterSettle))
                {
                    error = "position_after_settle must be three finite numbers";
                    return false;
                }

                if (double.IsNaN(message.MovedDuringSettle.Value)
                    || double.IsInfinity(message.MovedDuringSettle.Value)
                    || message.MovedDuringSettle.Value < 0)
                {
                    error = "moved_during_settle must be a non-negative finite number";
                    return false;
                }

                if (!string.IsNullOrEmpty(message.Reason))
                {
                    error = "a placed result has no reason";
                    return false;
                }

                error = null;
                return true;
            }

            if (hasPosition || hasMoved)
            {
                error = "an unplaced result carries no measurement";
                return false;
            }

            if (string.IsNullOrEmpty(message.Reason))
            {
                error = "an unplaced result needs a reason";
                return false;
            }

            error = null;
            return true;
        }

        private static bool IsFiniteTriple(List<double> values)
        {
            if (values == null || values.Count != 3)
            {
                return false;
            }

            for (int i = 0; i < values.Count; i++)
            {
                if (double.IsNaN(values[i]) || double.IsInfinity(values[i]))
                {
                    return false;
                }
            }

            return true;
        }
    }
}
