import assert from "node:assert/strict";
import { test } from "node:test";

import { viewAngles } from "../src/view.js";
import { cameraFacing } from "../support/doubles.mjs";

const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`);

test("heading 0 faces -Z and turning right is positive", () => {
  const cases = [
    [[0, 0, -1], 0],
    [[1, 0, 0], 90],
    [[-1, 0, 0], -90],
    [[0, 0, 1], 180],
    [[1, 0, -1], 45],
  ];
  for (const [forward, yaw] of cases) {
    const view = viewAngles(cameraFacing(forward));
    close(view.yaw_deg, yaw);
    close(view.pitch_deg, 0);
  }
});

test("looking down is positive pitch", () => {
  const down30 = [0, -Math.sin(Math.PI / 6), -Math.cos(Math.PI / 6)];
  close(viewAngles(cameraFacing(down30)).pitch_deg, 30);
  close(viewAngles(cameraFacing([0, 1, 0])).pitch_deg, -90);
});

test("an unreadable camera is null, never a heading of zero", () => {
  assert.equal(viewAngles(null), null);
  assert.equal(viewAngles({}), null);
  assert.equal(viewAngles(cameraFacing([0, 0, 0])), null);
  assert.equal(viewAngles(cameraFacing([Number.NaN, 0, -1])), null);
});
