// SPDX-License-Identifier: Apache-2.0
using System;
using System.Globalization;
using Newtonsoft.Json;

namespace Playtest.Bridge.Protocol
{
    /// <summary>
    /// One serializer configuration for every wire message.
    /// <see cref="MissingMemberHandling.Error"/> makes unknown fields an error, matching the
    /// Python contracts' <c>extra="forbid"</c>.
    /// </summary>
    public static class BridgeJson
    {
        public static readonly JsonSerializerSettings Settings = new JsonSerializerSettings
        {
            MissingMemberHandling = MissingMemberHandling.Error,
            NullValueHandling = NullValueHandling.Ignore,
            DateParseHandling = DateParseHandling.DateTime,
            DateTimeZoneHandling = DateTimeZoneHandling.Utc,
            FloatParseHandling = FloatParseHandling.Double,
            Formatting = Formatting.None,
            Culture = CultureInfo.InvariantCulture,
        };

        public static string Serialize(object value)
        {
            return JsonConvert.SerializeObject(value, Settings);
        }

        public static T Deserialize<T>(string json)
        {
            return JsonConvert.DeserializeObject<T>(json, Settings);
        }
    }
}
