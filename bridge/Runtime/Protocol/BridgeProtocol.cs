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

        public static readonly IReadOnlyList<string> ObserveModes = new[]
        {
            "subscribe", "once", "unsubscribe",
        };

        public static readonly IReadOnlyList<string> ClockModes = new[]
        {
            "realtime", "stepped",
        };

        public static readonly IReadOnlyList<string> InspectionStages = new[]
        {
            "inventory", "discovery", "transforms", "fields",
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

        public static bool TryReadObserveRequest(string json, out ObserveRequestMessage request, out ProtocolError error)
        {
            return TryRead(json, "observe", out request, out error);
        }

        public static bool TryReadObservation(string json, out ObservationMessage observation, out ProtocolError error)
        {
            return TryRead(json, "observation", out observation, out error);
        }

        public static bool TryReadActRequest(string json, out ActRequestMessage request, out ProtocolError error)
        {
            return TryRead(json, "act", out request, out error);
        }

        public static bool TryReadExecutionRecord(string json, out ExecutionRecordMessage record, out ProtocolError error)
        {
            return TryRead(json, "execution", out record, out error);
        }

        public static bool TryReadStopRequest(string json, out StopRequestMessage request, out ProtocolError error)
        {
            return TryRead(json, "stop", out request, out error);
        }

        public static bool TryReadStopResult(string json, out StopResultMessage result, out ProtocolError error)
        {
            return TryRead(json, "stopped", out result, out error);
        }

        public static bool TryReadClockRequest(string json, out ClockRequestMessage request, out ProtocolError error)
        {
            return TryRead(json, "clock", out request, out error);
        }

        public static bool TryReadInspectRequest(string json, out InspectRequestMessage request, out ProtocolError error)
        {
            return TryRead(json, "inspect", out request, out error);
        }

        public static bool TryReadInspectionResult(string json, out InspectionResultMessage result, out ProtocolError error)
        {
            return TryRead(json, "inspection", out result, out error);
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
            else if (message is ObserveRequestMessage observe)
            {
                if (!HasString(obj, "mode") || !Contains(ObserveModes, observe.Mode))
                {
                    error = Invalid("mode must be subscribe, once or unsubscribe");
                    return false;
                }

                JToken withFrame = obj["with_frame"];
                if (withFrame != null && withFrame.Type != JTokenType.Boolean)
                {
                    error = Invalid("with_frame must be a boolean");
                    return false;
                }

                JToken rate = obj["rate_hz"];
                if (rate != null && rate.Type != JTokenType.Null)
                {
                    if (rate.Type != JTokenType.Float && rate.Type != JTokenType.Integer)
                    {
                        error = Invalid("rate_hz must be a number or null");
                        return false;
                    }

                    double rateHz = (double)rate;
                    if (rateHz <= 0 || rateHz > 60)
                    {
                        error = Invalid("rate_hz must be greater than 0 and at most 60");
                        return false;
                    }
                }
            }
            else if (message is ObservationMessage observation)
            {
                if (!HasString(obj, "clock_mode") || !Contains(ClockModes, observation.ClockMode))
                {
                    error = Invalid("clock_mode must be realtime or stepped");
                    return false;
                }
                if (!HasString(obj, "scene") || observation.Scene == null)
                {
                    error = Invalid("scene is required and must be a string");
                    return false;
                }

                if (obj["playing"]?.Type != JTokenType.Boolean)
                {
                    error = Invalid("playing is required and must be a boolean");
                    return false;
                }

                if (obj["paused"]?.Type != JTokenType.Boolean)
                {
                    error = Invalid("paused is required and must be a boolean");
                    return false;
                }

                JToken timeScale = obj["time_scale"];
                if (timeScale == null || (timeScale.Type != JTokenType.Float && timeScale.Type != JTokenType.Integer) || (double)timeScale < 0)
                {
                    error = Invalid("time_scale is required and must be a non-negative number");
                    return false;
                }

                JToken frame = obj["frame"];
                if (frame == null || frame.Type != JTokenType.Integer || (long)frame < 0)
                {
                    error = Invalid("frame is required and must be a non-negative integer");
                    return false;
                }

                JToken publicToken = obj["public"];
                if (publicToken != null && publicToken.Type != JTokenType.Null && publicToken.Type != JTokenType.Object)
                {
                    error = Invalid("public must be an object or null");
                    return false;
                }

                JToken ui = obj["ui"];
                if (ui != null && ui.Type != JTokenType.Null && ui.Type != JTokenType.Object)
                {
                    error = Invalid("ui must be an object or null");
                    return false;
                }

                JToken frameRef = obj["frame_ref"];
                if (frameRef != null && frameRef.Type != JTokenType.Null && frameRef.Type != JTokenType.String)
                {
                    error = Invalid("frame_ref must be a string or null");
                    return false;
                }

                JToken readerErrors = obj["reader_errors"];
                if (readerErrors != null && readerErrors.Type != JTokenType.Null)
                {
                    if (readerErrors.Type != JTokenType.Array)
                    {
                        error = Invalid("reader_errors must be an array");
                        return false;
                    }

                    foreach (JToken item in readerErrors.Children())
                    {
                        if (item.Type != JTokenType.Object || item["reader"]?.Type != JTokenType.String || item["error"]?.Type != JTokenType.String)
                        {
                            error = Invalid("each reader error needs a reader and error string");
                            return false;
                        }
                    }
                }
            }
            else if (message is ActRequestMessage act)
            {
                if (!HasString(obj, "action_id") || string.IsNullOrEmpty(act.ActionId))
                {
                    error = Invalid("action_id is required and must be a non-empty string");
                    return false;
                }

                if (!HasString(obj, "choice_id") || act.ChoiceId == null)
                {
                    error = Invalid("choice_id is required and must be a string");
                    return false;
                }

                if (!HasString(obj, "action") || string.IsNullOrEmpty(act.Action))
                {
                    error = Invalid("action is required and must be a non-empty string");
                    return false;
                }

                JToken parameters = obj["params"];
                if (parameters != null && parameters.Type != JTokenType.Null && parameters.Type != JTokenType.Object)
                {
                    error = Invalid("params must be an object");
                    return false;
                }

                if (!HasInteger(obj, "based_on_seq") || act.BasedOnSeq < 0)
                {
                    error = Invalid("based_on_seq is required and must be a non-negative integer");
                    return false;
                }

                if (!HasNumber(obj, "max_age_s") || !IsPositiveFinite(act.MaxAgeS))
                {
                    error = Invalid("max_age_s is required and must be a positive number");
                    return false;
                }

                JToken dependsOn = obj["depends_on"];
                if (dependsOn != null && dependsOn.Type != JTokenType.Null)
                {
                    if (dependsOn.Type != JTokenType.Array)
                    {
                        error = Invalid("depends_on must be an array of strings");
                        return false;
                    }

                    foreach (JToken item in dependsOn.Children())
                    {
                        if (item.Type != JTokenType.String)
                        {
                            error = Invalid("depends_on must contain only strings");
                            return false;
                        }
                    }
                }

                JToken lease = obj["lease_s"];
                if (lease != null && lease.Type != JTokenType.Null)
                {
                    if ((lease.Type != JTokenType.Float && lease.Type != JTokenType.Integer) || !IsPositiveFinite((double)lease))
                    {
                        error = Invalid("lease_s must be null or a positive number");
                        return false;
                    }
                }
            }
            else if (message is ExecutionRecordMessage execution)
            {
                if (!HasString(obj, "action_id") || string.IsNullOrEmpty(execution.ActionId))
                {
                    error = Invalid("action_id is required and must be a non-empty string");
                    return false;
                }

                if (!HasString(obj, "status") || !Contains(BridgeExecStatus.All, execution.Status))
                {
                    error = Invalid("status must be a valid execution status");
                    return false;
                }

                JToken validated = obj["validated_against_seq"];
                if (validated != null && validated.Type != JTokenType.Null && (validated.Type != JTokenType.Integer || (long)validated < 0))
                {
                    error = Invalid("validated_against_seq must be null or a non-negative integer");
                    return false;
                }

                JToken reject = obj["reject_reason"];
                if (reject != null && reject.Type != JTokenType.Null && (reject.Type != JTokenType.String || !Contains(BridgeRejectReason.All, (string)reject)))
                {
                    error = Invalid("reject_reason must be null or a valid reject reason");
                    return false;
                }

                string rejectReason = reject == null || reject.Type == JTokenType.Null ? null : (string)reject;
                bool rejected = string.Equals(execution.Status, BridgeExecStatus.Rejected, StringComparison.Ordinal);
                if (rejected != (rejectReason != null))
                {
                    error = Invalid("reject_reason is required exactly when status is 'rejected'");
                    return false;
                }

                if (!IsOptionalNumber(obj, "input_start_game_s")
                    || !IsOptionalNumber(obj, "input_end_game_s")
                    || !IsOptionalNumber(obj, "input_start_wall_s")
                    || !IsOptionalNumber(obj, "input_end_wall_s"))
                {
                    error = Invalid("execution times must be null or numbers");
                    return false;
                }

                JToken duration = obj["applied_duration_s"];
                if (duration != null && duration.Type != JTokenType.Null && (duration.Type != JTokenType.Float && duration.Type != JTokenType.Integer || (double)duration < 0))
                {
                    error = Invalid("applied_duration_s must be null or a non-negative number");
                    return false;
                }

                JToken releaseVerified = obj["release_verified"];
                if (releaseVerified != null && releaseVerified.Type != JTokenType.Null && releaseVerified.Type != JTokenType.Boolean)
                {
                    error = Invalid("release_verified must be null or a boolean");
                    return false;
                }

                if (!ValidateEffects(obj, "expected_effect", out error) || !ValidateEffects(obj, "observed_effect", out error))
                {
                    return false;
                }
            }
            else if (message is StopRequestMessage stop)
            {
                if (!HasString(obj, "reason") || !Contains(BridgeStopReason.All, stop.Reason))
                {
                    error = Invalid("reason must be user, reset, scene_change, disconnect, error or finish");
                    return false;
                }
            }
            else if (message is StopResultMessage stopped)
            {
                JToken released = obj["released"];
                if (released == null || released.Type != JTokenType.Array)
                {
                    error = Invalid("released is required and must be an array of strings");
                    return false;
                }

                foreach (JToken item in released.Children())
                {
                    if (item.Type != JTokenType.String)
                    {
                        error = Invalid("released must contain only strings");
                        return false;
                    }
                }

                if (obj["release_verified"]?.Type != JTokenType.Boolean)
                {
                    error = Invalid("release_verified is required and must be a boolean");
                    return false;
                }
            }
            else if (message is ClockRequestMessage clock)
            {
                if (!HasString(obj, "op") || !Contains(BridgeClockOps.All, clock.Op))
                {
                    error = Invalid("op must be pause, resume, step or set_scale");
                    return false;
                }

                JToken step = obj["step_game_s"];
                if (step != null && step.Type != JTokenType.Null
                    && ((step.Type != JTokenType.Float && step.Type != JTokenType.Integer) || !IsPositiveFinite((double)step)))
                {
                    error = Invalid("step_game_s must be null or a positive number");
                    return false;
                }

                JToken scale = obj["time_scale"];
                if (scale != null && scale.Type != JTokenType.Null
                    && ((scale.Type != JTokenType.Float && scale.Type != JTokenType.Integer) || (double)scale < 0 || double.IsNaN((double)scale) || double.IsInfinity((double)scale)))
                {
                    error = Invalid("time_scale must be null or a non-negative number");
                    return false;
                }

                bool isStep = string.Equals(clock.Op, BridgeClockOps.Step, System.StringComparison.Ordinal);
                bool hasStep = step != null && step.Type != JTokenType.Null;
                if (isStep != hasStep)
                {
                    error = Invalid("step_game_s is required exactly when op is 'step'");
                    return false;
                }

                bool isSetScale = string.Equals(clock.Op, BridgeClockOps.SetScale, System.StringComparison.Ordinal);
                bool hasScale = scale != null && scale.Type != JTokenType.Null;
                if (isSetScale != hasScale)
                {
                    error = Invalid("time_scale is required exactly when op is 'set_scale'");
                    return false;
                }
            }
            else if (message is InspectRequestMessage inspect)
            {
                if (!HasString(obj, "stage") || !Contains(InspectionStages, inspect.Stage))
                {
                    error = Invalid("stage must be a known inspection stage");
                    return false;
                }
            }
            else if (message is InspectionResultMessage inspection)
            {
                if (!HasString(obj, "stage") || !Contains(InspectionStages, inspection.Stage))
                {
                    error = Invalid("stage must be a known inspection stage");
                    return false;
                }

                if (!HasInteger(obj, "in_reply_to") || inspection.InReplyTo < 0)
                {
                    error = Invalid("in_reply_to is required and must be a non-negative integer");
                    return false;
                }

                JToken data = obj["data"];
                if (data == null || data.Type != JTokenType.Object)
                {
                    error = Invalid("data is required and must be an object");
                    return false;
                }

                JToken caps = obj["caps"];
                if (caps == null || caps.Type != JTokenType.Object)
                {
                    error = Invalid("caps is required and must be an object");
                    return false;
                }

                foreach (JProperty property in ((JObject)caps).Properties())
                {
                    JToken cap = property.Value;
                    if (cap == null || cap.Type != JTokenType.Object)
                    {
                        error = Invalid("caps." + property.Name + " must be an object");
                        return false;
                    }

                    JToken limit = cap["limit"];
                    if (limit == null || limit.Type != JTokenType.Integer || (long)limit <= 0)
                    {
                        error = Invalid("caps." + property.Name + ".limit must be a positive integer");
                        return false;
                    }

                    JToken observed = cap["observed"];
                    if (observed == null)
                    {
                        error = Invalid("caps." + property.Name + ".observed is required; use null when unknown");
                        return false;
                    }

                    long? observedValue = null;
                    if (observed.Type != JTokenType.Null)
                    {
                        if (observed.Type != JTokenType.Integer || (long)observed < 0)
                        {
                            error = Invalid("caps." + property.Name + ".observed must be null or a non-negative integer");
                            return false;
                        }

                        observedValue = (long)observed;
                    }

                    JToken returned = cap["returned"];
                    if (returned == null || returned.Type != JTokenType.Integer || (long)returned < 0)
                    {
                        error = Invalid("caps." + property.Name + ".returned must be a non-negative integer");
                        return false;
                    }

                    JToken truncated = cap["truncated"];
                    if (truncated == null)
                    {
                        error = Invalid("caps." + property.Name + ".truncated is required; use null when unknown");
                        return false;
                    }

                    bool? truncatedValue = null;
                    if (truncated.Type != JTokenType.Null)
                    {
                        if (truncated.Type != JTokenType.Boolean)
                        {
                            error = Invalid("caps." + property.Name + ".truncated must be null or a boolean");
                            return false;
                        }

                        truncatedValue = (bool)truncated;
                    }

                    if (!ValidateCap(
                        property.Name,
                        (long)limit,
                        observedValue,
                        (long)returned,
                        truncatedValue,
                        out string capError))
                    {
                        error = Invalid(capError);
                        return false;
                    }
                }

                JToken errors = obj["errors"];
                if (errors == null || errors.Type != JTokenType.Array)
                {
                    error = Invalid("errors is required and must be an array of strings");
                    return false;
                }

                foreach (JToken item in errors.Children())
                {
                    if (item.Type != JTokenType.String)
                    {
                        error = Invalid("errors must contain only strings");
                        return false;
                    }
                }
            }

            error = null;
            return true;
        }

        /// <summary>
        /// Checks the cross-field cap rules a source-built result must satisfy before it can be
        /// sent. The exported JSON Schema cannot express them, so the DTO and every producer use
        /// this helper; a contradiction is an error, never a silently clamped count.
        /// </summary>
        public static bool TryValidateInspectionCaps(Dictionary<string, InspectionCap> caps, out string error)
        {
            if (caps == null)
            {
                error = "caps is required and must be an object";
                return false;
            }

            foreach (KeyValuePair<string, InspectionCap> entry in caps)
            {
                if (entry.Value == null)
                {
                    error = "caps." + entry.Key + " must be an object";
                    return false;
                }

                if (!ValidateCap(
                    entry.Key,
                    entry.Value.Limit,
                    entry.Value.Observed,
                    entry.Value.Returned,
                    entry.Value.Truncated,
                    out error))
                {
                    return false;
                }
            }

            error = null;
            return true;
        }

        private static bool ValidateCap(
            string name,
            long limit,
            long? observed,
            long returned,
            bool? truncated,
            out string error)
        {
            string prefix = "caps." + name;
            if (limit <= 0)
            {
                error = prefix + ".limit must be greater than 0";
                return false;
            }

            if (returned < 0)
            {
                error = prefix + ".returned must be a non-negative integer";
                return false;
            }

            if (returned > limit)
            {
                error = prefix + ".returned must not exceed limit";
                return false;
            }

            if (observed.HasValue)
            {
                if (observed.Value < 0)
                {
                    error = prefix + ".observed must be null or a non-negative integer";
                    return false;
                }

                if (observed.Value < returned)
                {
                    error = prefix + ".observed must be at least returned";
                    return false;
                }

                if (truncated == true && returned >= observed.Value)
                {
                    error = prefix + ".truncated is true but returned covers observed";
                    return false;
                }

                if (truncated == false && returned != observed.Value)
                {
                    error = prefix + ".truncated is false but returned differs from observed";
                    return false;
                }
            }

            error = null;
            return true;
        }

        private static bool ValidateEffects(JObject obj, string name, out ProtocolError error)
        {
            JToken effects = obj[name];
            if (effects == null || effects.Type == JTokenType.Null)
            {
                error = null;
                return true;
            }

            if (effects.Type != JTokenType.Array)
            {
                error = Invalid(name + " must be an array");
                return false;
            }

            foreach (JToken item in effects.Children())
            {
                if (item.Type != JTokenType.Object || item["field"]?.Type != JTokenType.String)
                {
                    error = Invalid(name + " entries need a field string");
                    return false;
                }

                JToken delta = item["delta"];
                if (delta != null && delta.Type != JTokenType.Null && delta.Type != JTokenType.Float && delta.Type != JTokenType.Integer)
                {
                    error = Invalid(name + " delta must be null or a number");
                    return false;
                }

                JToken spread = item["spread"];
                if (spread != null && spread.Type != JTokenType.Null
                    && ((spread.Type != JTokenType.Float && spread.Type != JTokenType.Integer) || (double)spread < 0))
                {
                    error = Invalid(name + " spread must be null or a non-negative number");
                    return false;
                }

                JToken unit = item["unit"];
                if (unit != null && unit.Type != JTokenType.Null && unit.Type != JTokenType.String)
                {
                    error = Invalid(name + " unit must be null or a string");
                    return false;
                }
            }

            error = null;
            return true;
        }

        private static bool HasNumber(JObject obj, string name)
        {
            JToken token = obj[name];
            return token != null && (token.Type == JTokenType.Float || token.Type == JTokenType.Integer);
        }

        private static bool IsOptionalNumber(JObject obj, string name)
        {
            JToken token = obj[name];
            return token == null || token.Type == JTokenType.Null || token.Type == JTokenType.Float || token.Type == JTokenType.Integer;
        }

        private static bool IsPositiveFinite(double value)
        {
            return value > 0 && !double.IsNaN(value) && !double.IsInfinity(value);
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
