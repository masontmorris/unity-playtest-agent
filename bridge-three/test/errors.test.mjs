import assert from "node:assert/strict";
import { test } from "node:test";

import { ErrorMonitor, watchPage } from "../src/errors.js";

test("repeats merge, distinct errors count, and the window reports truncation", () => {
  const monitor = new ErrorMonitor({ now: () => 7 });
  monitor.record("Error", "same");
  monitor.record("Error", "same");
  monitor.record("Exception", new TypeError("bad"));
  const snapshot = monitor.snapshot();
  assert.equal(snapshot.active, true);
  assert.equal(snapshot.observed, 3);
  assert.equal(snapshot.truncated, false);
  assert.deepEqual(snapshot.events.map((e) => [e.message, e.count, e.first_sequence, e.last_sequence]),
    [["same", 2, 1, 2], ["TypeError: bad", 1, 3, 3]]);
  for (let i = 0; i < 70; i += 1) monitor.record("Error", `e${i}`);
  assert.equal(monitor.snapshot().truncated, true);
  assert.equal(monitor.snapshot().events.length, 64);
});

test("the page's error events and console.error feed the monitor, and detaching restores console.error", () => {
  const listeners = {};
  const printed = [];
  const original = (...args) => printed.push(args.join(" "));
  const target = {
    addEventListener: (type, listener) => { listeners[type] = listener; },
    removeEventListener: (type) => { delete listeners[type]; },
    console: { error: original },
  };
  const monitor = new ErrorMonitor();
  const detach = watchPage(monitor, target);
  listeners.error({ error: new Error("thrown") });
  listeners.unhandledrejection({ reason: "rejected" });
  target.console.error("logged", 3);
  assert.deepEqual(monitor.snapshot().events.map((e) => [e.type, e.message]),
    [["Exception", "Error: thrown"], ["Exception", "rejected"], ["Error", "logged 3"]]);
  assert.deepEqual(printed, ["logged 3"]);
  detach();
  assert.equal(target.console.error, original);
  assert.deepEqual(listeners, {});
});
