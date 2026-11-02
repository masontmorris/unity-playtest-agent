// SPDX-License-Identifier: Apache-2.0
using System.Collections.Generic;
using Newtonsoft.Json;

namespace Playtest.Bridge.Protocol
{
    /// <summary>
    /// Core -> bridge. Put the configured controlled entity at one position and let it settle.
    /// A tester's affordance, not game input: the bridge releases every held input first and acts
    /// only on the entity its own configuration names.
    /// </summary>
    public sealed class PlaceRequestMessage : Envelope
    {
        public PlaceRequestMessage() : base("place")
        {
        }

        /// <summary>Requested world position [x, y, z]; exactly three finite numbers.</summary>
        [JsonProperty("position", Order = 10)]
        public List<double> Position { get; set; } = new List<double>();

        /// <summary>Game seconds to wait after setting the pose.</summary>
        [JsonProperty("settle_s", Order = 11)]
        public double SettleS { get; set; }
    }

    /// <summary>
    /// Bridge -> core. What the placement measured. A placement that could not run is
    /// <see cref="Placed"/> false with a reason and no invented measurement; a placement the body
    /// kept moving after is placed true with its measured <see cref="MovedDuringSettle"/>.
    /// </summary>
    public sealed class PlaceResultMessage : Envelope
    {
        public PlaceResultMessage() : base("placed")
        {
        }

        [JsonProperty("placed", Order = 10)]
        public bool Placed { get; set; }

        /// <summary>Measured position after the settle wait; null when the place did not run.</summary>
        [JsonProperty("position_after_settle", Order = 11)]
        public List<double> PositionAfterSettle { get; set; }

        /// <summary>Distance from the requested position after settle; null when it was not measured.</summary>
        [JsonProperty("moved_during_settle", Order = 12)]
        public double? MovedDuringSettle { get; set; }

        /// <summary>Why the place did not run; null when it was placed.</summary>
        [JsonProperty("reason", Order = 13)]
        public string Reason { get; set; }
    }
}
