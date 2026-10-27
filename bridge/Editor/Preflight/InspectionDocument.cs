using System;
using System.Collections.Generic;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>One collection's bounds and observations, kept separate from its data.</summary>
    public sealed class InspectionCap
    {
        public int Limit;
        public int? Observed;
        public int Returned;
        public bool? Truncated;
    }

    public sealed class InspectionDocument
    {
        public readonly Dictionary<string, object> Data = new Dictionary<string, object>();
        public readonly Dictionary<string, InspectionCap> Caps = new Dictionary<string, InspectionCap>();
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

        public void Unavailable(string name, int limit, string reason)
        {
            Caps[name] = new InspectionCap { Limit = limit, Observed = null, Returned = 0, Truncated = null };
            Errors.Add(name + ": " + reason);
        }
    }
}
