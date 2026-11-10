// Where a three.js camera points, in the protocol's ViewAngles convention.
//
// The protocol's convention comes from Unity: yaw is the heading and a positive change turns right;
// pitch is asin(-forward.y) and a positive change looks down. A three.js camera looks down its local
// -Z axis in a right-handed, Y-up world, so a heading of 0 faces world -Z and turning right moves
// toward +X: yaw = atan2(forward.x, -forward.z).

const DEGREES = 180 / Math.PI;

/** The camera's world yaw and pitch in degrees, or null when its world matrix is not readable. */
export function viewAngles(camera) {
  const e = camera?.matrixWorld?.elements;
  if (!e || e.length !== 16) return null;
  // matrixWorld is column-major; elements 8..10 are the local Z axis in world space.
  let x = -e[8];
  let y = -e[9];
  let z = -e[10];
  const length = Math.hypot(x, y, z);
  if (!(length > 0) || !Number.isFinite(length)) return null;
  x /= length;
  y /= length;
  z /= length;
  const yaw = Math.atan2(x, -z) * DEGREES;
  const pitch = Math.asin(Math.min(1, Math.max(-1, -y))) * DEGREES;
  return { yaw_deg: yaw + 0, pitch_deg: pitch + 0 };
}
