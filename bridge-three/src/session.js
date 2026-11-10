// One bridge session with the playtest core, independent of transport.
//
// The relay (or, later, a native host) hands this class lines from the core and carries its replies
// back. It answers hello/status, observe (with frames when capture passed its self-test), act from
// the pinned catalog, clock, stop and the `transforms` inspection. `place` is recognised and refused,
// and the other inspection stages have no source. Unmeasured facts are null, never zero or success.

import { validateCatalog } from "./catalog.js";
import { inspectTransforms } from "./inspect.js";
import { DomInputSurface } from "./input-surface.js";
import { errorMessage, envelope, ProtocolError, readRequest } from "./protocol.js";
import { viewAngles } from "./view.js";

export const BRIDGE_VERSION = "0.1.0";
const DEFAULT_RATE_HZ = 20;
const wallS = () => Date.now() / 1000;

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
  #surface;
  #catalog;
  #catalogError = null;

  #endpoint = null;
  #session = null;
  #seq = 0;
  #stream = null;
  #withFrame = false;
  #observations = new Map();
  #active = null;

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
                realNow = () => null, catalog = null, catalogSha256 = null, target = globalThis, inputTargets = {},
                inputSurface = null,
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
    this.#surface = inputSurface ?? new DomInputSurface({ clock, ...inputTargets, target: inputTargets.target ?? target });
    try {
      if (catalog !== null && !/^[a-f0-9]{64}$/i.test(catalogSha256 ?? "")) throw new TypeError("catalog SHA-256 pin must be 64 hexadecimal digits");
      this.#catalog = catalog === null ? { actions: {} } : validateCatalog(catalog);
    }
    catch (error) { this.#catalogError = error.message; this.#catalog = { actions: {} }; }
  }

  /** The transport's endpoint facts: the token the core must present, and the lock owner. */
  advertise({ token, pid, port }) {
    this.#endpoint = { token, pid, port };
  }

  /** The core's connection went away: stop streaming and unbind. */
  disconnected() {
    this.#stopStream();
    this.#cancelActive("cancelled");
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
        void this.#stop(body.reason);
        return;
      case "act":
        void this.#act(body);
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
      input_backends: this.#surface.available ? ["dom"] : [],
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
    const observation = {
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
    this.#observations.set(observation.seq, { wallMs: Date.now(), playing: attached !== null, public: observation.public });
    while (this.#observations.size > 128) this.#observations.delete(this.#observations.keys().next().value);
    return observation;
  }

  #reject(request, reason, against = null) {
    this.#send({ ...this.#envelope(), type: "execution", action_id: request.action_id, status: "rejected",
      validated_against_seq: against, reject_reason: reason, input_start_game_s: null, input_end_game_s: null,
      input_start_wall_s: null, input_end_wall_s: null, applied_duration_s: null, release_verified: null,
      applied_inputs: [], expected_effect: [], observed_effect: [] });
  }

  async #act(request) {
    if (this.#catalogError) { this.#reject(request, "invalid_params"); return; }
    const declaration = this.#catalog.actions[request.action];
    if (!declaration) { this.#reject(request, "unknown_action"); return; }
    const based = this.#observations.get(request.based_on_seq);
    if (!based) { this.#reject(request, "stale_age"); return; }
    const ageS = (Date.now() - based.wallMs) / 1000;
    if (ageS > request.max_age_s) { this.#reject(request, "stale_age", request.based_on_seq); return; }
    if (!based.playing || !this.#playing()) { this.#reject(request, "not_playing", request.based_on_seq); return; }
    const latest = [...this.#observations.values()].at(-1) ?? based;
    const resolve = (object, path) => {
      let value = object;
      for (const key of path.split(".")) {
        if (!value || typeof value !== "object" || !(key in value)) return { found: false };
        value = value[key];
      }
      return { found: true, value };
    };
    if (request.depends_on.some(path => {
      const before = resolve(based.public, path), after = resolve(latest.public, path);
      return !before.found || !after.found || JSON.stringify(before.value) !== JSON.stringify(after.value);
    })) {
      this.#reject(request, "stale_fields", request.based_on_seq); return;
    }
    if (Object.keys(request.params).length) { this.#reject(request, "invalid_params", request.based_on_seq); return; }
    const isSkill = Array.isArray(declaration.steps);
    let steps;
    if (isSkill) {
      if (request.lease_s != null && request.lease_s !== declaration.max_s) { this.#reject(request, "invalid_params", request.based_on_seq); return; }
      steps = declaration.steps;
    } else {
      const hold = request.lease_s ?? declaration.default_lease_s;
      if (!(hold > 0) || hold > 3600) { this.#reject(request, "invalid_params", request.based_on_seq); return; }
      steps = [{ inputs: declaration.inputs, hold_s: hold, wait_s: 0 }];
    }
    if (steps.some(step => step.inputs.some(input => !this.#surface.supports(input)))) {
      this.#reject(request, "invalid_params", request.based_on_seq); return;
    }
    if (this.#active) this.#cancelActive("cancelled");
    const run = { request, declaration, steps, isSkill, bindings: [], receipts: [], cancelled: false, timer: null, wake: null,
      deadlineTimer: null, startWall: null, startGame: null, terminal: false };
    this.#active = run;
    if (isSkill) run.deadlineTimer = setTimeout(() => {
      run.cancelled = true;
      run.wake?.(false);
      this.#finish(run, "timed_out");
    }, declaration.max_s * 1000);
    void this.#run(run);
  }

  async #run(run) {
    let status = "completed";
    try {
      for (let index = 0; index < run.steps.length; index += 1) {
        const step = run.steps[index];
        for (const input of step.inputs) {
          run.bindings.push(input);
          const delta = this.#surface.apply(input);
          run.receipts.push({ step_index: index, device: input.device, control: input.control, x: input.x ?? null, y: input.y ?? null,
            input_start_wall_s: null, input_end_wall_s: null, applied_duration_s: null,
            applied_delta_x: delta.applied_delta_x, applied_delta_y: delta.applied_delta_y, release_verified: null, _start: null });
        }
        const observed = await this.#surface.observeFrame();
        if (run.cancelled) { status = "cancelled"; break; }
        const ack = wallS();
        if (!observed) { status = "failed"; break; }
        run.startWall ??= ack; run.startGame ??= this.#clock.gameS;
        for (const receipt of run.receipts.filter(item => item.step_index === index)) {
          receipt.input_start_wall_s = ack; receipt._start = ack;
          if (receipt.device === "Mouse" && receipt.control === "delta") {
            receipt.input_end_wall_s = ack; receipt.release_verified = true;
          }
        }
        if (this.#active === run && !run.runningSent) {
          run.runningSent = true;
          this.#execution(run, "running", null);
        }
        if (await this.#wait(step.hold_s, run) === false) { status = "cancelled"; break; }
        for (const input of step.inputs) {
          const receipt = run.receipts.findLast(item => item.step_index === index && item.device === input.device && item.control === input.control);
          const verified = this.#surface.release(input);
          const end = wallS();
          if (receipt && (input.device !== "Mouse" || input.control !== "delta")) {
            receipt.input_end_wall_s = end; receipt.applied_duration_s = Math.max(0, end - receipt._start); receipt.release_verified = verified;
          }
        }
        if (step.wait_s > 0 && await this.#wait(step.wait_s, run) === false) { status = "cancelled"; break; }
      }
      if (run.cancelled) status = "cancelled";
      else if (!run.runningSent) status = "failed";
    } catch { status = run.cancelled ? "cancelled" : "failed"; }
    if (!run.terminal) this.#finish(run, status);
  }

  #wait(seconds, run) {
    return new Promise(resolve => {
      run.wake = resolve;
      run.timer = setTimeout(() => { run.timer = null; run.wake = null; resolve(true); }, seconds * 1000);
    });
  }

  #cancelActive(status) {
    const run = this.#active;
    if (!run) return Promise.resolve();
    run.cancelled = true;
    clearTimeout(run.timer);
    run.wake?.(false);
    this.#finish(run, status);
    return Promise.resolve();
  }

  #finish(run, status) {
    if (run.terminal) return;
    run.terminal = true;
    clearTimeout(run.timer);
    clearTimeout(run.deadlineTimer);
    for (const input of run.bindings) {
      const receipt = run.receipts.findLast(item => item.device === input.device && item.control === input.control && item.release_verified == null);
      if (receipt) {
        const verified = this.#surface.release(input);
        const end = wallS();
        if (input.device !== "Mouse" || input.control !== "delta") {
          receipt.input_end_wall_s = end;
          if (receipt._start != null) receipt.applied_duration_s = Math.max(0, end - receipt._start);
        } else receipt.input_end_wall_s = end;
        receipt.release_verified = verified;
      }
    }
    for (const receipt of run.receipts) delete receipt._start;
    const starts = run.receipts.map(x => x.input_start_wall_s).filter(Number.isFinite);
    const ends = run.receipts.map(x => x.input_end_wall_s).filter(Number.isFinite);
    const start = run.startWall ?? (starts.length ? Math.min(...starts) : null);
    const end = start == null ? null : Math.max(wallS(), ...(ends.length ? ends : [start]));
    const verified = run.receipts.length ? run.receipts.every(x => x.release_verified === true) : null;
    const record = { action_id: run.request.action_id, status: verified === false && status === "completed" ? "failed" : status,
      validated_against_seq: run.request.based_on_seq, reject_reason: null,
      input_start_game_s: run.startGame, input_end_game_s: run.startGame == null ? null : this.#clock.gameS,
      input_start_wall_s: start, input_end_wall_s: end,
      applied_duration_s: start == null || end == null ? null : Math.max(0, end - start), release_verified: verified,
      applied_inputs: run.receipts, expected_effect: [], observed_effect: [] };
    this.#execution(run, record.status, record);
    if (this.#active === run) this.#active = null;
  }

  #execution(run, status, fields) {
    const base = fields ?? { action_id: run.request.action_id, status, validated_against_seq: run.request.based_on_seq,
      reject_reason: null, input_start_game_s: run.startGame, input_end_game_s: null, input_start_wall_s: run.startWall,
      input_end_wall_s: null, applied_duration_s: null, release_verified: null, applied_inputs: run.receipts,
      expected_effect: [], observed_effect: [] };
    const safe = { ...base, applied_inputs: (base.applied_inputs ?? []).map(({ _start, ...receipt }) => receipt) };
    this.#send({ ...this.#envelope(), type: "execution", ...safe });
  }

  async #stop(reason) {
    const active = this.#active;
    const released = this.#surface.releasedNames();
    if (active) await this.#cancelActive("cancelled");
    const verified = this.#surface.releaseAll();
    this.#send({ ...this.#envelope(), type: "stopped", released: [...new Set(released)], release_verified: verified });
    if (reason !== "release") { this.disconnected(); this.#end(); }
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
