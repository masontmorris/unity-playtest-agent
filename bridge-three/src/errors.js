// Runtime error monitor: the page's uncaught errors, unhandled rejections, console.error calls and
// errors thrown by frame callbacks, in the same shape as the Unity bridge's monitor of error logs.
//
// `observed` counts every error since the monitor started. `events` keeps the most recent distinct
// ones, merging an exact repeat of the previous event into it, and `truncated` turns true once an
// event had to be dropped.

const LIMIT = 64;
const TEXT_LIMIT = 2000;

const trim = (value) => (typeof value === "string" && value.length > TEXT_LIMIT ? value.slice(0, TEXT_LIMIT) : value ?? null);

function describe(error) {
  if (error instanceof Error) return { message: `${error.name}: ${error.message}`, stack: error.stack ?? null };
  return { message: String(error), stack: null };
}

export class ErrorMonitor {
  #observed = 0;
  #truncated = false;
  #events = [];
  #now;

  constructor({ now = () => Date.now() / 1000 } = {}) {
    this.#now = now;
  }

  record(type, error) {
    const { message, stack } = describe(error);
    const text = trim(message);
    const trace = trim(stack);
    const now = this.#now();
    this.#observed += 1;
    const last = this.#events[this.#events.length - 1];
    if (last && last.type === type && last.message === text && last.stack === trace) {
      last.last_sequence = this.#observed;
      last.last_wall_s = now;
      last.count += 1;
      return;
    }
    if (this.#events.length >= LIMIT) {
      this.#events.shift();
      this.#truncated = true;
    }
    this.#events.push({
      type, message: text, stack: trace,
      first_sequence: this.#observed, last_sequence: this.#observed,
      first_wall_s: now, last_wall_s: now, count: 1,
    });
  }

  snapshot() {
    return {
      active: true,
      observed: this.#observed,
      limit: LIMIT,
      truncated: this.#truncated,
      events: this.#events.map((event) => ({ ...event })),
    };
  }
}

/**
 * Feed `monitor` from a page: `error` and `unhandledrejection` events and `console.error`. Returns a
 * function that detaches it.
 */
export function watchPage(monitor, target = globalThis) {
  const onError = (event) => monitor.record("Exception", event.error ?? event.message);
  const onRejection = (event) => monitor.record("Exception", event.reason);
  target.addEventListener?.("error", onError);
  target.addEventListener?.("unhandledrejection", onRejection);
  const console = target.console;
  const original = console?.error;
  if (console && typeof original === "function") {
    console.error = (...args) => {
      monitor.record("Error", args.map((arg) => (arg instanceof Error ? `${arg.name}: ${arg.message}` : String(arg))).join(" "));
      original.apply(console, args);
    };
  }
  return () => {
    target.removeEventListener?.("error", onError);
    target.removeEventListener?.("unhandledrejection", onRejection);
    if (console && typeof original === "function") console.error = original;
  };
}
