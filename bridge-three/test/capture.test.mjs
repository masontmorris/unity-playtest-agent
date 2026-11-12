import assert from "node:assert/strict";
import { test } from "node:test";

import { BLANK_GRACE_MS, FrameCapture, MEASURES, canvasGrabber } from "../src/capture.js";

// A grabber whose grabs finish only when the test says so, like the worker that encodes them.
function harness({ visible = true } = {}) {
  let now = 0;
  const sent = [];
  const grabs = [];
  const capture = new FrameCapture({
    grabber: {
      grab: ({ checkVisible }) => new Promise((resolve, reject) => {
        grabs.push({ checkVisible, resolve, reject, at: now });
      }),
    },
    send: (message) => sent.push(message),
    realNow: () => now,
  });
  const finish = async (index = grabs.length - 1) => {
    const grab = grabs[index];
    grab.resolve({ visible: grab.checkVisible ? (typeof visible === "function" ? visible() : visible) : null, jpeg: "AAAA" });
    await settle();
  };
  return { capture, sent, grabs, finish, advance(ms) { now += ms; } };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("frames are grabbed at most once per interval and referenced only once written", async () => {
  const h = harness();
  h.capture.afterFrame();
  h.capture.afterFrame();
  assert.equal(h.grabs.length, 1);
  await h.finish();
  assert.deepEqual(h.sent.map((m) => [m.name, m.jpeg_base64]), [["f-000001.jpg", "AAAA"]]);
  assert.equal(h.capture.latestFrameRef, null, "an unconfirmed frame is never referenced");
  assert.equal(h.capture.selfTest, "unknown");
  h.capture.written({ name: "f-000001.jpg", path: "/p/f-000001.jpg" });
  assert.equal(h.capture.latestFrameRef, "/p/f-000001.jpg");
  assert.equal(h.capture.selfTest, "passed");
  h.advance(50);
  h.capture.afterFrame();
  assert.equal(h.grabs.length, 1, "the next frame waits for the interval");
  h.advance(60);
  h.capture.afterFrame();
  assert.equal(h.grabs.length, 2);
  assert.deepEqual(h.grabs.map((g) => g.checkVisible), [true, false], "visibility is checked only until the self-test passes");
});

test("at most one frame is in flight, from the grab until the relay confirms it", async () => {
  const h = harness();
  h.capture.afterFrame();
  h.advance(1000);
  h.capture.afterFrame();
  assert.equal(h.grabs.length, 1, "a grab still encoding holds the next one back");
  await h.finish();
  h.advance(1000);
  h.capture.afterFrame();
  assert.equal(h.grabs.length, 1, "a frame sent but not yet written holds the next one back");
  h.capture.written({ name: "f-000001.jpg", path: "/p/f-000001.jpg" });
  h.advance(1000);
  h.capture.afterFrame();
  assert.equal(h.grabs.length, 2);
});

test("blank frames before the game draws are skipped, and a later visible frame passes", async () => {
  let drawn = false;
  const h = harness({ visible: () => drawn });
  h.capture.afterFrame();
  await h.finish();
  h.advance(5000);
  h.capture.afterFrame();
  await h.finish();
  assert.equal(h.grabs.length, 2, "a skipped blank frame frees capture for the next one");
  assert.equal(h.capture.selfTest, "unknown", "a game that has not drawn yet is not a failure");
  assert.equal(h.sent.length, 0, "blank frames are never written");
  drawn = true;
  h.advance(200);
  h.capture.afterFrame();
  await h.finish();
  assert.deepEqual(h.sent.map((m) => m.name), ["f-000001.jpg"]);
  h.capture.written({ name: "f-000001.jpg", path: "/p/f-000001.jpg" });
  assert.equal(h.capture.selfTest, "passed");
});

test("only a whole grace period of blank frames fails the self-test and stops capture", async () => {
  const h = harness({ visible: false });
  h.capture.afterFrame();
  await h.finish();
  h.advance(BLANK_GRACE_MS - 1);
  h.capture.afterFrame();
  await h.finish();
  assert.equal(h.capture.selfTest, "unknown");
  h.advance(200);  // past the grace period, and past the capture interval so the grab is taken
  h.capture.afterFrame();
  await h.finish();
  assert.equal(h.capture.selfTest, "failed");
  assert.match(h.capture.lastError, /transparent/);
  h.advance(1000);
  h.capture.afterFrame();
  assert.equal(h.grabs.length, 3, "a failed capture grabs nothing more");
});

test("a grab that throws or rejects fails the self-test and keeps the reference unset", async () => {
  const thrown = new FrameCapture({
    grabber: { grab: () => { throw new Error("no canvas"); } }, send: () => {}, realNow: () => 0,
  });
  thrown.afterFrame();
  assert.equal(thrown.selfTest, "failed");
  assert.match(thrown.lastError, /copy failed: no canvas/);

  const h = harness();
  h.capture.afterFrame();
  h.grabs[0].reject(new Error("the canvas could not be encoded as JPEG"));
  await settle();
  assert.equal(h.capture.selfTest, "failed");
  assert.match(h.capture.lastError, /encode failed/);
  assert.equal(h.sent.length, 0);
});

test("a failed write fails the self-test and keeps the reference unset", async () => {
  const h = harness();
  h.capture.afterFrame();
  await h.finish();
  h.capture.written({ name: "f-000001.jpg", error: "disk full" });
  assert.equal(h.capture.selfTest, "failed");
  assert.equal(h.capture.latestFrameRef, null);
});

test("each grab's copy, encode and write are timed on the real clock", async () => {
  let now = 0;
  const timed = [];
  const capture = new FrameCapture({
    grabber: { grab: () => { now += 3; return new Promise((resolve) => setImmediate(() => { now += 20; resolve({ visible: true, jpeg: "AAAA" }); })); } },
    send: () => { now += 5; },
    realNow: () => now,
    timing: (name, start, end) => timed.push([name, end - start]),
  });
  capture.afterFrame();
  await settle();
  await settle();
  now += 7;
  capture.written({ name: "f-000001.jpg", path: "/p/f-000001.jpg" });
  assert.deepEqual(timed, [[MEASURES.copy, 3], [MEASURES.encode, 20], [MEASURES.write, 12]]);
});

test("a timing hook that throws never stops capture", async () => {
  const capture = new FrameCapture({
    grabber: { grab: async () => ({ visible: true, jpeg: "AAAA" }) },
    send: () => {},
    realNow: () => 0,
    timing: () => { throw new Error("no User Timing here"); },
  });
  capture.afterFrame();
  await settle();
  capture.written({ name: "f-000001.jpg", path: "/p/f-000001.jpg" });
  assert.equal(capture.selfTest, "passed");
});

// A worker double: records what it was sent and answers as the test says.
function fakeWorker() {
  const created = [];
  class FakeWorker {
    constructor(url, options) {
      this.url = String(url);
      this.options = options;
      this.posted = [];
      created.push(this);
    }
    postMessage(message, transfer) { this.posted.push({ message, transfer }); }
    answer(data) { this.onmessage({ data }); }
  }
  return { FakeWorker, created };
}

test("the grabber snapshots the canvas when called and has the worker encode it", async () => {
  const { FakeWorker, created } = fakeWorker();
  const source = { width: 1280, height: 720 };
  const snapshots = [];
  const grabber = canvasGrabber(source, {
    WorkerImpl: FakeWorker,
    snapshot: (image, options) => { snapshots.push([image, options]); return Promise.resolve({ bitmap: snapshots.length }); },
  });
  const [worker] = created;
  assert.match(worker.url, /capture-worker\.js$/);
  assert.deepEqual(worker.options, { type: "module" });

  const grabbed = grabber.grab({ checkVisible: true });
  assert.deepEqual(snapshots, [[source, { resizeWidth: 640, resizeHeight: 360 }]], "the snapshot is taken before grab returns");
  await settle();
  assert.deepEqual(worker.posted, [{
    message: { image: { bitmap: 1 }, width: 640, height: 360, checkVisible: true, quality: 0.8 },
    transfer: [{ bitmap: 1 }],
  }]);
  worker.answer({ visible: true, jpeg: "AQID" });
  assert.deepEqual(await grabbed, { visible: true, jpeg: "AQID" });

  const failed = grabber.grab({ checkVisible: false });
  await settle();
  worker.answer({ error: "the canvas could not be encoded as JPEG" });
  await assert.rejects(failed, /could not be encoded/);

  const crashed = grabber.grab();
  await settle();
  worker.onerror({ message: "" });
  await assert.rejects(crashed, /capture worker failed/);
});

test("a snapshot that fails rejects the grab", async () => {
  const { FakeWorker } = fakeWorker();
  const grabber = canvasGrabber({ width: 0, height: 0 }, {
    WorkerImpl: FakeWorker,
    snapshot: () => Promise.reject(new Error("The source image width is 0.")),
  });
  await assert.rejects(grabber.grab(), /width is 0/);
});

// The worker module installs its handler on `self` when imported; run it against canvas doubles.
async function loadWorker({ alpha = 255, type = "image/jpeg" } = {}) {
  const posted = [];
  const drawn = [];
  const sampled = [];
  const encoded = [];
  globalThis.self = { postMessage: (message) => posted.push(message) };
  globalThis.OffscreenCanvas = class {
    constructor(width, height) { this.size = [width, height]; }
    getContext(kind, options) {
      return {
        drawImage: (...args) => drawn.push([this.size, kind, options, ...args]),
        getImageData: (x, y, w, h) => { sampled.push([w, h]); return { data: new Uint8ClampedArray(w * h * 4).fill(alpha) }; },
      };
    }
    async convertToBlob(options) {
      encoded.push(options);
      return { type, arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer };
    }
  };
  await import(`../src/capture-worker.js?load=${Math.random()}`);
  const handler = globalThis.self.onmessage;
  const run = async (data) => {
    let closed = false;
    const image = { close: () => { closed = true; } };
    await handler({ data: { image, width: 640, height: 360, quality: 0.8, ...data } });
    return closed;
  };
  return { run, posted, drawn, sampled, encoded };
}

test("the worker scales, checks and encodes a frame, and closes it", async () => {
  const w = await loadWorker();
  assert.equal(await w.run({ checkVisible: true }), true, "the frame is closed once used");
  assert.equal(w.drawn[0][0].join("x"), "640x360");
  assert.deepEqual(w.drawn[0].slice(4), [0, 0, 640, 360]);
  assert.deepEqual(w.sampled, [[640, 360]]);
  assert.deepEqual(w.encoded, [{ type: "image/jpeg", quality: 0.8 }]);
  assert.deepEqual(w.posted, [{ visible: true, jpeg: "AQID" }]);

  await w.run({ checkVisible: false });
  assert.equal(w.sampled.length, 1, "no pixels are read once visibility is settled");
  assert.deepEqual(w.posted[1], { visible: null, jpeg: "AQID" });

  const blank = await loadWorker({ alpha: 0 });
  await blank.run({ checkVisible: true });
  assert.deepEqual(blank.posted, [{ visible: false, jpeg: "AQID" }]);
});

test("the worker reports a frame it cannot encode as JPEG, and still closes it", async () => {
  const w = await loadWorker({ type: "image/png" });  // a browser without a JPEG encoder answers PNG
  assert.equal(await w.run({ checkVisible: false }), true);
  assert.deepEqual(w.posted, [{ error: "the canvas could not be encoded as JPEG" }]);
});
