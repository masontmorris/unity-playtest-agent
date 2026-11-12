import assert from "node:assert/strict";
import { test } from "node:test";

import { BLANK_GRACE_MS, FrameCapture, MEASURES, canvasGrabber } from "../src/capture.js";

function harness({ visible = true, encode = async () => "AAAA" } = {}) {
  let now = 0;
  const sent = [];
  let copies = 0;
  const capture = new FrameCapture({
    grabber: { copy: () => { copies += 1; return { visible: typeof visible === "function" ? visible() : visible }; }, encode },
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

test("blank frames before the game draws are skipped, and a later visible frame passes", async () => {
  let drawn = false;
  const h = harness({ visible: () => drawn });
  h.capture.afterFrame();
  h.advance(5000);
  h.capture.afterFrame();
  assert.equal(h.capture.selfTest, "unknown", "a game that has not drawn yet is not a failure");
  assert.equal(h.sent.length, 0, "blank frames are never written");
  drawn = true;
  h.advance(200);
  h.capture.afterFrame();
  await settle();
  h.capture.written({ name: "f-000001.jpg", path: "/p/f-000001.jpg" });
  assert.equal(h.capture.selfTest, "passed");
});

test("only a whole grace period of blank frames fails the self-test and stops capture", () => {
  const h = harness({ visible: false });
  h.capture.afterFrame();
  h.advance(BLANK_GRACE_MS - 1);
  h.capture.afterFrame();
  assert.equal(h.capture.selfTest, "unknown");
  h.advance(200);  // past the grace period, and past the capture interval so the grab is taken
  h.capture.afterFrame();
  assert.equal(h.capture.selfTest, "failed");
  assert.match(h.capture.lastError, /transparent/);
  const copies = h.copies;
  h.advance(1000);
  h.capture.afterFrame();
  assert.equal(h.copies, copies, "a failed capture grabs nothing more");
});

test("a failed write fails the self-test and keeps the reference unset", async () => {
  const h = harness();
  h.capture.afterFrame();
  await settle();
  h.capture.written({ name: "f-000001.jpg", error: "disk full" });
  assert.equal(h.capture.selfTest, "failed");
  assert.equal(h.capture.latestFrameRef, null);
});

test("each grab's copy, encode and write are timed on the real clock", async () => {
  let now = 0;
  const timed = [];
  const capture = new FrameCapture({
    grabber: { copy: () => { now += 3; return { visible: true }; }, encode: async () => { now += 20; return "AAAA"; } },
    send: () => { now += 5; },
    realNow: () => now,
    timing: (name, start, end) => timed.push([name, end - start]),
  });
  capture.afterFrame();
  await settle();
  now += 7;
  capture.written({ name: "f-000001.jpg", path: "/p/f-000001.jpg" });
  assert.deepEqual(timed, [[MEASURES.copy, 3], [MEASURES.encode, 20], [MEASURES.write, 12]]);
});

test("a timing hook that throws never stops capture", async () => {
  const capture = new FrameCapture({
    grabber: { copy: () => ({ visible: true }), encode: async () => "AAAA" },
    send: () => {},
    realNow: () => 0,
    timing: () => { throw new Error("no User Timing here"); },
  });
  capture.afterFrame();
  await settle();
  capture.written({ name: "f-000001.jpg", path: "/p/f-000001.jpg" });
  assert.equal(capture.selfTest, "passed");
});

test("the grabber encodes through OffscreenCanvas when the page has one, else through a canvas element", async () => {
  const drawn = [];
  const context = {
    drawImage: (...args) => drawn.push(args),
    getImageData: (x, y, w, h) => ({ data: new Uint8ClampedArray(w * h * 4).fill(255) }),
  };
  const blob = { arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer };
  const source = { width: 1280, height: 720 };

  const requested = [];
  class FakeOffscreen {
    constructor(width, height) { this.width = width; this.height = height; }
    getContext() { return context; }
    async convertToBlob(options) { requested.push(options); return blob; }
  }
  const offscreen = canvasGrabber(source, { OffscreenCanvasImpl: FakeOffscreen, documentRef: null });
  assert.deepEqual(offscreen.copy(), { visible: true });
  assert.deepEqual(drawn[0], [source, 0, 0, 640, 360]);
  assert.equal(await offscreen.encode(), "AQID");
  assert.deepEqual(requested, [{ type: "image/jpeg", quality: 0.8 }]);

  const element = { getContext: () => context, toBlob: (done, type) => { requested.push(type); done(blob); } };
  const fallback = canvasGrabber(source, { OffscreenCanvasImpl: undefined, documentRef: { createElement: () => element } });
  fallback.copy();
  assert.equal(await fallback.encode(), "AQID");
  assert.equal(requested.at(-1), "image/jpeg");

  const failing = canvasGrabber(source, { OffscreenCanvasImpl: undefined,
    documentRef: { createElement: () => ({ ...element, toBlob: (done) => done(null) }) } });
  await assert.rejects(failing.encode(), /could not be encoded/);
});
