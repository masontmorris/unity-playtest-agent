# FPSKit b04 reproduced by a scripted push (31 October 2026)

Issue #192 (tool), following #190 (session 10). No model was called: the probe sends six 0.4-second W holds, the
same input a session's `move_forward` presses, from a fixed starting pose. `b04` clamps the character controller's
step offset to its skin width, and the only surface that can show it is a riser taller than the clamp with a tread
the capsule can stand on. Ten sessions never reached one; a scripted push does.

## What was measured

- **The flight.** The 2-unit side staircase: root `(18, 0, -26)`, 16 units of depth, riser 0.25, so tread 2.0.
  It climbs toward +x and occupies z -28 to -26; its low end, the only face a walker can step onto, is at x = 18.
  `Assets/FPSKit/Utility/Stairs.cs` generates the mesh and the collider from the same steps.
- **The pose.** `FirstPersonController` starts at `(16.5, 0.2, -27)` with a 90° yaw, facing +x, 1.5 units of level
  floor before the first riser. It settles to y 0.08, camera y 1.78.
- **The input.** Six 0.4 s holds of W, sent one at a time through the bridge with a transform read before and
  after each. All twelve holds in both runs completed with verified release, and the bridge shutdown verified
  release too.
- **The one difference.** Run 2 deletes the clamp at `FirstPersonController.cs:186`
  (`Mathf.Min(stepOffset, skinWidth)` → `stepOffset`), so the controller's 0.3 step offset takes effect. That edit
  is a diagnostic, reverted after the run; it is not a fix.

## The two runs

`scripted-push-1` — clamp as built, step limit 0.08:

| Hold | Net displacement | Entity x | Entity y | Camera y |
|---:|---:|---:|---:|---:|
| 1 | 1.12793 | 16.5 → 17.62793 | 0.08 | 1.780 |
| 2 | 0.00000 | 17.62793 | 0.08 | 1.780 |
| 3 | 0.00000 | 17.62793 | 0.08 | 1.780 |
| 4 | 0.00000 | 17.62793 | 0.08 | 1.780 |
| 5 | 0.00000 | 17.62793 | 0.08 | 1.780 |
| 6 | 0.00000 | 17.62793 | 0.08 | 1.780 |

`scripted-push-2` — the same scene, pose and holds with the clamp removed, step limit 0.3:

| Hold | Net displacement | Entity x | Entity y | Camera y |
|---:|---:|---:|---:|---:|
| 1 | 2.13205 | 16.5 → 18.61734 | 0.08 → 0.33 | 2.030 |
| 2 | 2.02020 | → 20.62201 | → 0.58 | 2.280 |
| 3 | 2.01915 | → 22.62562 | → 0.83 | 2.530 |
| 4 | 2.02246 | → 24.63257 | → 1.08 | 2.780 |
| 5 | 2.01743 | → 26.63445 | → 1.33 | 3.030 |
| 6 | 2.01885 | → 28.63776 | → 1.58 | 3.280 |

## Reading

With the clamp, S1-style forward holds into the first 0.25 riser produce **no movement and no height**: the capsule
stops at x 17.62793, which is the riser at x 18 less the 0.4 capsule radius, and stays there for five more pushes.
Without the clamp, every hold steps exactly one 0.25 riser and travels its 2.0 tread, raising the entity 0.25 each
time. Same scene, same pose, same inputs, same bridge; the only change is the clamped line. That is `b04`
reproduced: the clamp, not the staircase geometry, is what blocks the step, and it blocks it at the first riser.

This also reconciles the two earlier observations. The main staircase is climbable because its 0.571 treads are
narrower than the 0.8-diameter capsule, which rides the nosings up a 24.3° line and never needs a step offset. The
side flights have treads the capsule can stand on, so they do need one, and with the clamp they cannot climb at all.

**What this does not say.** It is not a session: no model chose anything, the pose and the one-line control are
operator setup, and both were reverted, so the shipped scene and package are untouched. One pose on one flight was
sampled (the 2-unit flight at z -27); the 4-, 6- and 8-unit flights, and a jump-assisted approach, were not. And a
reproduction is not a fix: the clamp is still in the development build.

## Method

- **Tool.** `playtest.bench.scripted` (#192), run by the existing bench host
  (`BenchRecordingPlayModeTests`) with `UPA_BENCH_MODULE=playtest.bench.scripted` and a JSON hold script in
  `UPA_SCRIPTED_PLAN`. Each hold is bound to a fresh observation, sent once, never retried; the report records
  status, release, applied time, position and yaw before and after, net displacement, and the closing camera.
- **Setup for run 1.** `Assets/FPSKit_TestScene.unity` was edited to start the controller at `(16.5, 0.2, -27)`,
  yaw 90°, and reverted afterwards; the game's HEAD stayed `2d630da`.
- **Setup for run 2.** The same pose, plus `FirstPersonController.cs:186` replaced by `stepOffset` for the run and
  reverted afterwards.
- **Reproduction.** Reapply both local edits, then run the launcher as it stands
  (`runs/b04-hunt/run-scripted-push.ps1`); the hold script is `runs/b04-hunt/scripted-push/plan.json`.

## Sources

Local, not committed, under `runs/b04-hunt/`: `scripted-push-1/probe.json`, `scripted-push-1/playmode.log`,
`scripted-push-2/probe.json`, `scripted-push-2/playmode.log`, `scripted-push/plan.json`,
`run-scripted-push.ps1`, `run-scripted-push-2.ps1`.

Public: issue #192
