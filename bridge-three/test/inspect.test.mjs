import assert from "node:assert/strict";
import { test } from "node:test";

import { VirtualClock } from "../src/clock.js";
import { inspectTransforms } from "../src/inspect.js";
import { objectAt, sceneOf } from "../support/doubles.mjs";

const reading = (scene, limit) => inspectTransforms({
  sceneId: "level", scene, clock: new VirtualClock(), realTimeS: 1.5, runtimeErrors: null, limit,
});

const plain = (vector) => vector.map((value) => value + 0);

test("objects are listed depth first with ids from child indices and parents", () => {
  const scene = sceneOf([
    objectAt({ name: "root", children: [objectAt({ name: "child" })] }),
    objectAt({ name: "other", visible: false, children: [objectAt({ name: "hidden" })] }),
  ]);
  const { data, caps, errors } = reading(scene);
  assert.equal(scene.updates, 1, "world matrices are brought up to date first");
  assert.deepEqual(errors, []);
  assert.deepEqual(data.game_objects.map((row) => [row.id, row.parent_id, row.name]), [
    ["scene:level::0", null, "root"],
    ["scene:level::0.0", "scene:level::0", "child"],
    ["scene:level::1", null, "other"],
    ["scene:level::1.0", "scene:level::1", "hidden"],
  ]);
  const hidden = data.game_objects[3];
  assert.equal(hidden.active_self, true);
  assert.equal(hidden.active_in_hierarchy, false);
  assert.deepEqual(caps.game_objects, { limit: 4096, observed: 4, returned: 4, truncated: false });
  assert.equal(data.position_unit, "three_world_unit");
  assert.equal(data.real_time_s, 1.5);
});

test("positions and bases are reported with z negated, so a right turn is a positive yaw", () => {
  const facing = objectAt({ position: [1, 2, 3] });
  const turned = objectAt({ yawRad: -Math.PI / 2 });
  const { data } = reading(sceneOf([facing, turned]));
  const [a, b] = data.game_objects;
  assert.deepEqual(a.position, [1, 2, -3]);
  assert.deepEqual(plain(a.world_forward), [0, 0, -1]);
  assert.deepEqual(plain(a.world_right), [-1, 0, 0]);
  const yaw = (row) => Math.atan2(row.world_forward[0], row.world_forward[2]) * 180 / Math.PI;
  const turn = ((yaw(b) - yaw(a) + 540) % 360) - 180;
  assert.ok(Math.abs(turn - 90) < 1e-9, `turn was ${turn}`);
});

test("a camera's forward is its local -Z", () => {
  const { data } = reading(sceneOf([objectAt({ isCamera: true })]));
  assert.deepEqual(plain(data.game_objects[0].world_forward), [0, 0, 1]);
  assert.deepEqual(plain(data.game_objects[0].world_right), [1, 0, 0]);
});

test("a capped reading says it was truncated, and an unreadable object is unavailable, not zero", () => {
  const broken = { name: "broken", visible: true, children: [] };
  const { data, caps, errors } = reading(sceneOf([objectAt(), broken, objectAt()]), 2);
  assert.deepEqual(caps.game_objects, { limit: 2, observed: 3, returned: 2, truncated: true });
  assert.equal(data.game_objects[1].position, null);
  assert.equal(data.game_objects[1].position_state, "unavailable");
  assert.equal(data.game_objects[1].basis_state, "unavailable");
  assert.match(errors[0], /scene:level::1: position/);
});
