// Test doubles: a camera whose world matrix points along a given forward vector.

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
