import assert from "node:assert/strict";
import { test } from "node:test";

import { installClock, VirtualClock } from "../src/clock.js";

test("realtime frames advance by the real interval times the scale", () => {
  const clock = new VirtualClock();
  const seen = [];
  const loop = (t) => {
    seen.push(t);
    clock.requestAnimationFrame(loop);
  };
  clock.requestAnimationFrame(loop);
  clock.tick(1000);
  clock.tick(1016);
  clock.setScale(0.5);
  clock.tick(1036);
  assert.deepEqual(seen, [16, 26]);
  assert.equal(clock.frame, 2);
  assert.equal(clock.now(), 26);
});

test("a paused clock runs no frames and does not jump on resume", () => {
  const clock = new VirtualClock();
  let calls = 0;
  const loop = () => {
    calls += 1;
    clock.requestAnimationFrame(loop);
  };
  clock.requestAnimationFrame(loop);
  clock.tick(0);
  clock.tick(10);
  clock.pause();
  clock.tick(500);
  assert.equal(calls, 1);
  assert.equal(clock.now(), 10);
  clock.resume();
  clock.tick(520);
  assert.equal(calls, 2);
  assert.equal(clock.now(), 30);
});

test("step advances exactly the requested game time, even while paused", () => {
  const clock = new VirtualClock({ mode: "stepped", stepFrameS: 0.1 });
  const times = [];
  const loop = (t) => {
    times.push(t);
    clock.requestAnimationFrame(loop);
  };
  clock.requestAnimationFrame(loop);
  clock.tick(0);
  clock.tick(1000);
  assert.equal(clock.frame, 0, "a stepped clock ignores real frames");
  clock.pause();
  assert.equal(clock.step(0.25), 3);
  assert.equal(clock.gameS, 0.25);
  assert.deepEqual(times.map((t) => Math.round(t)), [100, 200, 250]);
});

test("cancelled callbacks do not run and a throwing callback does not stop the frame", () => {
  const errors = [];
  const clock = new VirtualClock({ onError: (error) => errors.push(error.message) });
  let ran = 0;
  const id = clock.requestAnimationFrame(() => { ran += 100; });
  clock.cancelAnimationFrame(id);
  clock.requestAnimationFrame(() => { throw new Error("boom"); });
  clock.requestAnimationFrame(() => { ran += 1; });
  clock.step(0.01);
  assert.equal(ran, 1);
  assert.deepEqual(errors, ["boom"]);
});

test("invalid arguments are refused", () => {
  assert.throws(() => new VirtualClock({ mode: "fast" }), RangeError);
  const clock = new VirtualClock();
  assert.throws(() => clock.step(0), RangeError);
  assert.throws(() => clock.setScale(-1), RangeError);
  assert.throws(() => clock.setScale(Number.NaN), RangeError);
});

test("installClock routes the page's frame and time functions through the clock and restores them", () => {
  const real = [];
  let realNow = 0;
  const performance = { now: () => realNow };
  const target = {
    performance,
    requestAnimationFrame(callback) { real.push(callback); return real.length; },
    cancelAnimationFrame() {},
  };
  const originalRaf = target.requestAnimationFrame;
  const clock = new VirtualClock();
  const uninstall = installClock(clock, target);

  let gameTime = null;
  target.requestAnimationFrame((t) => { gameTime = t; });
  const driveOnce = () => real.shift()();
  realNow = 100;
  driveOnce();
  realNow = 150;
  driveOnce();
  assert.equal(gameTime, 50);
  assert.equal(target.performance.now(), 50);

  uninstall();
  assert.equal(target.performance.now(), 150);
  assert.notEqual(target.requestAnimationFrame, originalRaf, "the restored function is the bound original");
  target.requestAnimationFrame(() => {});
  assert.equal(real.length, 2);
});
