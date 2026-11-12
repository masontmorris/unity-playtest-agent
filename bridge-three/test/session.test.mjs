import assert from "node:assert/strict";
import { test } from "node:test";

import { VirtualClock } from "../src/clock.js";
import { BridgeSession } from "../src/session.js";
import { cameraFacing, manualTimers } from "../support/doubles.mjs";

let seq = 0;
function line(type, fields = {}, session = "core-1") {
  seq += 1;
  return JSON.stringify({ protocol: "upa/1", session, seq, game_s: null, wall_utc: "2026-11-10T12:00:00Z", type, ...fields });
}

function harness({ attached = { sceneId: "level", camera: cameraFacing([1, 0, 0]) } } = {}) {
  const clock = new VirtualClock({ stepFrameS: 0.1 });
  const sent = [];
  const timers = manualTimers();
  let ended = 0;
  let view = attached;
  const session = new BridgeSession({
    clock, view: () => view, project: "example", engineVersion: "176",
    send: (message) => sent.push(message), end: () => { ended += 1; }, timers,
  });
  session.advertise({ token: "secret", pid: 42, port: 5000 });
  return {
    clock, sent, timers, session,
    get ended() { return ended; },
    setView(next) { view = next; },
    last: () => sent[sent.length - 1],
  };
}

const hello = (token = "secret") => line("hello", { token, core_version: "0.0.1" });

test("hello with the token gets a three status", () => {
  const h = harness();
  h.session.receive(hello());
  const status = h.last();
  assert.equal(status.type, "status");
  assert.equal(status.session, "core-1");
  assert.equal(status.engine, "three");
  assert.equal(status.engine_version, "176");
  assert.equal(status.unity_version, null);
  assert.deepEqual(status.input_backends, []);
  assert.equal(status.capture, false);
  assert.deepEqual(status.lock, { held: true, owner_pid: 42, port: 5000 });
});

test("a wrong token or a message before hello is refused and closes the connection", () => {
  const wrong = harness();
  wrong.session.receive(hello("nope"));
  assert.equal(wrong.last().code, "auth");
  assert.equal(wrong.ended, 1);

  const early = harness();
  early.session.receive(line("observe", { mode: "once", with_frame: false, rate_hz: null }));
  assert.equal(early.last().code, "invalid");
  assert.equal(early.ended, 1);
});

test("another protocol version gets a protocol error", () => {
  const h = harness();
  h.session.receive(JSON.stringify({ ...JSON.parse(hello()), protocol: "upa/2" }));
  assert.equal(h.last().code, "protocol");
  assert.equal(h.ended, 1);
});

test("observe once reports the attached scene, the clock and the camera", () => {
  const h = harness();
  h.session.receive(hello());
  h.clock.step(0.5);
  h.session.receive(line("observe", { mode: "once", with_frame: false, rate_hz: null }));
  const observation = h.last();
  assert.equal(observation.type, "observation");
  assert.equal(observation.scene, "level");
  assert.equal(observation.playing, true);
  assert.equal(observation.frame, 5);
  assert.equal(observation.game_s, 0.5);
  assert.equal(observation.clock_mode, "realtime");
  assert.equal(observation.view.yaw_deg, 90);
  assert.equal(observation.frame_ref, null);
  assert.deepEqual(observation.reader_errors, []);
});

test("with no scene attached the observation says so instead of inventing one", () => {
  const h = harness({ attached: null });
  h.session.receive(hello());
  h.session.receive(line("observe", { mode: "once", with_frame: false, rate_hz: null }));
  const observation = h.last();
  assert.equal(observation.playing, false);
  assert.equal(observation.game_s, null);
  assert.equal(observation.view, null);
  assert.deepEqual(observation.reader_errors, [{ reader: "scene", error: "no scene is attached" }]);
});

test("subscribe streams at the requested rate until unsubscribed or disconnected", () => {
  const h = harness();
  h.session.receive(hello());
  h.session.receive(line("observe", { mode: "subscribe", with_frame: false, rate_hz: 10 }));
  assert.equal(h.timers.active, 1);
  h.timers.fire();
  h.timers.fire();
  assert.equal(h.sent.filter((m) => m.type === "observation").length, 2);
  h.session.receive(line("observe", { mode: "unsubscribe", with_frame: false, rate_hz: null }));
  assert.equal(h.timers.active, 0);
  h.session.receive(line("observe", { mode: "subscribe", with_frame: false, rate_hz: null }));
  h.session.disconnected();
  assert.equal(h.timers.active, 0);
});

test("clock ops change the clock and reply with status", () => {
  const h = harness();
  h.session.receive(hello());
  h.session.receive(line("clock", { op: "pause", step_game_s: null, time_scale: null }));
  assert.equal(h.last().type, "status");
  assert.equal(h.clock.paused, true);
  h.session.receive(line("clock", { op: "step", step_game_s: 0.3, time_scale: null }));
  assert.ok(Math.abs(h.clock.gameS - 0.3) < 1e-12);
  h.session.receive(line("clock", { op: "set_scale", step_game_s: null, time_scale: 2 }));
  assert.equal(h.clock.timeScale, 2);
  h.session.receive(line("clock", { op: "resume", step_game_s: null, time_scale: null }));
  assert.equal(h.clock.paused, false);
  assert.equal(h.ended, 0);
});

test("a clock op with the wrong arguments is invalid", () => {
  const h = harness();
  h.session.receive(hello());
  h.session.receive(line("clock", { op: "step", step_game_s: null, time_scale: null }));
  assert.equal(h.last().code, "invalid");
  assert.match(h.last().message, /step_game_s/);
});

test("unknown members are refused rather than ignored", () => {
  const h = harness();
  h.session.receive(hello());
  h.session.receive(line("observe", { mode: "once", with_frame: false, rate_hz: null, extra: 1 }));
  assert.equal(h.last().code, "invalid");
  assert.match(h.last().message, /unknown member extra/);
});

test("unknown acts are rejected as data and place stays refused; inspect has no source", () => {
  const h = harness();
  h.session.receive(hello());
  h.session.receive(line("act", { action_id: "x", choice_id: "c", action: "anything", params: {}, based_on_seq: 0, max_age_s: 1, depends_on: [], lease_s: null }));
  assert.deepEqual([h.last().status, h.last().reject_reason], ["rejected", "unknown_action"]);
  h.session.receive(line("place", {}));
  assert.equal(h.last().code, "invalid");
  h.session.receive(line("inspect", { stage: "inventory" }));
  assert.equal(h.last().code, "internal");
  assert.equal(h.ended, 0);
});

test("stop releases nothing, verifies it, and ends the session unless it is a release", () => {
  const h = harness();
  h.session.receive(hello());
  h.session.receive(line("stop", { reason: "release" }));
  assert.deepEqual([h.last().type, h.last().released, h.last().release_verified], ["stopped", [], true]);
  assert.equal(h.ended, 0);
  h.session.receive(line("stop", { reason: "finish" }));
  assert.equal(h.ended, 1);
  h.session.receive(hello());
  assert.equal(h.last().type, "status", "a new core can say hello after the old one stopped");
});

test("attached readers fill public; a failing or non-JSON reader is null with an error", () => {
  let coins = 3;
  const readers = {
    coins: () => coins,
    grounded: () => true,
    position: () => [1.5, 0, -2],
    broken: () => { throw new Error("player not spawned"); },
    infinite: () => Infinity,
    object: () => ({ nested: 1 }),
  };
  const h = harness({ attached: { sceneId: "level", camera: cameraFacing([1, 0, 0]), readers } });
  h.session.receive(hello());
  h.session.receive(line("observe", { mode: "once", with_frame: false, rate_hz: null }));
  let observation = h.last();
  assert.deepEqual(observation.public, { coins: 3, grounded: true, position: [1.5, 0, -2], broken: null, infinite: null, object: null });
  assert.deepEqual(observation.reader_errors.map((e) => e.reader), ["broken", "infinite", "object"]);
  assert.match(observation.reader_errors[0].error, /player not spawned/);
  coins = 4;
  h.session.receive(line("observe", { mode: "once", with_frame: false, rate_hz: null }));
  observation = h.last();
  assert.equal(observation.public.coins, 4);
});
