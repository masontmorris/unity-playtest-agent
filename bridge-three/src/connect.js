// The page's side of the relay: one WebSocket that carries upa/1 lines and relay control messages.

import { FrameCapture } from "./capture.js";
import { BridgeSession } from "./session.js";

/**
 * Connect a page to the relay at `url` and serve one BridgeSession over it. Resolves once the
 * socket is open, to `{ close() }`.
 *
 * `grabber` returns the frame grabber for the attached canvas, or null while there is none; capture
 * starts once it returns one. `realNow` is real milliseconds, never the page's virtualised clock.
 */
export function connectPage({ url, clock, view, engineVersion = null, WebSocketImpl = globalThis.WebSocket, timers,
                              grabber = () => null, errors = null, realNow = () => Date.now() }) {
  const socket = new WebSocketImpl(url);
  const send = (message) => {
    if (socket.readyState === 1) socket.send(JSON.stringify(message));
  };
  const started = realNow();
  let capture = null;
  const removeHook = clock.afterFrame(() => {
    if (capture === null) {
      const source = grabber();
      if (source === null) return;
      capture = new FrameCapture({ grabber: source, send, realNow });
    }
    if (socket.readyState === 1) capture.afterFrame();
  });
  let session = null;
  const create = (project) => new BridgeSession({
    clock, view, project, engineVersion, send, end: () => send({ relay: "end" }), timers,
    capture: () => capture, errors, realNow: () => (realNow() - started) / 1000,
  });

  socket.addEventListener("message", (event) => {
    const text = typeof event.data === "string" ? event.data : String(event.data);
    let relay = null;
    if (text.includes('"relay"')) {
      try {
        const body = JSON.parse(text);
        if (body !== null && typeof body === "object" && "relay" in body) relay = body;
      } catch {
        // Not JSON: the session reports it as a malformed line.
      }
    }
    if (relay === null) {
      session?.receive(text);
    } else if (relay.relay === "endpoint") {
      session = create(relay.project);
      session.advertise(relay);
    } else if (relay.relay === "close") {
      session?.disconnected();
    } else if (relay.relay === "frame_written") {
      capture?.written(relay);
    }
  });
  socket.addEventListener("close", () => {
    removeHook();
    session?.disconnected();
  });

  return new Promise((resolve, reject) => {
    socket.addEventListener("open", () => resolve({ close: () => socket.close() }), { once: true });
    socket.addEventListener("error", () => reject(new Error(`could not reach the playtest relay at ${url}`)), { once: true });
  });
}
