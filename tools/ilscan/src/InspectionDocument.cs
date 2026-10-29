using System;
using System.Collections.Generic;
using System.Text.Json.Nodes;

namespace Playtest.IlScan
{
    /// <summary>One collection's bounds and observations, kept separate from its data.</summary>
    public sealed class InspectionCap
    {
        public int Limit;
        public int? Observed;
        public int Returned;
        public bool? Truncated;
    }

    /// <summary>
    /// The facts, caps and errors this tool adds to a discovery snapshot, in the shape the bridge's
    /// inspection documents use. Preflight merges it into the snapshot.
    /// </summary>
    public sealed class InspectionDocument
    {
        public readonly JsonObject Data = new JsonObject();
        public readonly OrderedDictionary<string, InspectionCap> Caps = new OrderedDictionary<string, InspectionCap>();
        public readonly List<string> Errors = new List<string>();

        public List<T> Take<T>(string name, IList<T> values, int limit)
        {
            if (limit <= 0) throw new ArgumentOutOfRangeException(nameof(limit));
            int count = Math.Min(values.Count, limit);
            var result = new List<T>(count);
            for (int i = 0; i < count; i++) result.Add(values[i]);
            Caps[name] = new InspectionCap
            {
                Limit = limit,
                Observed = values.Count,
                Returned = count,
                Truncated = values.Count > count,
            };
            return result;
        }

        public JsonObject ToJson()
        {
            var caps = new JsonObject();
            foreach (KeyValuePair<string, InspectionCap> pair in Caps)
            {
                caps[pair.Key] = new JsonObject
                {
                    ["limit"] = pair.Value.Limit,
                    ["observed"] = pair.Value.Observed,
                    ["returned"] = pair.Value.Returned,
                    ["truncated"] = pair.Value.Truncated,
                };
            }

            var errors = new JsonArray();
            foreach (string error in Errors) errors.Add(error);
            return new JsonObject { ["data"] = Data.DeepClone(), ["caps"] = caps, ["errors"] = errors };
        }
    }
}
