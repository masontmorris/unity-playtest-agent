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

import { canvasGrabber } from "./capture.js";
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
} = {}) {
  const relayUrl = url ?? relayUrlFromLocation(target);
  if (!relayUrl) return null;

  const realPerformance = target.performance;
  const realNow = realPerformance.now.bind(realPerformance);
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
  });

  return {
    clock,
    ready: connection,
    /** Tell the bridge which scene and camera the game is showing, and the canvas to capture. */
    attach({ sceneId, scene, camera, canvas = null }) {
      if (typeof sceneId !== "string" || sceneId.length === 0) throw new TypeError("attach needs a non-empty sceneId");
      attached = { sceneId, scene, camera };
      if (canvas !== null && grabber === null) grabber = canvasGrabber(canvas, { documentRef: target.document });
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
