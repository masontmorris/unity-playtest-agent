// Frame capture: JPEG frames of the game canvas, written by the relay as the Unity bridge writes
// them (`<project>/Temp/playtest-bridge-frames/f-NNNNNN.jpg`, a rolling window, 640 px wide, 10 Hz).
//
// A frame is grabbed right after a game frame's callbacks, in the same task, so the canvas still
// holds what the game rendered. `frame_ref` is the path of the newest frame the relay reported
// written; a frame the relay has not confirmed is never referenced. The self-test passes once the
// first frame is written and its pixels read back as something other than fully transparent, the
// result a blank, unreadable canvas gives.

export const CAPTURE_WIDTH = 640;
export const CAPTURE_RATE_HZ = 10;
const JPEG_QUALITY = 0.8;

/** Grab frames from an HTML canvas: a synchronous copy, then an asynchronous JPEG encode. */
export function canvasGrabber(source, { width = CAPTURE_WIDTH, documentRef = globalThis.document } = {}) {
  const scratch = documentRef.createElement("canvas");
  const context = scratch.getContext("2d", { willReadFrequently: true });
  return {
    copy() {
      const height = Math.max(2, Math.round((width * source.height) / Math.max(1, source.width)));
      scratch.width = width;
      scratch.height = height;
      context.drawImage(source, 0, 0, width, height);
      const sample = context.getImageData(0, 0, width, height).data;
      let visible = false;
      for (let i = 3; i < sample.length; i += 4 * 97) {
        if (sample[i] !== 0) { visible = true; break; }
      }
      return { visible };
    },
    encode() {
      return new Promise((resolve, reject) => {
        scratch.toBlob(async (blob) => {
          if (blob === null) return reject(new Error("the canvas could not be encoded as JPEG"));
          const bytes = new Uint8Array(await blob.arrayBuffer());
          let binary = "";
          for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
          resolve(globalThis.btoa(binary));
        }, "image/jpeg", JPEG_QUALITY);
      });
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
  #latest = null;
  #lastError = null;

  /**
   * @param {object} options
   * @param {{copy(): {visible: boolean}, encode(): Promise<string>}} options.grabber
   * @param {(message: object) => void} options.send Deliver a relay control message
   * @param {() => number} options.realNow Real milliseconds
   */
  constructor({ grabber, send, realNow, rateHz = CAPTURE_RATE_HZ }) {
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
    try {
      copied = this.#grabber.copy();
    } catch (error) {
      this.#fail(`frame copy failed: ${error.message}`);
      return;
    }
    if (this.#selfTest === "unknown" && !copied.visible) {
      this.#fail("the first frame read back fully transparent");
      return;
    }
    this.#busy = true;
    this.#count += 1;
    const name = `f-${String(this.#count).padStart(6, "0")}.jpg`;
    this.#grabber.encode().then((jpeg) => {
      this.#pending.set(name, true);
      this.#send({ relay: "frame", name, jpeg_base64: jpeg });
    }, (error) => {
      this.#busy = false;
      this.#fail(`frame encode failed: ${error.message}`);
    });
  }

  /** The relay's answer for one frame. */
  written({ name, path, error }) {
    if (!this.#pending.delete(name)) return;
    this.#busy = false;
    if (error) {
      this.#fail(`frame write failed: ${error}`);
      return;
    }
    this.#latest = path;
    if (this.#selfTest === "unknown") this.#selfTest = "passed";
  }

  #fail(message) {
    this.#lastError = message;
    if (this.#selfTest === "unknown") this.#selfTest = "failed";
  }
}
