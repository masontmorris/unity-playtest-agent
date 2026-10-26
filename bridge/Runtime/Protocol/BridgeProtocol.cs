using System;
using System.Collections.Generic;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Playtest.Bridge.Protocol
{
    /// <summary>Wire error codes from <c>schemas/bridge_error.schema.json</c>.</summary>
    public static class BridgeErrorCode
    {
        public const string Auth = "auth";
        public const string Busy = "busy";
        public const string Protocol = "protocol";
        public const string Invalid = "invalid";
        public const string Internal = "internal";

        public static readonly IReadOnlyList<string> All = new[]
        {
            Auth, Busy, Protocol, Invalid, Internal,
        };
    }

    public sealed class ProtocolError
    {
        public ProtocolError(string code, string message)
        {
            Code = code;
            Message = message;
        }

        public string Code { get; }

        public string Message { get; }
    }

    /// <summary>
    /// Reads and writes the same wire contracts the Python core uses. Parsing is strict:
    /// unknown members, wrong JSON types and values outside the schema are rejected rather
    /// than repaired. Missing evidence stays missing.
    /// </summary>
    public static class BridgeProtocol
    {
        public static readonly IReadOnlyList<string> InputBackends = new[]
        {
            "input_system", "legacy",
        };

        public static readonly IReadOnlyList<string> CaptureSelfTestValues = new[]
        {
            "passed", "failed", "unknown",
        };

        public static string Serialize(Envelope message)
        {
            if (message == null)
            {
                throw new ArgumentNullException(nameof(message));
            }

            return BridgeJson.Serialize(message);
        }

        public static bool TryReadHello(string json, out HelloMessage hello, out ProtocolError error)
        {
            return TryRead(json, "hello", out hello, out error);
        }

        public static bool TryReadStatus(string json, out StatusMessage status, out ProtocolError error)
        {
            return TryRead(json, "status", out status, out error);
        }

        public static bool TryReadBridgeError(string json, out BridgeErrorMessage message, out ProtocolError error)
        {
            return TryRead(json, "error", out message, out error);
        }

        public static bool TryRead<T>(string json, string expectedType, out T message, out ProtocolError error)
            where T : Envelope
        {
            message = null;

            JObject obj;
            try
            {
                obj = ParseObject(json);
            }
            catch (JsonException ex)
            {
                error = Invalid("malformed JSON: " + ex.Message);
                return false;
            }

            JToken typeToken = obj["type"];
            if (typeToken != null)
            {
                if (typeToken.Type != JTokenType.String || !string.Equals((string)typeToken, expectedType, StringComparison.Ordinal))
                {
                    error = Invalid($"expected type '{expectedType}', got '{typeToken}'");
                    return false;
                }
            }

            T parsed;
            try
            {
                parsed = obj.ToObject<T>(JsonSerializer.Create(BridgeJson.Settings));
            }
            catch (JsonException ex)
            {
                error = Invalid(ex.Message);
                return false;
            }
            catch (ArgumentException ex)
            {
                error = Invalid(ex.Message);
                return false;
            }

            if (parsed == null)
            {
                error = Invalid("message is null");
                return false;
            }

            if (!ValidateEnvelope(parsed, obj, out error))
            {
                return false;
            }

            if (!ValidateBody(parsed, obj, out error))
            {
                return false;
            }

            message = parsed;
            return true;
        }

        public static BridgeErrorMessage Error(
            string session,
            long seq,
            string code,
            string message,
            long? inReplyTo = null)
        {
            return new BridgeErrorMessage
            {
                Session = string.IsNullOrEmpty(session) ? "unknown" : session,
                Seq = seq,
                GameS = null,
                WallUtc = DateTime.UtcNow,
                Code = code,
                Message = message,
                InReplyTo = inReplyTo,
            };
        }

        private static bool ValidateEnvelope(Envelope message, JObject obj, out ProtocolError error)
        {
            if (!HasString(obj, "session") || string.IsNullOrEmpty(message.Session))
            {
                error = Invalid("session is required and must be a non-empty string");
                return false;
            }

            if (!HasInteger(obj, "seq") || message.Seq < 0)
            {
                error = Invalid("seq is required and must be a non-negative integer");
                return false;
            }

            if (!HasString(obj, "wall_utc") || message.WallUtc == default(DateTime))
            {
                error = Invalid("wall_utc is required and must be an ISO-8601 timestamp");
                return false;
            }

            if (!ProtocolVersion.IsWellFormed(message.Protocol))
            {
                error = Invalid("protocol must match upa/<integer>");
                return false;
            }

            if (message.GameS.HasValue)
            {
                JToken gameToken = obj["game_s"];
                if (gameToken == null || (gameToken.Type != JTokenType.Float && gameToken.Type != JTokenType.Integer))
                {
                    error = Invalid("game_s must be a number or null");
                    return false;
                }

                double gameS = message.GameS.Value;
                if (double.IsNaN(gameS) || double.IsInfinity(gameS) || gameS < 0)
                {
                    error = Invalid("game_s must be null or a non-negative number");
                    return false;
                }
            }

            error = null;
            return true;
        }

        private static bool ValidateBody(Envelope message, JObject obj, out ProtocolError error)
        {
            if (message is HelloMessage hello)
            {
                if (!HasString(obj, "token") || string.IsNullOrEmpty(hello.Token))
                {
                    error = Invalid("token is required and must be a non-empty string");
                    return false;
                }

                if (!HasString(obj, "core_version"))
                {
                    error = Invalid("core_version is required and must be a string");
                    return false;
                }
            }
            else if (message is StatusMessage status)
            {
                if (!HasString(obj, "bridge_version") || status.BridgeVersion == null)
                {
                    error = Invalid("bridge_version is required");
                    return false;
                }

                if (!HasString(obj, "unity_version") || status.UnityVersion == null)
                {
                    error = Invalid("unity_version is required");
                    return false;
                }

                if (!HasString(obj, "project") || status.Project == null)
                {
                    error = Invalid("project is required");
                    return false;
                }

                if (obj["input_backends"]?.Type != JTokenType.Array || status.InputBackends == null)
                {
                    error = Invalid("input_backends is required and must be an array");
                    return false;
                }

                foreach (string backend in status.InputBackends)
                {
                    if (!Contains(InputBackends, backend))
                    {
                        error = Invalid($"unknown input backend '{backend}'");
                        return false;
                    }
                }

                if (obj["clock_control"]?.Type != JTokenType.Boolean)
                {
                    error = Invalid("clock_control is required and must be a boolean");
                    return false;
                }

                if (obj["capture"]?.Type != JTokenType.Boolean)
                {
                    error = Invalid("capture is required and must be a boolean");
                    return false;
                }

                if (!HasString(obj, "capture_self_test") || !Contains(CaptureSelfTestValues, status.CaptureSelfTest))
                {
                    error = Invalid("capture_self_test must be passed, failed or unknown");
                    return false;
                }

                JToken lockToken = obj["lock"];
                if (lockToken == null || lockToken.Type != JTokenType.Object || status.Lock == null)
                {
                    error = Invalid("lock is required and must be an object");
                    return false;
                }

                if (lockToken["held"]?.Type != JTokenType.Boolean)
                {
                    error = Invalid("lock.held is required and must be a boolean");
                    return false;
                }
            }
            else if (message is BridgeErrorMessage bridgeError)
            {
                if (!HasString(obj, "code") || !Contains(BridgeErrorCode.All, bridgeError.Code))
                {
                    error = Invalid("code must be one of auth, busy, protocol, invalid, internal");
                    return false;
                }

                if (!HasString(obj, "message"))
                {
                    error = Invalid("message is required and must be a string");
                    return false;
                }
            }

            error = null;
            return true;
        }

        private static JObject ParseObject(string json)
        {
            using (var text = new System.IO.StringReader(json))
            using (var reader = new JsonTextReader(text) { DateParseHandling = DateParseHandling.None })
            {
                JToken token = JToken.ReadFrom(reader);
                if (token.Type != JTokenType.Object)
                {
                    throw new JsonReaderException("message must be a JSON object");
                }

                return (JObject)token;
            }
        }

        private static bool HasString(JObject obj, string name)
        {
            return obj[name]?.Type == JTokenType.String;
        }

        private static bool HasInteger(JObject obj, string name)
        {
            return obj[name]?.Type == JTokenType.Integer;
        }

        private static bool Contains(IReadOnlyList<string> values, string value)
        {
            if (value == null)
            {
                return false;
            }

            for (int i = 0; i < values.Count; i++)
            {
                if (string.Equals(values[i], value, StringComparison.Ordinal))
                {
                    return true;
                }
            }

            return false;
        }

        private static ProtocolError Invalid(string message)
        {
            return new ProtocolError(BridgeErrorCode.Invalid, message);
        }
    }
}
