// Page entry point for the three.js playtest bridge.
//
//   import * as THREE from "three";
//   import { installBridge } from "@playtest/bridge-three";
//
//   const bridge = installBridge({ engineVersion: THREE.REVISION });
//   bridge?.attach({ sceneId: "level-1", scene, camera, canvas: renderer.domElement });
//
// Install it before the game starts its loop. It is inert (returns null) unless the page URL carries
// `?playtest=<relay url>`, which the relay prints when it starts.

import { CAPTURE_RATE_HZ, canvasGrabber } from "./capture.js";
import { installClock, VirtualClock } from "./clock.js";
import { connectPage } from "./connect.js";
import { ErrorMonitor, watchPage } from "./errors.js";

export { VirtualClock, installClock } from "./clock.js";
export { BridgeSession, BRIDGE_VERSION } from "./session.js";
export { connectPage } from "./connect.js";
export { viewAngles } from "./view.js";
export { inspectTransforms } from "./inspect.js";
export { ErrorMonitor, watchPage } from "./errors.js";
export { FrameCapture, canvasGrabber } from "./capture.js";

function relayUrlFromLocation(target) {
  const search = target.location?.search;
  return search ? new URLSearchParams(search).get("playtest") : null;
}

export function installBridge({
  url = undefined,
  engineVersion = null,
  clockMode = "realtime",
  stepFrameS = 1 / 60,
  target = globalThis,
  inputTargets = {},
  captureRateHz = undefined,
} = {}) {
  const relayUrl = url ?? relayUrlFromLocation(target);
  if (!relayUrl) return null;
  // Frames per second to capture: the option, else ?playtest_capture_hz=, else the default. 0 turns capture off.
  const fromUrl = target.location?.search ? new URLSearchParams(target.location.search).get("playtest_capture_hz") : null;
  const rateHz = captureRateHz ?? (fromUrl !== null ? Number(fromUrl) : CAPTURE_RATE_HZ);
  if (!Number.isFinite(rateHz) || rateHz < 0) throw new TypeError("captureRateHz must be a finite number of frames per second, 0 or more");

  const realPerformance = target.performance;
  const realNow = realPerformance.now.bind(realPerformance);
  // Capture steps appear as User Timing measures on the real timeline (`realNow` is that timeline's clock).
  const timing = typeof realPerformance.measure === "function"
    ? (name, start, end) => realPerformance.measure(name, { start, end })
    : null;
  const errors = new ErrorMonitor();
  const unwatch = watchPage(errors, target);
  const clock = new VirtualClock({
    mode: clockMode, stepFrameS,
    onError: (error) => {
      errors.record("Exception", error);
      target.console?.warn?.(error);
    },
  });
  const uninstallClock = installClock(clock, target);
  let attached = null;
  let grabber = null;
  const connection = connectPage({
    url: relayUrl,
    clock,
    view: () => attached,
    engineVersion,
    WebSocketImpl: target.WebSocket,
    grabber: () => grabber,
    errors,
    realNow,
    target,
    inputTargets,
    captureRateHz: rateHz,
    timing,
  });

  return {
    clock,
    ready: connection,
    /** Tell the bridge which scene and camera the game is showing, and the canvas to capture. */
    attach({ sceneId, scene, camera, canvas = null, readers = {} }) {
      if (typeof sceneId !== "string" || sceneId.length === 0) throw new TypeError("attach needs a non-empty sceneId");
      if (readers === null || typeof readers !== "object" || Object.values(readers).some(read => typeof read !== "function")) {
        throw new TypeError("attach readers must map each alias to a function");
      }
      attached = { sceneId, scene, camera, readers };
      if (canvas !== null && grabber === null && rateHz > 0) grabber = canvasGrabber(canvas, { WorkerImpl: target.Worker, snapshot: (image, options) => target.createImageBitmap(image, options) });
    },
    detach() {
      attached = null;
    },
    async uninstall() {
      attached = null;
      uninstallClock();
      unwatch();
      (await connection.catch(() => null))?.close();
    },
  };
}
