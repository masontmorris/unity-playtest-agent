# Executable input actions, chords and bounded skills

This change makes one named act execute exactly its declared keyboard, mouse or gamepad action
(one to three simultaneous inputs) or bounded skill (one to eight ordered hold/wait steps). S1
still owns the choice: code validates, binds, executes, rejects and fails closed. Nothing in this
document lets code choose, substitute, rank, suppress or repair an answer, and no threshold or
timer creates help.

## Catalog

The exported `action_catalog` contract is the strict modern shape:

```json
{"actions": {"look_right": {
  "backend": "input_system",
  "inputs": [{"device": "Mouse", "control": "delta", "x": 1.0, "y": 0.0}],
  "default_lease_s": 0.05
}}}
```

A definition is either `inputs` (1-3) with `default_lease_s`, or `steps` (1-8) with `max_s`,
never both. Each step has inputs, `hold_s > 0` and `wait_s >= 0`; the declared hold plus wait
total must fit `max_s`, and every action hold and skill bound is at most 3600 s. A duplicate
physical input, an over-size list, a missing vector value, a button carrying values, an
unsupported control path or an over-budget skill is rejected by the contract, not repaired.
`uv run playtest-export-catalog --manifest <manifest.json> --output <catalog.json>` writes only
declarations with exact execution evidence. Action names are lower_snake_case; that is a model
validator, so the C# DTO must check the same rule even though the exported JSON Schema cannot
express object-key patterns.

Supported root controls are keyboard key tokens, mouse `delta` and its buttons, and gamepad
`leftStick`, `rightStick`, `dpad`, `leftTrigger`, `rightTrigger` and named buttons. Keyboard and
device buttons require null `x` and `y`. Vector controls require both finite values in [-1, 1];
the one-dimensional trigger axes use `x` in [0, 1] with `y = 0`, and digital dpad components are
each -1, 0 or 1. Absolute pointer position, relative scroll
(until separately tested), touch, VR and remapping are rejected with an explicit reason.

Mouse delta is one relative impulse per apply, not a value repeated every update. Its `x` and `y`
are normalized displacement; the runtime applies `x * 64` and `y * 64` pixels per component. The
declared values are not normalized, clamped or direction-inferred. Gamepad values go unchanged
into device state. Calibration declares one reproducible value per discovered vector control; it
does not claim other directions, magnitudes or dead zones. Dead zone and sensitivity stay null.

## Execution receipts

`ExecutionRecord.applied_inputs` is a list of receipts (empty for a stale rejection):

| field | meaning |
|---|---|
| `step_index` | 0 for an action; the ordered step index for a skill |
| `device`, `control` | the physical device root control |
| `x`, `y` | the declared normalized values, null for buttons |
| `input_start_wall_s`, `input_end_wall_s` | device apply acknowledgement and observed release |
| `applied_duration_s` | measured apply-to-release seconds, never the configured hold |
| `applied_delta_x`, `applied_delta_y` | actual mouse pixel delta for a relative impulse |
| `release_verified` | device-state read confirmed the input is released |

`InputExecutionEvidence.applied_duration_s` is the measured total from the first apply to the
terminal completion, after verified release and any final wait. For a skill it includes the waits; it is not a sum of
configured holds.

A pointer impulse may have a null duration when its neutral-reset instant was not measured, but it
must report the applied pixel delta. Keys and held axes need a measured duration and wall-time
bounds; a pointer impulse that is part of a chord or a skill also needs measured start and end
times so its simultaneous or ordered window can be checked. Receipt `step_index` values are
ordered in the record; a rejected record cannot carry receipts.

## Evidence and support

`CalibrationRecord.execution_evidence` may carry `InputExecutionEvidence` records. Each record
stores the exact requested `declaration` (`ActionDefinition`), so support cannot be reused by a
changed hold, wait, value or `max_s`; a historical record without a declaration fails closed and
is never given a guessed one. A manifest action or skill stores at most one matching record and
exposes `execution_supported` true only when all of these hold:

1. The recorded declaration is exactly the requested one: backend, ordered inputs, `device`,
   `control`, `x`, `y`, holds, waits and `max_s`. This equality is checked before any measured
   tolerance, and the evidence label need not repeat the manifest action name.
2. The execution status is `completed` and terminal release is verified.
3. The receipts count, order, `step_index`, device, control, `x` and `y` exactly match the
   declaration.
4. Every held (keyboard/gamepad) receipt has a measured duration within the published measured
   allowance of its declared hold (at least 50 ms or 25%), and start/end times.
5. A chord's receipts acknowledge within a declared 10 ms window of each other and release
   within the same window.
6. Every pointer receipt reports both pixel deltas, and the measured delta equals the declared
   `x * 64` / `y * 64` within a declared 1e-3 pixel float-transport allowance. A zero or short
   delta is not repaired or clamped to look like the declaration.
7. Every receipt says release was verified.
8. A skill releases each step before the next apply, and each measured release-to-next-apply
   interval meets its declared `wait_s` (including the final wait before the measured terminal
   end) within a declared 10 ms timestamp allowance. Step `step_index` order is strict.
9. The measured total is at least the measured step spans plus every declared wait and no more than
   `max_s` beyond the 10 ms allowance for a skill. A skill with no measured total fails; the
   configured values are never summed and presented as a measurement.

A chord is not proven by its individual keys, and a skill is not proven by a single step. A
declaration with a failed or missing receipt keeps `execution_supported: false`; a declaration
with no evidence is preserved for a later named probe and is not exported in the executable
catalog or offered as an executable choice. Cached selections re-derive support from the current
packet; old receipts are never inherited across a changed declaration.

## Offline named-declaration probe

`python -m playtest.preflight.input_probe --project <unity-project> --plan <plan.json> --output <runs/...>` is
an offline measurement tool with zero model calls. Stage 7 remains the narrower single-button
effect-and-reload measurement; it never writes evidence. `playtest.preflight.execution_verify`
reviews stored evidence for any named action or skill without sending input. A generic PlayMode
harness can host the bridge and forward these files using `UPA_INPUT_PROBE_CATALOG`,
`UPA_INPUT_PROBE_PLAN`, `UPA_INPUT_PROBE_OUTPUT`, `UPA_INPUT_PROBE_PYTHON` and `UPA_REPO_ROOT`;
Python reads the first and last, and the CLI receives the rest. The plan is Python-defined:

```json
{"schema_version": 1,
 "actions": [{"name": "look_right", "repeats": 3},
             {"name": "dash_and_look", "repeats": 2}],
 "catalog": "catalog.json",
 "manifest": "runs/manifest.json",
 "update_manifest": "runs/manifest.json",
 "readers": [{"alias": "speed", "field_id": "..."}],
 "public_fields": ["..."], "depends_on": ["public..."],
 "max_age_s": 2.0, "settle_s": 0.15}
```

Each repeat obtains a fresh observation, binds `based_on_seq`, sends exactly the named action once
with `params: {}` and the declared lease (action hold or skill `max_s`), and records every success
and failure as k/n. Before and after each act it records transform position, measured basis,
derived yaw/pitch (documented Unity convention), public and field reader values and numeric deltas,
frame ref, SHA-256, byte count and image dimensions when the frame file is readable, all execution
receipts and a neutral-release check from the post-action device state. Missing evidence stays
null. A rejection stops that target's remaining repeats. An unverified release on any applied
input, a still-held control or an unreadable post-action state stops every remaining declaration
as well; nothing is retried or replayed. The neutral-release sample is unknown when its input
state is inactive, errors, has an unknown update type, is stale relative to the field sample, or
predates the terminal execution end; an unknown independent sample is not reported as neutral
even when the per-input device receipts separately prove release.

Execution records are retained immediately after `act` returns. If any later read fails or the
act request fails before a terminal record arrives, the attempt stays in the output as a failure
and every remaining declaration is skipped: an unobserved release is never assumed neutral.
Attempt counts are the attempts actually made, marked skipped rather than presented as invented
zero counts.

Output is written only under `<repo>/runs`, and an existing file is never overwritten. When
`update_manifest` names a run-specific manifest under runs and bridge shutdown verified release,
the probe writes the most recent successful attempt's exact declaration-bearing
`InputExecutionEvidence` into only the named declaration. A failed probe clears that declaration's
evidence and support; it does not leave an older proof in place. Earlier failures stay in the
probe output and are never cleared by promoting a later success.

## Validation and remaining scope

The C# catalog/execution consumer passes the shared fixtures. Real Unity EditMode and PlayMode
checks and a zero-model-call FPSKit probe passed; see [the measurement record](../probes/2026-10-28-input-actions.md).
The probe used fixed caller-authored declarations, not model-selected preflight or S1 turns.

The bridge emits `input_state` with `active`, `error`, `update_type`, `real_time_s`, `wall_s`,
and `devices.<Device>` set to null or `{pressed: [...], values: {...}}`. Python reads that state
and preserves the earlier keyboard `keys` alias. Missing devices and non-finite or incomplete
neutral values remain unknown. Archived frames include hashes and observation timestamps.

Nonempty `ActRequest.params` rejects before input is disturbed. A skill lease is absent or
exactly its `max_s`. The offline probe sends `params: {}` and the declared lease.
The turn loop's default action lease does not override a skill's program bound. New benchmark
choices also send empty params and use their explicit execution lease; historical frozen cases
and results are retained. A new corpus has different choice/request hashes, so a model comparison
must freeze and approve its own inputs and call budget before any new paid run.

The calibration host registers the generic keyboard/mouse/gamepad vocabulary; its Python
client probes only controls actually found in discovery. Keyboard names remain
`preflight_key_<control>`; mouse/gamepad names are `preflight_<canonical_id>`. The generic
PlayMode probe host forwards the plan path to Python. A synthesized missing gamepad is owned
and removed by the backend, and does not constitute a choice fallback.

- Interactive focus routing and physical-controller interference were not tested by the batch run.
- Relative mouse scroll, absolute pointer, touch, VR, remapping, pointer duration for some
  impulses and analog dead zone/sensitivity remain unmeasured and are reported as such.
