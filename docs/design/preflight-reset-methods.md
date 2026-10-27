# How preflight finds and measures save/load and checkpoint resets

Issue #90. The calibration record reports `save_load` and `checkpoint` as `null`, with the reason
"no generic save/load operation was exercised". That is true of the harness, not of the game, and it
leaves `reproduce` (#38) with one reset mechanism: the scene reload measured in
`2026-10-27-fpskit-reset.md`. This note records the decision for the other two before any code.

## Decision

Preflight does not look for methods by name. The stage 0–1 scan already enumerates the methods the
game's authored types declare in its loaded assemblies; stage 5's selection step (the manifest model,
#27) may bind at most
three of the zero-argument callables the inspection reports: `save`, `restore` and `checkpoint`.
Code validates a binding and never infers one. Stage 7 (#28) then measures each declared binding with
the same mutate-then-reset-then-compare protocol as the scene reload, and records k/n counts or an
explicit unavailable reason. A game with no such callable — which includes FPSKit — records both
methods as unavailable; it does not record them as broken.

## What a reset has to establish

Two different claims, because `reproduce` needs the second and the scene reload only supports the
first:

1. **Returns to the reference state.** After a mutation, the reset puts the sampled state back to
   where it was before the mutation. Reported for the entity position and each sampled field, k/n.
2. **Repeatable.** Two resets from different mutations leave the same state. A checkpoint that
   always returns to the level start fails (1) but can pass (2), and `reproduce` can still use it as
   long as it replays the setup from the reset state. Unknowns stay unknown; neither claim is ever
   inferred from a method's name or return value.

## Discovery: callable candidates, not name matches

- **Candidate:** a method whose declaring type the manifest already names (a component on the
  identified entity, a scene object, or a static class), with zero required parameters, returning
  `void` or `bool`, not a property accessor, constructor, coroutine iterator or Unity message
  (`Awake`, `Start`, `Update`, and the rest of the engine's callbacks).
- **Engine types are excluded.** Methods declared in a `UnityEngine` or `UnityEditor` assembly are not
  game operations, and `Application.Quit()` would end the run rather than reset it.
- **The candidate is a fact:** `{id, declaring_type_id, method_id, parameter_count, return_type,
  static}`. The method name travels as data, exactly like a field name. The list is capped like the
  other collections, and truncation is reported rather than hidden.
- **No new scan is needed.** `IlWriteScanner` already enumerates the loaded types' methods in
  `CollectMethods`, and `MethodId` already carries the declaring type and parameter types. Emitting
  the callable subset is a small addition to the existing pass, not a second reflection walk.

## Selection: the manifest model names the bindings

- Stage 5 already asks a schema-validated model to choose readers and actions from what code
  discovered, and to put what is missing in the gaps list (#27). The same answer may name up to three
  **reset bindings**: `save`, `restore` (the game's load or continue operation) and `checkpoint`,
  each an action id or `null`.
- Code validates that the id exists, that the target is still alive and callable, and that two
  bindings do not share one id. An invalid binding is rejected with a reason; code never substitutes
  another candidate.
- The binding is a manifest fact, so `reproduce` uses the one the model selected, not one code
  picked. Which model and prompt produced it belongs to the manifest fingerprint (#28).
- A name like "Save" is one of the facts the model may use. The difference from a code whitelist is
  that the decision is schema-validated, reviewable in the manifest, and reported as a gap when absent.
- **Two selections, not one.** The manifest model binds a candidate at preflight. S1 may *use* a reset
  action at play time only if the manifest exposes it as an action, and that remains S1's choice.
  `reproduce` is an offline measurement procedure rather than an S1 turn: its reset step is part of the
  procedure the operator runs, it cannot escalate, and it never substitutes for an S1 choice.

## Measurement (stage 7)

One protocol for all three mechanisms, at rest, after the entity is identified:

1. Wait for rest and sample the reference state: entity position and the sampled fields.
2. If a `save` binding exists, call it once and sample again; that sample becomes the reference,
   because the saved state is what a restore can return to. A save that throws or returns `false`
   makes the measurement inconclusive.
3. Mutate with one calibrated action (#23), verify release, and confirm the mutation changed the
   position or at least one sampled field. If nothing changed, retry once with another calibrated
   action; if still nothing changed, the measurement is inconclusive, not a failed reset.
4. Call the `restore` or `checkpoint` binding once, wait, and sample.
5. Compare with the reference the way the scene reload does: position within 0.01, each field only
   when all its samples are comparable, and every unknown stays `null`.
6. Repeat for three counted trials, then run the repeatability check: two resets from two different
   mutations, comparing the two post-reset samples field by field.
7. A call that throws, outlives a 10 s timeout or crashes the run marks the trial invalid and records
   the runtime error. After two consecutive failures the binding is recorded as unavailable with that
   reason. A crashed run is restarted, not retried into a different state.

The three trial minimum matches the scene-reload measurement, so the two can be compared. The
mutation uses a calibrated action because its effect is already measured; choosing that action is a
measurement decision, not an S1 choice.

## What the record says

`reset.save_load` and `reset.checkpoint` gain a tri-state `status` when this is implemented:

- `measured`: `trials`, `trial_count`, `counted_trial_count`, `position_restored {restored, of}`,
  `fields` with the same columns as `scene_reload`, and `repeatable {equal, compared_fields}`, where
  `equal` is true only when the position and every comparable field agree. No single boolean: one
  failed property does not make a reset useless, and one passing property does not make it sound.
- `unavailable`: `reason` names the fact — no callable candidate in the inspection, no binding in the
  manifest, or a call that could not run. This is the FPSKit case.
- `inconclusive`: the binding was declared but the measurement could not observe it (save failed,
  the mutation changed nothing, the call timed out). `reason` names which.

`works` is retired when this lands. A boolean cannot carry three outcomes and would silently turn an
unmeasured method into a failed one. Until stage 7 exists the current record keeps `works: null` and
its generic reason; this note changes no schema now.

## Safety and cost

- Calls happen only during preflight measurement, only on declared bindings, and never during a
  scored run. `reproduce` uses a binding only when its record says `measured`.
- One call per trial, a 10 s timeout, the existing runtime-error capture, and a batch-mode game run.
  Any file a save call writes is recorded and stays out of the commit.
- No model calls in discovery or measurement. The only model cost is stage 5's selection, which is
  already in that stage's budget.

## FPSKit outcome

The fork at `25e43fb` has no save, load, checkpoint, restart, respawn or scene-load call in its 29 C#
files (24 in the FPSKit package, 5 in `Assets`); one comment mentions a pickup respawning, and there
is no persistence code at all. So:

- `save_load`: **unavailable** — the game exposes no save or restore callable.
- `checkpoint`: **unavailable** — the game exposes no checkpoint or restart callable.
- `scene_reload`: **measured** — 3/3 position restorations, with the field counts in
  `2026-10-27-fpskit-reset.md`.

This is a finding about this game, not a rule in code: the survey used names, which the automated
path must not.

## Consequence for `reproduce`

On FPSKit, `reproduce` resets with a scene reload and inherits its limits: scene-authored state only,
0.01 position tolerance, and fields that change continuously cannot show restoration. A game with a
measured save/restore can additionally restore state outside the scene. A game with nothing measured
gets a recorded reset gap, never an improvised reset.

## Alternatives rejected

- **Name whitelist in code** (`Save|Load|Checkpoint`): brittle, unverifiable, and moves the choice into
  code. A method named `Load` may load a config file.
- **Call every zero-argument method**: destructive and unbounded. One call can quit, delete a save or
  start a match, and there is no evidence that it finds a save/restore pair.
- **Pair methods by trial and error (n²)**: every pair needs a full mutate-and-compare trial, so cost
  grows with the square of the candidates and risk grows with every call.
- **Require the game to implement a bridge interface**: the strongest contract, but it only works for
  games that were modified. Keep it as the escape hatch if real games expose nothing callable.
- **Process restart as the only reset**: slow, and it loses in-game state unless the setup is
  scripted. It stays a fallback for a game with no measured reset, not a replacement.

## Deferred

- The manifest schema fields and fingerprint (#28).
- Which model performs the selection and how it is prompted (#27).
- Process restart as a harness-level fallback.
- Whether S1 can re-propose a binding the manifest model declined, once reset is exposed as an action.

## Validation

Documentation only: no code, schema, test or run changed, and no model was called. The availability
finding comes from reading the game's scripts; the measured reset numbers come from
`runs/preflight/issue89-fpskit-01/`, reported in `2026-10-27-fpskit-reset.md`.
