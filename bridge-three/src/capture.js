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
 * Grab frames from an HTML canvas: a copy, then a JPEG encode, both synchronous.
 *
 * The encode is `toDataURL`, 1.5 to 3 ms for a 640 px frame. Chrome defers the asynchronous encoders
 * (`toBlob`, `OffscreenCanvas.convertToBlob`) until the page is idle, up to about 1 s, and a game page is
 * never idle: on Kinema they took 1.1 to 1.6 s per frame, which held capture under 1 frame per second.
 *
 * The scratch canvas stays GPU-backed (no `willReadFrequently`), so the scale happens on the GPU and only the
 * 640 px frame is read back. Pixels are sampled for visibility only when asked, which is until the self-test passes.
 */
export function canvasGrabber(source, { width = CAPTURE_WIDTH, documentRef = globalThis.document } = {}) {
  const scratch = documentRef.createElement("canvas");
  const context = scratch.getContext("2d");
  return {
    copy({ checkVisible = true } = {}) {
      const height = Math.max(2, Math.round((width * source.height) / Math.max(1, source.width)));
      scratch.width = width;
      scratch.height = height;
      context.drawImage(source, 0, 0, width, height);
      if (!checkVisible) return { visible: null };
      const sample = context.getImageData(0, 0, width, height).data;
      let visible = false;
      for (let i = 3; i < sample.length; i += 4 * 97) {
        if (sample[i] !== 0) { visible = true; break; }
      }
      return { visible };
    },
    async encode() {
      const url = scratch.toDataURL("image/jpeg", JPEG_QUALITY);
      // A browser that cannot encode JPEG answers with another type, or "data:," for an empty canvas.
      if (!url.startsWith("data:image/jpeg;base64,")) throw new Error("the canvas could not be encoded as JPEG");
      return url.slice("data:image/jpeg;base64,".length);
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
   * @param {{copy(options?: {checkVisible?: boolean}): {visible: boolean | null}, encode(): Promise<string>}} options.grabber
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

  /** Call after each game frame. Grabs at most one frame per interval and one encode at a time. */
  afterFrame() {
    const now = this.#realNow();
    if (this.#busy || now < this.#next || this.#selfTest === "failed") return;
    this.#next = now + this.#intervalMs;
    let copied;
    const copyStart = now;
    try {
      copied = this.#grabber.copy({ checkVisible: this.#selfTest === "unknown" });
    } catch (error) {
      this.#fail(`frame copy failed: ${error.message}`);
      return;
    }
    this.#measure(MEASURES.copy, copyStart);
    if (this.#selfTest === "unknown" && !copied.visible) {
      // Not drawn yet: skip it, and fail only once the game has stayed blank for the whole grace period.
      this.#firstGrabMs ??= now;
      if (now - this.#firstGrabMs >= BLANK_GRACE_MS) this.#fail(`every frame read back fully transparent for ${BLANK_GRACE_MS / 1000} s`);
      return;
    }
    this.#busy = true;
    this.#count += 1;
    const name = `f-${String(this.#count).padStart(6, "0")}.jpg`;
    const encodeStart = this.#realNow();
    this.#grabber.encode().then((jpeg) => {
      this.#measure(MEASURES.encode, encodeStart);
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
