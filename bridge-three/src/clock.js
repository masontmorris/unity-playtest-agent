// Virtual game clock for a three.js page.
//
// The bridge owns the page's notion of time: `requestAnimationFrame` callbacks and
// `performance.now()` both read this clock, so pausing it freezes every game that derives its
// frame delta from either, and stepping it advances game time by an exact amount. Timers
// (`setTimeout`, `setInterval`) and `Date.now()` are left alone; a game that reads them is not
// paused by this clock.

export const CLOCK_MODES = Object.freeze(["realtime", "stepped"]);

export class VirtualClock {
  #mode;
  #stepFrameMs;
  #paused = false;
  #scale = 1;
  #gameMs = 0;
  #frame = 0;
  #lastRealMs = null;
  #queue = new Map();
  #nextId = 1;
  #afterFrame = [];
  #onError;

  constructor({ mode = "realtime", stepFrameS = 1 / 60, onError = (error) => console.error(error) } = {}) {
    if (!CLOCK_MODES.includes(mode)) throw new RangeError(`unknown clock mode ${mode}`);
    if (!(stepFrameS > 0) || !Number.isFinite(stepFrameS)) throw new RangeError("stepFrameS must be a finite positive number");
    this.#mode = mode;
    this.#stepFrameMs = stepFrameS * 1000;
    this.#onError = onError;
  }

  get mode() { return this.#mode; }
  get paused() { return this.#paused; }
  get timeScale() { return this.#scale; }
  get frame() { return this.#frame; }
  get gameS() { return this.#gameMs / 1000; }

  /** The `performance.now()` the game sees: game milliseconds. */
  now() { return this.#gameMs; }

  requestAnimationFrame(callback) {
    if (typeof callback !== "function") throw new TypeError("requestAnimationFrame needs a function");
    const id = this.#nextId++;
    this.#queue.set(id, callback);
    return id;
  }

  cancelAnimationFrame(id) {
    this.#queue.delete(id);
  }

  /** Run `hook` after every game frame's callbacks, in the same task. Returns a function that removes it. */
  afterFrame(hook) {
    this.#afterFrame.push(hook);
    return () => { this.#afterFrame = this.#afterFrame.filter((item) => item !== hook); };
  }

  /**
   * One real display frame, at real time `realMs`. In realtime mode an unpaused clock advances by
   * the real interval times the time scale and runs one game frame; a scale of 0 still runs frames
   * that do not advance time. A paused or stepped clock runs nothing here.
   */
  tick(realMs) {
    const last = this.#lastRealMs;
    this.#lastRealMs = realMs;
    if (this.#mode !== "realtime" || this.#paused || last === null) return false;
    this.#gameMs += Math.max(0, realMs - last) * this.#scale;
    this.#runFrame();
    return true;
  }

  /** Advance exactly `gameS` seconds in frames of the step size; the last frame may be shorter. */
  step(gameS) {
    if (!(gameS > 0) || !Number.isFinite(gameS)) throw new RangeError("step needs a finite positive number of seconds");
    const targetMs = this.#gameMs + gameS * 1000;
    let frames = 0;
    while (this.#gameMs < targetMs) {
      this.#gameMs = Math.min(targetMs, this.#gameMs + this.#stepFrameMs);
      this.#runFrame();
      frames += 1;
    }
    return frames;
  }

  pause() { this.#paused = true; }
  resume() { this.#paused = false; }

  setScale(scale) {
    if (!(scale >= 0) || !Number.isFinite(scale)) throw new RangeError("time scale must be a finite number >= 0");
    this.#scale = scale;
  }

  // Callbacks requested during a frame run on the next one, as in a browser.
  #runFrame() {
    this.#frame += 1;
    const callbacks = this.#queue;
    this.#queue = new Map();
    for (const callback of callbacks.values()) {
      try {
        callback(this.#gameMs);
      } catch (error) {
        this.#onError(error);
      }
    }
    for (const hook of this.#afterFrame) {
      try {
        hook(this.#frame);
      } catch (error) {
        this.#onError(error);
      }
    }
  }
}

/**
 * Route a global object's `requestAnimationFrame`, `cancelAnimationFrame` and `performance.now`
 * through `clock`, and drive `clock.tick` from the original animation frames. Returns a function
 * that restores the originals.
 */
export function installClock(clock, target = globalThis) {
  const realRaf = target.requestAnimationFrame.bind(target);
  const realCaf = target.cancelAnimationFrame.bind(target);
  const performance = target.performance;
  const realNow = performance.now.bind(performance);
  const hadOwnNow = Object.prototype.hasOwnProperty.call(performance, "now");
  const ownNow = performance.now;

  let driver = 0;
  const drive = () => {
    clock.tick(realNow());
    driver = realRaf(drive);
  };

  target.requestAnimationFrame = (callback) => clock.requestAnimationFrame(callback);
  target.cancelAnimationFrame = (id) => clock.cancelAnimationFrame(id);
  performance.now = () => clock.now();
  driver = realRaf(drive);

  return () => {
    realCaf(driver);
    target.requestAnimationFrame = realRaf;
    target.cancelAnimationFrame = realCaf;
    if (hadOwnNow) performance.now = ownNow;
    else delete performance.now;
  };
}
