// upa/1 wire messages, read strictly.
//
// The Pydantic models in src/playtest/contracts/ are the authority; this file mirrors the parts of
// them that the three.js bridge reads. An unknown member, a wrong type or a missing required field is
// refused, never coerced.

export const PROTOCOL = "upa/1";

const ENVELOPE = ["protocol", "session", "seq", "game_s", "wall_utc", "type"];

const REQUESTS = {
  hello: { token: "string", core_version: "string" },
  observe: { mode: "string", with_frame: "boolean?", rate_hz: "number?" },
  clock: { op: "string", step_game_s: "number?", time_scale: "number?" },
  stop: { reason: "string" },
  inspect: { stage: "string" },
};

const OBSERVE_MODES = ["subscribe", "once", "unsubscribe"];
const CLOCK_OPS = ["pause", "resume", "step", "set_scale"];
const STOP_REASONS = ["user", "reset", "scene_change", "disconnect", "error", "finish", "release"];
const INSPECT_STAGES = ["inventory", "discovery", "transforms", "fields"];

// Verbs this bridge recognises but does not serve yet.
export const UNSUPPORTED = Object.freeze(["act", "place"]);

export class ProtocolError extends Error {
  constructor(code, message, inReplyTo = null) {
    super(message);
    this.code = code;
    this.inReplyTo = inReplyTo;
  }
}

function typeOk(value, kind) {
  const optional = kind.endsWith("?");
  if (value === undefined || value === null) return optional;
  const base = optional ? kind.slice(0, -1) : kind;
  if (base === "number") return typeof value === "number" && Number.isFinite(value);
  return typeof value === base;
}

function require(condition, message, seq) {
  if (!condition) throw new ProtocolError("invalid", message, seq);
}

/**
 * Parse one line from the core. Returns `{ type, body }` for a request this bridge serves or
 * recognises; throws a ProtocolError otherwise.
 */
export function readRequest(line) {
  let body;
  try {
    body = JSON.parse(line);
  } catch {
    throw new ProtocolError("invalid", "malformed JSON");
  }
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    throw new ProtocolError("invalid", "a message must be a JSON object");
  }
  const seq = Number.isInteger(body.seq) && body.seq >= 0 ? body.seq : null;
  if (typeof body.protocol !== "string" || !/^upa\/\d+$/.test(body.protocol)) {
    throw new ProtocolError("invalid", "protocol must look like upa/N", seq);
  }
  if (body.protocol !== PROTOCOL) {
    throw new ProtocolError("protocol", `this bridge speaks ${PROTOCOL}, not ${body.protocol}`, seq);
  }
  require(typeof body.session === "string" && body.session.length > 0, "session must be a non-empty string", seq);
  require(seq !== null, "seq must be an integer >= 0", seq);
  require(body.game_s === undefined || body.game_s === null || (typeOk(body.game_s, "number") && body.game_s >= 0),
    "game_s must be null or a number >= 0", seq);
  require(typeof body.wall_utc === "string", "wall_utc must be a timestamp string", seq);

  const type = body.type;
  if (UNSUPPORTED.includes(type)) return { type, body };
  const fields = REQUESTS[type];
  require(fields !== undefined, `unknown verb ${JSON.stringify(type)}`, seq);
  for (const key of Object.keys(body)) {
    require(ENVELOPE.includes(key) || key in fields, `unknown member ${key} in ${type}`, seq);
  }
  for (const [key, kind] of Object.entries(fields)) {
    require(typeOk(body[key], kind), `${type}.${key} must be ${kind.replace("?", " or null")}`, seq);
  }

  if (type === "hello") {
    require(body.token.length > 0, "hello.token must be non-empty", seq);
  } else if (type === "observe") {
    require(OBSERVE_MODES.includes(body.mode), `unknown observe mode ${body.mode}`, seq);
    require(body.rate_hz == null || (body.rate_hz > 0 && body.rate_hz <= 60), "rate_hz must be in (0, 60]", seq);
  } else if (type === "clock") {
    require(CLOCK_OPS.includes(body.op), `unknown clock op ${body.op}`, seq);
    require((body.op === "step") === (body.step_game_s != null), "step_game_s is required exactly when op is 'step'", seq);
    require((body.op === "set_scale") === (body.time_scale != null),
      "time_scale is required exactly when op is 'set_scale'", seq);
    require(body.step_game_s == null || body.step_game_s > 0, "step_game_s must be > 0", seq);
    require(body.time_scale == null || body.time_scale >= 0, "time_scale must be >= 0", seq);
  } else if (type === "stop") {
    require(STOP_REASONS.includes(body.reason), `unknown stop reason ${body.reason}`, seq);
  } else if (type === "inspect") {
    require(INSPECT_STAGES.includes(body.stage), `unknown inspect stage ${body.stage}`, seq);
  }
  return { type, body };
}

export function envelope(session, seq, gameS) {
  return { protocol: PROTOCOL, session, seq, game_s: gameS, wall_utc: new Date().toISOString() };
}

export function errorMessage(session, seq, gameS, code, message, inReplyTo) {
  return { ...envelope(session, seq, gameS), type: "error", code, message, in_reply_to: inReplyTo };
}
