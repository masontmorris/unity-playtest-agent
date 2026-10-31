// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Playtest.Bridge.Transport;

namespace Playtest.Bridge.Editor.Session
{
    /// <summary>
    /// A control record is malformed, carries a field this version does not know, or breaks one of the
    /// cross-field rules its phase requires. Nothing is repaired and no default is guessed: the caller
    /// fails closed and keeps the message.
    /// </summary>
    public sealed class SessionRecordException : Exception
    {
        public SessionRecordException(string message) : base(message)
        {
        }

        public SessionRecordException(string message, Exception inner) : base(message, inner)
        {
        }
    }

    /// <summary>
    /// Strict JSON for the host control mailbox. The Python contracts forbid extra fields, so an
    /// unknown key is an error here too; types are exact (no <c>"1"</c> for an integer and no
    /// ISO string for a number), duplicate keys are errors, and every timestamp must carry an
    /// explicit offset so a local time can never be mistaken for UTC.
    /// </summary>
    public static class SessionRecordJson
    {
        /// <summary>UTC ISO-8601 with an explicit <c>Z</c>; the same instant Python's datetime writes.</summary>
        public const string TimestampFormat = "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'";

        private static readonly Regex ExplicitOffset = new Regex(
            @"(?:Z|[+-]\d{2}:\d{2}|[+-]\d{4})$", RegexOptions.CultureInvariant);

        private static readonly Regex Sha256 = new Regex("^[0-9a-f]{64}$", RegexOptions.CultureInvariant);

        public static string Timestamp(DateTime utc)
        {
            DateTime value = utc.Kind == DateTimeKind.Utc ? utc : utc.ToUniversalTime();
            return value.ToString(TimestampFormat, CultureInfo.InvariantCulture);
        }

        /// <summary>Parses exactly one JSON object and nothing else.</summary>
        public static JObject ParseObject(string json, string what)
        {
            if (string.IsNullOrWhiteSpace(json))
            {
                throw new SessionRecordException(what + " is empty");
            }

            try
            {
                using (var reader = new JsonTextReader(new StringReader(json)))
                {
                    reader.DateParseHandling = DateParseHandling.None;
                    reader.FloatParseHandling = FloatParseHandling.Double;
                    var settings = new JsonLoadSettings
                    {
                        DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error,
                        CommentHandling = CommentHandling.Ignore,
                        LineInfoHandling = LineInfoHandling.Ignore,
                    };
                    JObject obj = JObject.Load(reader, settings);
                    while (reader.Read())
                    {
                        if (reader.TokenType != JsonToken.Comment)
                        {
                            throw new SessionRecordException(what + " has content after its JSON object");
                        }
                    }

                    return obj;
                }
            }
            catch (SessionRecordException)
            {
                throw;
            }
            catch (JsonException ex)
            {
                throw new SessionRecordException(what + " is not valid JSON: " + ex.Message, ex);
            }
        }

        /// <summary>The object's keys must be exactly <paramref name="keys"/>: no missing, no extra.</summary>
        public static void RequireExactKeys(JObject obj, string what, params string[] keys)
        {
            var expected = new HashSet<string>(keys, StringComparer.Ordinal);
            var actual = new HashSet<string>(obj.Properties().Select(p => p.Name), StringComparer.Ordinal);
            var missing = expected.Where(k => !actual.Contains(k)).OrderBy(k => k, StringComparer.Ordinal).ToList();
            var extra = actual.Where(k => !expected.Contains(k)).OrderBy(k => k, StringComparer.Ordinal).ToList();
            if (missing.Count != 0)
            {
                throw new SessionRecordException(what + " is missing " + string.Join(", ", missing));
            }

            if (extra.Count != 0)
            {
                throw new SessionRecordException(what + " carries unknown " + string.Join(", ", extra));
            }
        }

        public static int RequireVersion(JObject obj, string what)
        {
            JToken token = obj["version"];
            if (token == null || token.Type != JTokenType.Integer)
            {
                throw new SessionRecordException(what + ".version must be the integer 1");
            }

            int version = (int)token;
            if (version != 1)
            {
                throw new SessionRecordException(what + ".version is " + version + ", not 1");
            }

            return version;
        }

        public static string RequireString(JObject obj, string key, string what)
        {
            JToken token = obj[key];
            if (token == null || token.Type != JTokenType.String)
            {
                throw new SessionRecordException(what + "." + key + " must be a string");
            }

            string value = (string)token;
            if (string.IsNullOrWhiteSpace(value))
            {
                throw new SessionRecordException(what + "." + key + " must not be empty");
            }

            return value;
        }

        public static string OptionalString(JObject obj, string key, string what)
        {
            JToken token = obj[key];
            if (token == null || token.Type == JTokenType.Null)
            {
                return null;
            }

            if (token.Type != JTokenType.String)
            {
                throw new SessionRecordException(what + "." + key + " must be a string or null");
            }

            string value = (string)token;
            if (string.IsNullOrWhiteSpace(value))
            {
                throw new SessionRecordException(what + "." + key + " must not be an empty string; use null");
            }

            return value;
        }

        public static int RequireInteger(JObject obj, string key, string what)
        {
            JToken token = obj[key];
            if (token == null || token.Type != JTokenType.Integer)
            {
                throw new SessionRecordException(what + "." + key + " must be an integer");
            }

            return (int)token;
        }

        public static int? OptionalInteger(JObject obj, string key, string what)
        {
            JToken token = obj[key];
            if (token == null || token.Type == JTokenType.Null)
            {
                return null;
            }

            if (token.Type != JTokenType.Integer)
            {
                throw new SessionRecordException(what + "." + key + " must be an integer or null");
            }

            return (int)token;
        }

        public static long? OptionalLong(JObject obj, string key, string what)
        {
            JToken token = obj[key];
            if (token == null || token.Type == JTokenType.Null)
            {
                return null;
            }

            if (token.Type != JTokenType.Integer)
            {
                throw new SessionRecordException(what + "." + key + " must be an integer or null");
            }

            try
            {
                return (long)token;
            }
            catch (OverflowException)
            {
                throw new SessionRecordException(what + "." + key + " must fit a signed 64-bit integer");
            }
        }

        public static string RequireUuid(JObject obj, string key, string what)
        {
            string value = RequireString(obj, key, what);
            if (!BridgeGeneration.IsCanonical(value))
            {
                throw new SessionRecordException(
                    what + "." + key + " must be a canonical lower-case hyphenated UUID, not '" + value + "'");
            }

            return value;
        }

        public static string OptionalUuid(JObject obj, string key, string what)
        {
            string value = OptionalString(obj, key, what);
            if (value != null && !BridgeGeneration.IsCanonical(value))
            {
                throw new SessionRecordException(
                    what + "." + key + " must be a canonical lower-case hyphenated UUID or null, not '" + value + "'");
            }

            return value;
        }

        public static string RequireAbsolutePath(JObject obj, string key, string what)
        {
            string value = RequireString(obj, key, what);
            if (!Path.IsPathRooted(value))
            {
                throw new SessionRecordException(what + "." + key + " must be an absolute path");
            }

            return value;
        }

        public static string RequireSha256(JObject obj, string key, string what)
        {
            string value = RequireString(obj, key, what);
            if (!Sha256.IsMatch(value))
            {
                throw new SessionRecordException(what + "." + key + " must be 64 lower-case hex digits");
            }

            return value;
        }

        public static DateTime RequireTimestampUtc(JObject obj, string key, string what)
        {
            DateTime? value = OptionalTimestampUtc(obj, key, what);
            if (value == null)
            {
                throw new SessionRecordException(what + "." + key + " must be a timezone-aware ISO-8601 string");
            }

            return value.Value;
        }

        public static DateTime? OptionalTimestampUtc(JObject obj, string key, string what)
        {
            JToken token = obj[key];
            if (token == null || token.Type == JTokenType.Null)
            {
                return null;
            }

            if (token.Type != JTokenType.String)
            {
                throw new SessionRecordException(
                    what + "." + key + " must be a timezone-aware ISO-8601 string or null");
            }

            string text = (string)token;
            if (!ExplicitOffset.IsMatch(text))
            {
                throw new SessionRecordException(
                    what + "." + key + " must carry an explicit timezone offset or 'Z', not the local time '"
                    + text + "'");
            }

            DateTimeOffset parsed;
            if (!DateTimeOffset.TryParse(
                    text,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.None,
                    out parsed))
            {
                throw new SessionRecordException(what + "." + key + " is not an ISO-8601 instant: '" + text + "'");
            }

            return parsed.UtcDateTime;
        }

        public static IReadOnlyList<string> RequireStringArray(JObject obj, string key, string what)
        {
            JToken token = obj[key];
            if (token == null || token.Type != JTokenType.Array)
            {
                throw new SessionRecordException(what + "." + key + " must be an array of strings");
            }

            var values = new List<string>();
            foreach (JToken item in (JArray)token)
            {
                if (item.Type != JTokenType.String || string.IsNullOrWhiteSpace((string)item))
                {
                    throw new SessionRecordException(what + "." + key + " must contain only non-empty strings");
                }

                values.Add((string)item);
            }

            return values;
        }

        /// <summary>Serializes with every key present, writing null for an unmeasured value.</summary>
        public static string Serialize(JObject obj)
        {
            return obj.ToString(Formatting.None);
        }

        /// <summary>An explicit JSON null for an absent value, so the key is never dropped.</summary>
        public static JToken Nullable(string value)
        {
            return value == null ? JValue.CreateNull() : new JValue(value);
        }

        public static JToken Nullable(int? value)
        {
            return value.HasValue ? new JValue(value.Value) : JValue.CreateNull();
        }

        public static JToken Nullable(long? value)
        {
            return value.HasValue ? new JValue(value.Value) : JValue.CreateNull();
        }

        public static JToken Nullable(DateTime? value)
        {
            return value.HasValue ? new JValue(Timestamp(value.Value)) : JValue.CreateNull();
        }
    }
}
