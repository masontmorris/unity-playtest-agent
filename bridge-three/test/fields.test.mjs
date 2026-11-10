import assert from "node:assert/strict";
import { test } from "node:test";

import { VirtualClock } from "../src/clock.js";
import { inspectFields } from "../src/inspect.js";
import { DomInputSurface } from "../src/input-surface.js";

function eventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, callback) { listeners.set(type, [...(listeners.get(type) ?? []), callback]); },
    dispatchEvent(event) { for (const callback of listeners.get(event.type) ?? []) callback(event); return true; },
  };
}

function surface() {
  const clock = new VirtualClock();
  const documentTarget = eventTarget();
  const target = { window: eventTarget(), document: documentTarget, navigator: { getGamepads: () => [] } };
  return { clock, surface: new DomInputSurface({ clock, target, documentTarget, canvas: eventTarget() }) };
}

test("the input state lists held keys and buttons per device, as the Unity collector does", () => {
  const { surface: dom } = surface();
  dom.apply({ device: "Keyboard", control: "w" });
  dom.apply({ device: "Gamepad", control: "leftStick", x: 0.5, y: 1 });
  dom.apply({ device: "Gamepad", control: "buttonSouth" });
  const state = dom.state();
  assert.deepEqual(state.Keyboard.pressed, ["w"]);
  assert.deepEqual(state.Gamepad.pressed, ["buttonSouth"]);
  assert.deepEqual(state.Gamepad.values.leftStick, [0.5, 1], "Unity's +y is up");
  dom.release({ device: "Keyboard", control: "w" });
  dom.release({ device: "Gamepad", control: "leftStick" });
  dom.release({ device: "Gamepad", control: "buttonSouth" });
  const after = dom.state();
  assert.deepEqual(after.Keyboard.pressed, []);
  assert.deepEqual(after.Gamepad.pressed, []);
  assert.deepEqual(after.Gamepad.values.leftStick, [0, 0]);
});

test("a mouse delta reads as moving only until the game frame after it has run", () => {
  const { clock, surface: dom } = surface();
  dom.apply({ device: "Mouse", control: "delta", x: 0.5, y: -0.25 });
  assert.deepEqual(dom.state().Mouse.values.delta, [32, -16]);
  clock.step(1 / 60);
  assert.deepEqual(dom.state().Mouse.values.delta, [32, -16], "the first frame after delivery still sees it");
  clock.step(1 / 60);
  assert.deepEqual(dom.state().Mouse.values.delta, [0, 0]);
});

test("the fields inspection carries the input state and no game fields, with complete caps", () => {
  const { clock, surface: dom } = surface();
  const reading = inspectFields({ clock, realTimeS: 2.5, inputState: dom.state() });
  assert.equal(reading.data.input_state.active, true);
  assert.equal(reading.data.input_state.update_type, "dynamic");
  assert.equal(reading.data.input_state.real_time_s, 2.5);
  assert.deepEqual(reading.data.fields, []);
  assert.deepEqual(reading.caps.fields, { limit: 4096, observed: 0, returned: 0, truncated: false });
  const none = inspectFields({ clock, realTimeS: 2.5, inputState: null });
  assert.equal(none.data.input_state.active, false);
  assert.match(none.data.input_state.error, /no input surface/);
});
