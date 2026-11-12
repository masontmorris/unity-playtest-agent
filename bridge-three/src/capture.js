// Frame capture: JPEG frames of the game canvas, written by the relay as the Unity bridge writes
// them (`<project>/Temp/playtest-bridge-frames/f-NNNNNN.jpg`, a rolling window, 640 px wide, 10 Hz).
//
// A frame is grabbed right after a game frame's callbacks, in the same task, so the canvas still
// holds what the game rendered. `frame_ref` is the path of the newest frame the relay reported
// written; a frame the relay has not confirmed is never referenced. The self-test passes once the
// first frame is written and its pixels read back as something other than fully transparent, the
// result a blank, unreadable canvas gives.
//
// Each grab's copy, encode and write are timed through the `timing` hook. `installBridge` records them as
// User Timing measures (`MEASURES`), which DevTools shows and a test driver can read, so a slow page can be
// traced to capture or ruled out.

export const CAPTURE_WIDTH = 640;
export const CAPTURE_RATE_HZ = 10;
export const MEASURES = { copy: "playtest-capture-copy", encode: "playtest-capture-encode", write: "playtest-capture-write" };
// A game can attach before it first draws (while it loads, or when it skips a menu), so blank frames are
// skipped until one is visible. Only this long of nothing but blank frames fails the self-test.
export const BLANK_GRACE_MS = 60_000;
const JPEG_QUALITY = 0.8;

/**
 * Grab frames from an HTML canvas without blocking the page: a snapshot on the main thread, then the
 * readback and JPEG encode in a dedicated worker.
 *
 * `createImageBitmap` takes the snapshot when it is called, so the frame is the one the game just drew,
 * and it neither reads pixels back nor waits for the GPU on the main thread. The worker draws it, samples
 * it for visibility when asked (until the self-test passes), and encodes it with `convertToBlob`.
 *
 * Measured on Kinema (WebGPU, RTX 3060, 1280x720, 10 Hz, three fresh pages held 5.5 min): the snapshot
 * costs the main thread 0.1 ms a frame, and a frame reaches the relay 35 to 40 ms after it is grabbed.
 * The synchronous copy and `toDataURL` this replaced cost 13 to 20 ms a frame in steady play, and while
 * three.js was still compiling pipelines its GPU readback made single tasks of 19 to 49 s. The
 * asynchronous encoders are no remedy on the main thread: Chrome defers them until the page is idle,
 * which a game page never is (1.1 to 1.6 s a frame). A worker is idle between frames, so it encodes at once.
 */
export function canvasGrabber(source, {
  width = CAPTURE_WIDTH,
  WorkerImpl = globalThis.Worker,
  snapshot = (image, options) => globalThis.createImageBitmap(image, options),
} = {}) {
  const worker = new WorkerImpl(new URL("./capture-worker.js", import.meta.url), { type: "module" });
  let waiting = null;
  const settle = (outcome) => {
    const answer = waiting;
    waiting = null;
    answer?.(outcome);
  };
  worker.onmessage = ({ data }) => settle(data);
  worker.onerror = (event) => settle({ error: event.message || "the capture worker failed" });
  return {
    grab({ checkVisible = true } = {}) {
      const height = Math.max(2, Math.round((width * source.height) / Math.max(1, source.width)));
      return snapshot(source, { resizeWidth: width, resizeHeight: height }).then((image) => new Promise((resolve, reject) => {
        waiting = (data) => (data.error ? reject(new Error(data.error)) : resolve({ visible: data.visible, jpeg: data.jpeg }));
        worker.postMessage({ image, width, height, checkVisible, quality: JPEG_QUALITY }, [image]);
      }));
    },
  };
}

export class FrameCapture {
  #grabber;
  #send;
  #realNow;
  #intervalMs;
  #next = 0;
  #count = 0;
  #busy = false;
  #pending = new Map();
  #selfTest = "unknown";
  #firstGrabMs = null;
  #latest = null;
  #lastError = null;
  #timing;
  #sentAt = new Map();

  /**
   * @param {object} options
   * @param {{grab(options?: {checkVisible?: boolean}): Promise<{visible: boolean | null, jpeg: string}>}} options.grabber
   *   Snapshots the canvas when called and resolves once the frame is encoded
   * @param {(message: object) => void} options.send Deliver a relay control message
   * @param {() => number} options.realNow Real milliseconds
   * @param {((name: string, startMs: number, endMs: number) => void) | null} [options.timing] Records how
   *   long each step of a grab took, in `realNow` milliseconds; null times nothing
   */
  constructor({ grabber, send, realNow, rateHz = CAPTURE_RATE_HZ, timing = null }) {
    this.#timing = timing;
    this.#grabber = grabber;
    this.#send = send;
    this.#realNow = realNow;
    this.#intervalMs = 1000 / rateHz;
  }

  get selfTest() { return this.#selfTest; }
  get latestFrameRef() { return this.#latest; }
  get lastError() { return this.#lastError; }

  /** Call after each game frame. Grabs at most one frame per interval and one at a time. */
  afterFrame() {
    const now = this.#realNow();
    if (this.#busy || now < this.#next || this.#selfTest === "failed") return;
    this.#next = now + this.#intervalMs;
    const checkVisible = this.#selfTest === "unknown";
    let grabbed;
    try {
      grabbed = this.#grabber.grab({ checkVisible });
    } catch (error) {
      this.#fail(`frame copy failed: ${error.message}`);
      return;
    }
    this.#measure(MEASURES.copy, now);
    this.#busy = true;
    const encodeStart = this.#realNow();
    grabbed.then(({ visible, jpeg }) => {
      this.#measure(MEASURES.encode, encodeStart);
      if (checkVisible && !visible) {
        // Not drawn yet: skip it, and fail only once the game has stayed blank for the whole grace period.
        this.#busy = false;
        this.#firstGrabMs ??= now;
        if (now - this.#firstGrabMs >= BLANK_GRACE_MS) this.#fail(`every frame read back fully transparent for ${BLANK_GRACE_MS / 1000} s`);
        return;
      }
      this.#count += 1;
      const name = `f-${String(this.#count).padStart(6, "0")}.jpg`;
      this.#pending.set(name, true);
      this.#sentAt.set(name, this.#realNow());
      this.#send({ relay: "frame", name, jpeg_base64: jpeg });
    }, (error) => {
      this.#busy = false;
      this.#fail(`frame encode failed: ${error.message}`);
    });
  }

  /** The relay's answer for one frame. */
  written({ name, path, error }) {
    if (!this.#pending.delete(name)) return;
    this.#measure(MEASURES.write, this.#sentAt.get(name));
    this.#sentAt.delete(name);
    this.#busy = false;
    if (error) {
      this.#fail(`frame write failed: ${error}`);
      return;
    }
    this.#latest = path;
    if (this.#selfTest === "unknown") this.#selfTest = "passed";
  }

  #measure(name, start) {
    if (this.#timing === null || start === undefined) return;
    try {
      this.#timing(name, start, this.#realNow());
    } catch {
      // Timing is diagnostic only; capture goes on without it.
    }
  }

  #fail(message) {
    this.#lastError = message;
    if (this.#selfTest === "unknown") this.#selfTest = "failed";
  }
}
