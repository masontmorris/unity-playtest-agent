# FPSKit preflight freshness: vectors, keys and repeated windows

Calibration (see `2026-10-27-fpskit-calibration.md`) left three freshness gaps: vector-valued fields
were `unknown`, a change observed while a key was held was not tied to a key, and "continuous"
rested on two intervals per window. The field reader now reports `Vector2/3/4` and `Quaternion` as
exact component arrays; the analysis splits held windows by key and reports where repeated windows
disagreed; and the sampling is denser. The same FPSKit entity and scene were measured as before, in
Unity world space and world units.

Two full runs used FPSKit commit `25e43fb` and Unity `6000.5.4f1`. The game's pre-existing package
and `TimeManager.asset` changes hashed identically before and after both, no file in this checkout
changed during either, and the original package files were restored byte for byte. The local
records are `runs/preflight/issue85-fpskit-01/record.json` (SHA-256
`cb6475927ca3b63ccaec153b0e644060a5570767455326446e33b549a5f8e3e2`) and
`runs/preflight/issue85-fpskit-02/record.json` (SHA-256
`d1a6fdf155667e4cebedd49861cba04d34ec5ea7613737c5a586f93eb9d3c170`); raw samples, Unity results
and provenance are beside them and stay local. No model was called, and this is a code-driven
preflight probe, not an S1 turn.

## Sampling

- **Idle:** two windows of 8 samples (7 intervals each), 0.17–0.33 s apart, one before the key
  trials and one about 115 s later.
- **Held:** 32 windows per run (8 keys × 4 repeats of the 1.2 s hold). Each kept the samples taken
  while Unity's input update reported the key held: 5 in 62 of the 64 windows (run 02 had one window
  with 6 and one with 4), so usually 4 intervals, 0.17–0.28 s apart. In 63 of 64 windows the first
  of the six replies predates the key showing as held and is discarded.
- **Trials:** 96 of 98 were valid in each run. Every valid baseline was at rest height with zero
  drift. The two invalid trials are the number-key exceptions already recorded.

## Vector and quaternion fields

Of the 302 fields in the entity's scene, 23 hold a `Vector3` (13), `Vector2` (7), `Vector4` (2) or
`Quaternion` (1). In the earlier note all 23 were `unknown`. Now 21 show no change in the sampled
intervals and 2 changed: `FirstPersonController.m_Move` and `m_Forces`, both `Vector3`, changing
continuously at idle in both windows and under every key. A vector is compared component by
component with exact equality, so a jittering component counts as a change.

84 fields remain unknown, not unchanged: 46 hold a type the reader still does not serialize
(structs, arrays, collections, masks, delegates and others), 26 are object references, 11 are input actions, and 1 was null
at idle but held an unsupported type once input began.

## Which keys a change followed

Eleven fields changed in at least one window. "Changed under" means a change was seen in that key's
windows (four per key). It says what the field did during those windows, not that the key caused it.
F, Ctrl and Shift leave the entity stationary, so their windows resemble idle.

| Field | Idle | Changed under | Unchanged under |
|---|---|---|---|
| `CinemachineBrain.m_LastFrameUpdated` | continuous | all 8 keys | none |
| `FirstPersonController.m_Move` | continuous | all 8 keys | none |
| `FirstPersonController.m_Forces` | continuous | all 8 keys | none |
| `FirstPersonController.m_ForceMag` | continuous | all 8 keys | none |
| `FirstPersonController.m_JumpTTL` | continuous | all 8 keys | none |
| `PlayAudioEffect.idx` (object 5.1) | continuous | F, Ctrl, Shift, Space | A, D, S, W |
| `FirstPersonController.m_Jump` | continuous (run 01: one window a single change) | F, Ctrl, Shift, Space | A, D, S, W |
| `PlayAudioEffect.idx` (object 5.0) | no change observed | A, D, S, W | F, Ctrl, Shift, Space |
| `FirstPersonController.m_NextFoley` | no change observed | A, D, S, W | F, Ctrl, Shift, Space |
| `FirstPersonController.m_ViewBob` | no change observed | A, D, S, W | F, Ctrl, Shift, Space |
| `FirstPersonController.m_ViewBobTime` | no change observed | A, D, S, W | F, Ctrl, Shift, Space |

The two key sets were the same in both runs for all 302 fields. The five fields changing under
every key also changed continuously at idle, so held input alone does not explain them. The
`PlayAudioEffect.idx` field on object 5.2 did not change in any window.

## Stability across windows and runs

- **Across runs:** the overall class, the continuous class and the keys whose windows showed a change
  were identical for all 302 fields. The largest difference in any key-and-hold mean displacement
  was 0.047 world units.
- **Across repeated windows:** 218 fields had at least two windows with comparable intervals in
  some scope (idle, or one key's held windows); the other 84 are the unknown fields. In run 01, 2 of
  them had windows that disagreed: `PlayAudioEffect.idx` (5.1) under F, Ctrl, Shift and Space, and
  `m_Jump` in the idle windows (one single change, one continuous) and under those four keys. In
  run 02, 3 did: those two (with `m_Jump`'s idle windows agreeing) and `m_ViewBob` under W (one
  window with no change, three with a change). Every other compared field had the same class in all
  of its repeated windows. The record names these scopes in `varied_between_windows`.
- **Idle is not a startup transient:** six fields were continuous in both idle windows, about 115 s
  apart, in both runs (`m_Jump` was a seventh in run 02).

## Limits

- Each held window has 4 intervals and each idle window 7, 0.17–0.33 s apart, so changes between
  samples and anything shorter than that are unobserved.
- No change observed does not show that a field is constant or configuration.
- Fields that change continuously at idle cannot show restoration by comparing a value before and
  after a scene reload: `m_Move` and `m_Forces` changed before the reload and differed from their
  initial values after it in both runs.
- One entity, one scene, one game, two runs on one machine.
- Not established: causes, freshness of the 84 unknown fields, stepped-mode support, reset by
  save/load or checkpoint.

## Validation

446 Python tests, 223 Unity EditMode tests and 13 Unity PlayMode tests passed, with the exported
schemas matching the models. The EditMode additions check that vectors and quaternions come back as
exact component arrays, that a non-finite component is reported as `special_vector` with the other
parts as numbers, and that an unrelated struct (`Color`) stays unsupported.
