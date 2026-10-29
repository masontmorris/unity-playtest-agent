// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Transport
{
    /// <summary>A controlled, safe-to-report reason why an inspection cannot run.</summary>
    public sealed class BridgeInspectionUnavailableException : Exception
    {
        public BridgeInspectionUnavailableException(string message) : base(message) { }
    }

    /// <summary>
    /// One Editor inspection snapshot. <see cref="Data"/> is open because each stage carries
    /// reflection descriptors the contract does not fix. <see cref="Caps"/> records how complete
    /// every collection is, so a capped sample is never mistaken for the whole project.
    /// </summary>
    public sealed class BridgeInspectionSnapshot
    {
        public Dictionary<string, object> Data { get; set; } = new Dictionary<string, object>();

        public Dictionary<string, InspectionCap> Caps { get; set; } = new Dictionary<string, InspectionCap>();

        public List<string> Errors { get; set; } = new List<string>();
    }

    /// <summary>
    /// Source of Editor inspection facts. Called from a transport thread, so an implementation
    /// must marshal to the Unity main thread itself and must not write to the project. Transport
    /// never calls Unity APIs; the Editor host supplies and owns this source.
    /// </summary>
    public interface IBridgeInspectionSource
    {
        BridgeInspectionSnapshot Inspect(string stage);
    }
}
