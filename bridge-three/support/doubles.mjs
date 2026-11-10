// Test doubles: three.js objects reduced to the parts the bridge reads, and hand-driven timers.

/** A stand-in for a three.js object: a world matrix from a position and a yaw about +Y, plus children. */
export function objectAt({ name = "", position = [0, 0, 0], yawRad = 0, visible = true, isCamera = false, children = [] } = {}) {
  const c = Math.cos(yawRad);
  const s = Math.sin(yawRad);
  const elements = [c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, position[0], position[1], position[2], 1];
  return { name, visible, isCamera, matrixWorld: { elements }, children };
}

/** A stand-in for a three.js scene. */
export function sceneOf(children) {
  return { visible: true, children, updates: 0, updateMatrixWorld() { this.updates += 1; } };
}

/** A stand-in for a three.js camera: only `matrixWorld.elements` is read. */
export function cameraFacing(forward) {
  const elements = new Array(16).fill(0);
  elements[0] = 1;
  elements[5] = 1;
  elements[15] = 1;
  // Column 2 is the local Z axis in world space; a camera looks down local -Z.
  elements[8] = -forward[0];
  elements[9] = -forward[1];
  elements[10] = -forward[2];
  return { matrixWorld: { elements } };
}

/** Timers the test advances by hand. */
export function manualTimers() {
  const intervals = new Map();
  let next = 1;
  return {
    setInterval(callback, ms) {
      const id = next++;
      intervals.set(id, { callback, ms });
      return id;
    },
    clearInterval(id) {
      intervals.delete(id);
    },
    fire() {
      for (const { callback } of [...intervals.values()]) callback();
    },
    get active() {
      return intervals.size;
    },
  };
}
