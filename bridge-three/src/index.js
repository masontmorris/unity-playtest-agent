// Page entry point for the three.js playtest bridge.
//
//   import * as THREE from "three";
//   import { installBridge } from "@playtest/bridge-three";
//
//   const bridge = installBridge({ engineVersion: THREE.REVISION });
//   bridge?.attach({ sceneId: "level-1", scene, camera });
//
// Install it before the game starts its loop. It is inert (returns null) unless the page URL carries
// `?playtest=<relay url>`, which the relay prints when it starts.

import { installClock, VirtualClock } from "./clock.js";
import { connectPage } from "./connect.js";

export { VirtualClock, installClock } from "./clock.js";
export { BridgeSession, BRIDGE_VERSION } from "./session.js";
export { connectPage } from "./connect.js";
export { viewAngles } from "./view.js";

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
} = {}) {
  const relayUrl = url ?? relayUrlFromLocation(target);
  if (!relayUrl) return null;

  const clock = new VirtualClock({ mode: clockMode, stepFrameS });
  const uninstallClock = installClock(clock, target);
  let attached = null;
  const connection = connectPage({
    url: relayUrl,
    clock,
    view: () => attached,
    engineVersion,
    WebSocketImpl: target.WebSocket,
  });

  return {
    clock,
    ready: connection,
    /** Tell the bridge which scene and camera the game is showing. */
    attach({ sceneId, scene, camera }) {
      if (typeof sceneId !== "string" || sceneId.length === 0) throw new TypeError("attach needs a non-empty sceneId");
      attached = { sceneId, scene, camera };
    },
    detach() {
      attached = null;
    },
    async uninstall() {
      attached = null;
      uninstallClock();
      (await connection.catch(() => null))?.close();
    },
  };
}
