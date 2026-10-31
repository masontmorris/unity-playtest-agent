# The probe-samples reader: what S1 could not measure before (31 October 2026)

Issue #195. The bridge now publishes `public.probe` with every observation: a small geometry field in
the controlled entity's own frame. This is the reading session 10 lacked. It pressed a 1.1-unit side
face for 98 turns and read it as a step to jump over, because a blocked push into a riser and a blocked
push into a wall produce identical evidence — zero displacement, an unchanged camera height, and a
frame of grey geometry. The difference is arithmetic, and arithmetic was not in any picture. This run
shows it now is.

No model was called. Both readings come from the operator scripted-hold probe (#192), one 0.4 s W hold
each, with the start pose set in the scene and reverted afterwards; the game stayed at `2d630da`.

## The shape

`public.probe` carries raw facts only — no sample is labelled blocked, climbable or a wall:

- `anchor` — the entity the field is taken from (`kind`, `name`, `position`).
- `under` — the surface straight down from the anchor, with `delta_y` measured against the anchor, so
  it also says how far the entity stands above its own floor.
- `samples` — nine downward casts on a 3×3 grid (`forward` 0.6 / 1.2 / 1.8, `lateral` −0.35 / 0 / 0.35)
  with `hit`, `point`, `normal`, `delta_y` and `inside`. `delta_y` is measured against the ground under
  the anchor, so flat ground reads 0 and a riser reads its own height — directly comparable with a step
  limit.
- `blockers` — two forward casts at 0.15 and 0.9 above the anchor, 1.8 long, with `distance`, `point`,
  `normal`. Ankle height and chest height, which is what separates a step from a wall.

A reading that could not be taken is `null`, never zero, and a point inside geometry (an obstruction
taller than the cast) is `hit: true, inside: true` rather than an empty reading.

## The two readings

Both at FPSKit's 2-unit side flight: root `(18, 0, -26)`, riser 0.25, tread 2.0, climbing toward +x.
The entity holds W for 0.4 s; the grid and blockers are recorded before and after the hold.

**Low end** — pose `(16.5, 0.2, -27)`, yaw 90° (facing +x), 1.5 units of level floor before the first
riser. Net movement **1.128**, stopping at x 17.628 (the riser's face less the capsule radius):

| Grid `delta_y` (forward 0.6 / 1.2 / 1.8) | Blockers (0.15 / 0.9) |
|---|---|
| before: `0.00` `0.00` / `0.00` `0.00` / `0.25` `0.25` | 1.500 / miss |
| after: `0.25` `0.25` / `0.25` `0.25` / `0.25` `0.25` | 0.372 / miss |

**Side face** — pose `(27, 0.2, -25.2)`, yaw 180° (facing −z), the north face of the same flight, which
session 10 pressed for 98 turns. The capsule slides 0.320 into contact and stops:

| Grid `delta_y` (forward 0.6 / 1.2 / 1.8) | Blockers (0.15 / 0.9) |
|---|---|
| before: `0.00` `0.00` / `1.25` `1.25` / `1.25` `1.25` | 0.800 / 0.800 |
| after: `1.25` `1.25` / `1.25` `1.25` / `1.25` `1.25` | 0.480 / 0.480 |

(`under.delta_y` is −0.08 in both: the capsule rests 0.08 above the floor, which is its skin width, and
the grid is measured against that floor, not the anchor.)

## Reading

The two cases are distinguishable at a glance, and neither reading existed in session 10:

- **A 0.25 step.** One grid row at 0.25 against a flat 0.00, and the ankle-height cast blocked while the
  chest-height cast is clear — a low obstruction with room above it. With the player's step limit at
  0.08, 0.25 is a step it should be able to take, and cannot: that is `b04` visible from inside the game.
- **A 1.25 wall.** Every grid row at 1.25 and both casts blocked at the same distance — an obstruction
  taller than a step with no clearance above it. Backing off and going round is the only correct
  response, and the opposite of what the frontier kept directing.

That is the whole difference between "jump higher" (seven times, at a face it could never climb) and
"this is a wall, walk round to its low end".

**What this does not say.** It is one game, one flight and two poses. The probe measures geometry, not
capability: it does not know the entity's step limit, so the comparison that identifies `b04` is still
made by the agent (or a fact line) reading 0.25 next to 0.08. An obstruction taller than the 6-unit cast
reports `inside: true` with no height, which is honest but less useful than a number. No frames are
captured by the scripted probe, so this is geometry evidence, not visual evidence. And a reader that
publishes useful facts is not a run: whether S1 *uses* them is unmeasured until a session is approved,
which needs a fresh paid OK.

## Method

- `bridge/Runtime/Unity/UnityProbeField.cs` — the reader. Anchor: `UPA_PROBE_ANCHOR` as a hierarchy
  path, else the first enabled `CharacterController`, else the first enabled `Rigidbody`, else the
  reader is omitted with a reason. Sampling is rate-limited to 0.1 s.
- `UPA_PROBE_ANCHOR=FirstPersonController` for both runs, so the anchor is named rather than discovered.
- Launcher: `runs/b04-hunt/run-probe-samples.ps1 -Name low-end|side-face`; hold script
  `runs/b04-hunt/probe-samples/plan.json`; pose set by `runs/b04-hunt/probe-samples/set-pose.py`.
- Scene edits (start transform) reverted; `git status` in the game shows only the three pre-existing
  package/time-settings files.

## Sources

Local, not committed, under `runs/b04-hunt/probe-samples/`: `low-end/probe.json`,
`side-face/probe.json`, both `playmode.log`s, `plan.json`, `set-pose.py`. Public: issue #195, and the
reader's contract in `bridge/README.md`.
