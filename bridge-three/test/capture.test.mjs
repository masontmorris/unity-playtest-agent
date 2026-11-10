import assert from "node:assert/strict";
import { test } from "node:test";

import { FrameCapture } from "../src/capture.js";

function harness({ visible = true, encode = async () => "AAAA" } = {}) {
  let now = 0;
  const sent = [];
  let copies = 0;
  const capture = new FrameCapture({
    grabber: { copy: () => { copies += 1; return { visible }; }, encode },
    send: (message) => sent.push(message),
    realNow: () => now,
  });
  return { capture, sent, advance(ms) { now += ms; }, get copies() { return copies; } };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("frames are grabbed at most once per interval and referenced only once written", async () => {
  const h = harness();
  h.capture.afterFrame();
  h.capture.afterFrame();
  await settle();
  assert.equal(h.copies, 1);
  assert.deepEqual(h.sent.map((m) => m.name), ["f-000001.jpg"]);
  assert.equal(h.capture.latestFrameRef, null, "an unconfirmed frame is never referenced");
  assert.equal(h.capture.selfTest, "unknown");
  h.capture.written({ name: "f-000001.jpg", path: "/p/f-000001.jpg" });
  assert.equal(h.capture.latestFrameRef, "/p/f-000001.jpg");
  assert.equal(h.capture.selfTest, "passed");
  h.advance(50);
  h.capture.afterFrame();
  assert.equal(h.copies, 1, "the next frame waits for the interval");
  h.advance(60);
  h.capture.afterFrame();
  assert.equal(h.copies, 2);
});

test("a blank first frame fails the self-test and stops capture", () => {
  const h = harness({ visible: false });
  h.capture.afterFrame();
  assert.equal(h.capture.selfTest, "failed");
  assert.match(h.capture.lastError, /transparent/);
  h.advance(1000);
  h.capture.afterFrame();
  assert.equal(h.copies, 1);
});

test("a failed write fails the self-test and keeps the reference unset", async () => {
  const h = harness();
  h.capture.afterFrame();
  await settle();
  h.capture.written({ name: "f-000001.jpg", error: "disk full" });
  assert.equal(h.capture.selfTest, "failed");
  assert.equal(h.capture.latestFrameRef, null);
});
