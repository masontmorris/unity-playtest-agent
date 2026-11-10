import assert from "node:assert/strict";
import { test } from "node:test";

import { VirtualClock } from "../src/clock.js";
import { BridgeSession } from "../src/session.js";
import { cameraFacing } from "../support/doubles.mjs";

const definition = (inputs, hold = 0.04) => ({ backend: "input_system", inputs, default_lease_s: hold });
const key = (control) => ({ device: "Keyboard", control, x: null, y: null });
const envelope = (seq, type, fields = {}) => JSON.stringify({ protocol: "upa/1", session: "s", seq, game_s: null, wall_utc: new Date().toISOString(), type, ...fields });
const waitUntil = async (predicate, ms = 1000) => {
  const end = Date.now() + ms;
  while (!predicate()) { if (Date.now() > end) throw new Error("timed out"); await new Promise(resolve => setTimeout(resolve, 2)); }
};

function setup(catalog, { attached = true } = {}) {
  const clock = new VirtualClock();
  const events = [];
  const listeners = new Map();
  const target = { addEventListener(type, callback) { const list = listeners.get(type) ?? []; list.push(callback); listeners.set(type, list); },
    dispatchEvent(value) { events.push(value); for (const callback of listeners.get(value.type) ?? []) callback(value); return true; }, navigator: { getGamepads: () => [] } };
  const sent = [];
  let ended = false;
  const session = new BridgeSession({ clock, view: () => attached ? { sceneId: "sim", camera: cameraFacing([0, 0, -1]) } : null,
    project: "sim", catalog, catalogSha256: "a".repeat(64), target, send: message => sent.push(message), end: () => { ended = true; } });
  session.advertise({ token: "token", pid: 1, port: 2 });
  let seq = 0;
  const send = (type, fields = {}) => { seq += 1; session.receive(envelope(seq, type, fields)); return seq; };
  send("hello", { token: "token", core_version: "test" });
  const observe = () => {
    send("observe", { mode: "once", with_frame: false, rate_hz: null });
    return sent.findLast(item => item.type === "observation").seq;
  };
  const act = (action, fields = {}) => send("act", { action_id: `${action}-${seq}`, choice_id: "choice", action,
    params: fields.params ?? {}, based_on_seq: fields.based_on_seq, max_age_s: fields.max_age_s ?? 2, depends_on: fields.depends_on ?? [], lease_s: fields.lease_s ?? null });
  const tick = setInterval(() => clock.tick(performance.now()), 2);
  return { clock, events, target, sent, send, observe, act, close: () => clearInterval(tick), get ended() { return ended; } };
}

async function terminal(h, actionId, count = 1) {
  await waitUntil(() => h.sent.filter(x => x.type === "execution" && x.action_id === actionId).filter(x => ["completed", "cancelled", "failed", "timed_out", "rejected"].includes(x.status)).length >= count);
  return h.sent.filter(x => x.type === "execution" && x.action_id === actionId).at(-1);
}

test("a keyboard hold and chord emit matching key events and measured receipts", async () => {
  const h = setup({ actions: { hold: definition([key("a")]), chord: definition([key("leftshift"), key("digit2")]) } });
  try {
    const observation = h.observe();
    h.act("hold", { based_on_seq: observation });
    const held = await terminal(h, "hold-2");
    assert.equal(held.status, "completed");
    assert.ok(held.applied_inputs[0].applied_duration_s >= 0.03);
    assert.equal(held.applied_inputs[0].release_verified, true);
    assert.deepEqual(h.events.filter(x => x.type.startsWith("key")).slice(0, 2).map(x => [x.type, x.code, x.key]), [["keydown", "KeyA", "a"], ["keyup", "KeyA", "a"]]);

    const next = h.observe();
    h.act("chord", { based_on_seq: next });
    const chord = await terminal(h, "chord-4");
    assert.deepEqual(chord.applied_inputs.map(x => x.control), ["leftshift", "digit2"]);
    assert.ok(Math.max(...chord.applied_inputs.map(x => x.input_start_wall_s)) - Math.min(...chord.applied_inputs.map(x => x.input_start_wall_s)) <= 0.01);
  } finally { h.close(); }
});

test("a skill releases, waits, then applies its next step", async () => {
  const h = setup({ actions: { skill: { backend: "input_system", max_s: 0.2, steps: [
    { inputs: [key("a")], hold_s: 0.02, wait_s: 0.04 }, { inputs: [key("b")], hold_s: 0.02, wait_s: 0.01 },
  ] } } });
  try {
    const observation = h.observe(); h.act("skill", { based_on_seq: observation });
    const record = await terminal(h, "skill-2");
    assert.equal(record.status, "completed");
    assert.deepEqual(record.applied_inputs.map(x => x.step_index), [0, 1]);
    assert.ok(record.applied_inputs[1].input_start_wall_s - record.applied_inputs[0].input_end_wall_s >= 0.03);
    assert.ok(record.applied_duration_s >= 0.08);
  } finally { h.close(); }
});

test("mouse delta reports the dispatched pixel impulse", async () => {
  const h = setup({ actions: { move: definition([{ device: "Mouse", control: "delta", x: 0.5, y: -0.25 }], 0.02) } });
  try {
    const observation = h.observe(); h.act("move", { based_on_seq: observation });
    const record = await terminal(h, "move-2");
    assert.equal(record.applied_inputs[0].applied_delta_x, 32);
    assert.equal(record.applied_inputs[0].applied_delta_y, -16);
    assert.equal(record.applied_inputs[0].applied_duration_s, null);
    assert.equal(record.applied_inputs[0].release_verified, true);
  } finally { h.close(); }
});

test("gamepad axes and trigger use standard positions and neutralize on release", async () => {
  const h = setup({ actions: { pad: definition([
    { device: "Gamepad", control: "leftStick", x: 0.5, y: 0.25 },
    { device: "Gamepad", control: "leftTrigger", x: 0.7, y: 0 },
  ]) } });
  try {
    const observation = h.observe(); h.act("pad", { based_on_seq: observation });
    await waitUntil(() => h.sent.some(x => x.type === "execution" && x.action_id === "pad-2" && x.status === "running"));
    const gamepad = h.target.navigator.getGamepads()[0];
    assert.equal(gamepad.mapping, "standard"); assert.equal(gamepad.connected, true);
    assert.deepEqual(gamepad.axes, [0.5, -0.25, 0, 0]);
    assert.equal(gamepad.buttons[6].value, 0.7);
    const record = await terminal(h, "pad-2");
    assert.equal(record.status, "completed");
    assert.deepEqual(h.target.navigator.getGamepads()[0].axes, [0, 0, 0, 0]);
    assert.equal(h.target.navigator.getGamepads()[0].buttons[6].value, 0);
  } finally { h.close(); }
});

test("a newer act cancels the active lease, and stop releases a held input", async () => {
  const h = setup({ actions: { long: definition([key("a")], 0.5), short: definition([key("b")], 0.02) } });
  try {
    const observation = h.observe(); h.act("long", { based_on_seq: observation });
    await waitUntil(() => h.sent.some(x => x.type === "execution" && x.action_id === "long-2" && x.status === "running"));
    h.act("short", { based_on_seq: observation });
    assert.equal((await terminal(h, "long-2")).status, "cancelled");
    assert.equal((await terminal(h, "short-3")).status, "completed");
    const next = h.observe(); h.act("long", { based_on_seq: next });
    await waitUntil(() => h.sent.some(x => x.type === "execution" && x.action_id === "long-5" && x.status === "running"));
    h.send("stop", { reason: "release" });
    await waitUntil(() => h.sent.some(x => x.type === "stopped"));
    assert.equal(h.sent.findLast(x => x.type === "stopped").release_verified, true);
    assert.deepEqual(h.sent.findLast(x => x.type === "stopped").released, ["key:a"]);
    assert.equal((await terminal(h, "long-5")).status, "cancelled");
  } finally { h.close(); }
});

test("stale requests, unknown actions, invalid params, and non-playing observations reject with their reasons", async () => {
  const h = setup({ actions: { hold: definition([key("a")]) } });
  try {
    const observation = h.observe();
    h.act("hold", { based_on_seq: 999 });
    assert.equal(h.sent.at(-1).reject_reason, "stale_age");
    await new Promise(resolve => setTimeout(resolve, 3));
    h.act("hold", { based_on_seq: observation, max_age_s: 0.000001 });
    assert.equal(h.sent.at(-1).reject_reason, "stale_age");
    h.act("hold", { based_on_seq: observation, depends_on: ["missing"] });
    assert.equal(h.sent.at(-1).reject_reason, "stale_fields");
    h.act("hold", { based_on_seq: observation, params: { changed: true } });
    assert.equal(h.sent.at(-1).reject_reason, "invalid_params");
    h.act("unknown", { based_on_seq: observation });
    assert.equal(h.sent.at(-1).reject_reason, "unknown_action");
  } finally { h.close(); }
  const stopped = setup({ actions: { hold: definition([key("a")]) } }, { attached: false });
  try { const observation = stopped.observe(); stopped.act("hold", { based_on_seq: observation }); assert.equal(stopped.sent.at(-1).reject_reason, "not_playing"); }
  finally { stopped.close(); }
});

test("an invalid catalog fails closed", async () => {
  const h = setup({ actions: { bad: { backend: "legacy", device: "Keyboard", control: "a", default_lease_s: 1 } } });
  try { const observation = h.observe(); h.act("bad", { based_on_seq: observation }); assert.equal(h.sent.at(-1).reject_reason, "invalid_params"); }
  finally { h.close(); }
});

test("a paused virtual clock cannot keep an unobserved input held", async () => {
  const h = setup({ actions: { hold: definition([key("a")], 1) } });
  try {
    h.clock.pause();
    const observation = h.observe(); h.act("hold", { based_on_seq: observation });
    const record = await terminal(h, "hold-2");
    assert.equal(record.status, "failed");
    assert.equal(record.release_verified, true);
    assert.ok(h.events.some(x => x.type === "keyup"));
  } finally { h.close(); }
});
