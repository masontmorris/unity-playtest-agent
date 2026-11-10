// The `transforms` inspection: every object in the attached scene, depth first, with its world
// position and orientation basis, in the shape the Unity bridge's transform collector uses.
//
// Coordinates are reported in the protocol's frame: x right, y up, and z negated from three.js, so
// the frame is left-handed like the one ViewAngles and the core's yaw formula assume. An object's
// forward is its local +Z (three.js's lookAt convention) and its right is local -X; a camera's
// forward is local -Z and its right local +X. Unreadable values are null with a stated reason.

const LIMIT = 4096;

function unit(x, y, z) {
  const length = Math.hypot(x, y, z);
  return length > 0 && Number.isFinite(length) ? [x / length, y / length, -z / length] : null;
}

function basis(object) {
  const e = object.matrixWorld?.elements;
  if (!e || e.length !== 16) return { error: "no world matrix" };
  const sign = object.isCamera ? -1 : 1;
  const forward = unit(sign * e[8], sign * e[9], sign * e[10]);
  const right = unit(sign * -e[0], sign * -e[1], sign * -e[2]);
  return forward && right ? { forward, right } : { error: "orientation basis is not finite" };
}

function position(object) {
  const e = object.matrixWorld?.elements;
  if (!e || e.length !== 16) return null;
  const p = [e[12], e[13], -e[14]];
  return p.every(Number.isFinite) ? p : null;
}

/**
 * One transforms reading of `scene`. `limit` caps the rows; a capped reading says so in its caps.
 * Returns `{ data, caps, errors }` for an inspection message.
 */
export function inspectTransforms({ sceneId, scene, clock, realTimeS, runtimeErrors, limit = LIMIT }) {
  const errors = [];
  const rows = [];
  let observed = 0;
  try {
    scene.updateMatrixWorld?.();
  } catch (error) {
    errors.push(`scene: updateMatrixWorld: ${error.message}`);
  }
  const sceneKey = `scene:${sceneId}`;

  const visit = (object, path, parentId, parentActive) => {
    observed += 1;
    const id = `${sceneKey}::${path.join(".")}`;
    const activeSelf = object.visible !== false;
    const activeInHierarchy = parentActive && activeSelf;
    if (rows.length < limit) {
      const at = position(object);
      if (at === null) errors.push(`object ${id}: position is not finite`);
      const axes = basis(object);
      rows.push({
        id,
        scene_id: sceneKey,
        scene_path: sceneId,
        parent_id: parentId,
        name: typeof object.name === "string" ? object.name : null,
        active_self: activeSelf,
        active_in_hierarchy: activeInHierarchy,
        position_state: at === null ? "unavailable" : "present",
        position: at,
        basis_state: axes.forward ? "present" : "unavailable",
        world_forward: axes.forward ?? null,
        world_right: axes.right ?? null,
        basis_error: axes.error ?? null,
      });
    }
    (object.children ?? []).forEach((child, index) => visit(child, [...path, index], id, activeInHierarchy));
  };
  (scene.children ?? []).forEach((child, index) => visit(child, [index], null, scene.visible !== false));

  return {
    data: {
      selection: "attached_scene_depth_first",
      id_policy: "scene_id_and_ordered_child_indices",
      position_space: "world",
      position_unit: "three_world_unit",
      position_order: ["x", "y", "z"],
      orientation_space: "world",
      axis_convention: "x right, y up, z negated from three.js (left-handed); forward is local +Z, a camera's local -Z",
      time_unit: "seconds",
      frame: clock.frame,
      real_time_s: realTimeS,
      game_time_s: clock.gameS,
      runtime_errors: runtimeErrors,
      game_objects: rows,
    },
    caps: { game_objects: { limit, observed, returned: rows.length, truncated: observed > rows.length } },
    errors,
  };
}

/**
 * The `fields` inspection: no game fields yet (they come with a preflight), and the delivered input
 * state, in the shape the Unity bridge's fields collector uses. `inputState` is the surface's
 * `state()`, or null when the page has no input surface.
 */
export function inspectFields({ clock, realTimeS, inputState }) {
  const wallS = Date.now() / 1000;
  const state = inputState === null
    ? { active: false, devices: null, real_time_s: realTimeS, wall_s: wallS, update_type: null,
        error: "no input surface is attached" }
    : { active: true, devices: inputState, real_time_s: realTimeS, wall_s: wallS, update_type: "dynamic", error: null };
  return {
    data: {
      time_unit: "seconds",
      frame: clock.frame,
      real_time_s: realTimeS,
      game_time_s: clock.gameS,
      sample_wall_s: wallS,
      input_state: state,
      game_objects: [],
      fields: [],
    },
    caps: {
      game_objects: { limit: LIMIT, observed: 0, returned: 0, truncated: false },
      fields: { limit: LIMIT, observed: 0, returned: 0, truncated: false },
    },
    errors: [],
  };
}
