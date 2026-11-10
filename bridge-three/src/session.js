// One bridge session with the playtest core, independent of transport.
//
// The relay (or, later, a native host) hands this class lines from the core and carries its replies
// back. It answers hello/status, observe (with frames when capture passed its self-test), clock,
// stop and the `transforms` inspection. `act` and `place` are recognised and refused, because this
// bridge applies no input yet; the other inspection stages have no source. Unmeasured facts are null,
// never zero or success.

import { inspectTransforms } from "./inspect.js";
import { errorMessage, envelope, ProtocolError, readRequest } from "./protocol.js";
import { viewAngles } from "./view.js";

export const BRIDGE_VERSION = "0.1.0";
const DEFAULT_RATE_HZ = 20;

export class BridgeSession {
  #clock;
  #view;
  #project;
  #engineVersion;
  #send;
  #end;
  #timers;
  #capture;
  #errors;
  #realNow;

  #endpoint = null;
  #session = null;
  #seq = 0;
  #stream = null;
  #withFrame = false;

  /**
   * @param {object} options
   * @param {import("./clock.js").VirtualClock} options.clock
   * @param {() => {sceneId: string, scene: object, camera: object} | null} options.view The attached scene, or null
   * @param {string} options.project
   * @param {string | null} [options.engineVersion] three.js REVISION, or null when not supplied
   * @param {(message: object) => void} options.send Deliver one message to the core
   * @param {() => void} options.end Close the core's connection
   * @param {() => import("./capture.js").FrameCapture | null} [options.capture] The capture, or null with no canvas
   * @param {import("./errors.js").ErrorMonitor | null} [options.errors] Null when runtime errors are not watched
   * @param {() => number | null} [options.realNow] Real seconds since the bridge started, or null when unknown
   */
  constructor({ clock, view, project, engineVersion = null, send, end, capture = () => null, errors = null,
                realNow = () => null,
                timers = { setInterval: globalThis.setInterval, clearInterval: globalThis.clearInterval } }) {
    this.#clock = clock;
    this.#view = view;
    this.#project = project;
    this.#engineVersion = engineVersion;
    this.#send = send;
    this.#end = end;
    this.#timers = timers;
    this.#capture = capture;
    this.#errors = errors;
    this.#realNow = realNow;
  }

  /** The transport's endpoint facts: the token the core must present, and the lock owner. */
  advertise({ token, pid, port }) {
    this.#endpoint = { token, pid, port };
  }

  /** The core's connection went away: stop streaming and unbind. */
  disconnected() {
    this.#stopStream();
    this.#session = null;
  }

  receive(line) {
    let request;
    try {
      request = readRequest(line);
    } catch (error) {
      if (!(error instanceof ProtocolError)) throw error;
      this.#fail(error.code, error.message, error.inReplyTo, true);
      return;
    }
    const { type, body } = request;

    if (this.#session === null) {
      if (type !== "hello") {
        this.#fail("invalid", "the first message must be hello", body.seq, true);
        return;
      }
      if (this.#endpoint === null || body.token !== this.#endpoint.token) {
        this.#fail("auth", "wrong token", body.seq, true);
        return;
      }
      this.#session = body.session;
      this.#send(this.#status());
      return;
    }
    if (body.session !== this.#session) {
      this.#fail("invalid", "message for another session", body.seq, true);
      return;
    }

    switch (type) {
      case "hello":
        this.#fail("invalid", "this session already said hello", body.seq, true);
        return;
      case "observe":
        this.#observe(body);
        return;
      case "clock":
        this.#applyClock(body);
        this.#send(this.#status());
        return;
      case "stop":
        this.#send(this.#stopped());
        if (body.reason !== "release") {
          this.disconnected();
          this.#end();
        }
        return;
      case "inspect":
        this.#inspect(body);
        return;
      default:
        this.#fail("invalid", `${type} is not supported by this bridge yet`, body.seq, false);
    }
  }

  #observe(body) {
    if (body.with_frame === true && this.#capture()?.selfTest !== "passed") {
      this.#fail("invalid", "with_frame is not available: capture has no canvas or did not pass its self-test", body.seq, false);
      return;
    }
    this.#withFrame = body.with_frame === true;
    if (body.mode === "once") {
      this.#send(this.#observation());
    } else if (body.mode === "subscribe") {
      this.#stopStream();
      const rate = Math.min(body.rate_hz ?? DEFAULT_RATE_HZ, 60);
      this.#stream = this.#timers.setInterval(() => this.#send(this.#observation()), 1000 / rate);
    } else {
      this.#stopStream();
    }
  }

  #inspect(body) {
    const attached = this.#view();
    if (body.stage !== "transforms") {
      this.#fail("internal", `no ${body.stage} inspection source is attached`, body.seq, false);
      return;
    }
    if (attached === null || attached.scene == null) {
      this.#fail("internal", "no scene is attached", body.seq, false);
      return;
    }
    const reading = inspectTransforms({
      sceneId: attached.sceneId, scene: attached.scene, clock: this.#clock, realTimeS: this.#realNow(),
      runtimeErrors: this.#errors?.snapshot() ?? null,
    });
    this.#send({ ...this.#envelope(), type: "inspection", stage: body.stage, in_reply_to: body.seq, ...reading });
  }

  #applyClock(body) {
    const clock = this.#clock;
    if (body.op === "pause") clock.pause();
    else if (body.op === "resume") clock.resume();
    else if (body.op === "step") clock.step(body.step_game_s);
    else clock.setScale(body.time_scale);
  }

  #stopStream() {
    if (this.#stream !== null) {
      this.#timers.clearInterval(this.#stream);
      this.#stream = null;
    }
  }

  #playing() {
    return this.#view() !== null;
  }

  #envelope() {
    this.#seq += 1;
    return envelope(this.#session ?? "unknown", this.#seq, this.#playing() ? this.#clock.gameS : null);
  }

  #status() {
    const endpoint = this.#endpoint;
    return {
      ...this.#envelope(),
      type: "status",
      bridge_version: BRIDGE_VERSION,
      engine: "three",
      engine_version: this.#engineVersion,
      unity_version: null,
      project: this.#project,
      input_backends: [],
      clock_control: true,
      capture: this.#capture() !== null,
      capture_self_test: this.#capture()?.selfTest ?? "unknown",
      lock: { held: true, owner_pid: endpoint?.pid ?? null, port: endpoint?.port ?? null },
    };
  }

  #observation() {
    const attached = this.#view();
    const readerErrors = [];
    if (attached === null) readerErrors.push({ reader: "scene", error: "no scene is attached" });
    const view = attached === null ? null : viewAngles(attached.camera);
    if (attached !== null && view === null) readerErrors.push({ reader: "view", error: "the camera has no readable world matrix" });
    const clock = this.#clock;
    return {
      ...this.#envelope(),
      type: "observation",
      clock_mode: clock.mode,
      scene: attached?.sceneId ?? "",
      playing: attached !== null,
      paused: clock.paused,
      time_scale: clock.timeScale,
      frame: clock.frame,
      public: {},
      ui: null,
      frame_ref: this.#withFrame ? this.#capture()?.latestFrameRef ?? null : null,
      view,
      reader_errors: readerErrors,
    };
  }

  #stopped() {
    // This bridge applies no input, so there is nothing held to release.
    return { ...this.#envelope(), type: "stopped", released: [], release_verified: true };
  }

  #fail(code, message, inReplyTo, close) {
    this.#seq += 1;
    this.#send(errorMessage(this.#session ?? "unknown", this.#seq, null, code, message, inReplyTo ?? null));
    if (close) {
      this.disconnected();
      this.#end();
    }
  }
}
