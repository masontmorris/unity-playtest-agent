# Manifest and cache review reference

Read this for a future production manifest, cache or descriptor replay, or when a static probe result is being read for semantics. The probes do not produce these artifacts; this is design review, and runtime effects remain `unknown` without measurement.

## Action and control meaning

For each action, reader and rule, build a provenance row: manifest id -> discovered control or field -> source path:line -> measured effect -> run or probe id. A row without a source and a measurement is a gap, not a confirmed meaning.

- Check sign and polarity. A "maximum" on one axis can be the opposite command, and a brake level can actually be throttle.
- Check exact discrete values against interpolation. A selected zero level decoded to 1/42 became a positive command, so the game missed its exact-zero branch. If a level means zero, it must decode to zero.
- Check declared analog capability against the actions S1 can select. A manifest can declare an axis while offering only a fixed action that pins it, or a steer-only action that releases throttle. Report the gap; do not synthesise an action.
- Check compound actions and whether one action changes a control another action depends on.

## Rules and invariants

Name the runtime writer of every field a rule reads, with its source line. A field that only appears in configuration is not a play-state rule. A timer or continuous value needs an explicit progress justification.

- Check polarity: a `failure` plus `true` on a counting flag fired during normal lap timing.
- Check vacuity: `never_decreases` on a field that counts up can only catch a reset. Name the bound or transition that would catch the claimed failure.
- Check that a violated invariant becomes a fact and a suspicion for S1, never a help request.
- Keep timers and continuous values out of declared progress unless the design explicitly scores them as progress.

## Cache and replay

- Fingerprint authored structure: scenes, scripts, bindings, schema and prompt versions, and discovery caps. Do not include Unity instance order or transient runtime state; a reordered instance list changed candidate ids and the fingerprint.
- Never cache an incomplete or ambiguous result. If a declared analog axis has no selectable action, that is incomplete.
- Replay only against frozen descriptors that still resolve in the current scene. Record cache source and reuse; a cache hit is not "zero preflight work".
- A semantic change bumps the version. A cache from an older schema or prompt is re-derived, not silently reused.

## Gaps and name-blindness

A missing control, phase field or recovery path is a gap shown to S1, never a reason for code to add, delete or rewrite an action after the model answers. Game knowledge belongs in the manifest; `src/playtest/core/` stays name-blind. A generic-looking suffix rule is still game knowledge if it assumes a field family.

## Outcome

Report the provenance table, semantic findings, invalid or incomplete rows, cache identity findings and explicit gaps. State that this is design review unless a real manifest artifact and measurement exist. If a row is invalid, preserve it and limit the claim; stop any dependent use, not the review.
