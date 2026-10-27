# FPSKit preflight reset: three scene-reload trials

The earlier calibration note measured a scene reload once, after the entity had moved only 0.231
world units. This run repeats it three times after a deliberate, larger movement and reports k/n.
The PlayMode harness now takes the first candidate control that the stage 2 record names for the
entity, holds it through the same input backend the bridge uses, verifies the release, and then
unloads and reloads the scene. No game word is involved: the control comes from the stage 2 record.

One run used FPSKit commit `25e43fb` and Unity `6000.5.4f1`. The same entity and scene as the earlier
notes were measured, in Unity world space and world units. The local record is
`runs/preflight/issue89-fpskit-01/record.json` (SHA-256
`89923e848c9dfe4903343cf55ce260daec3e9028521145f92f8c67b6b567da5f`) with `reset.json` (SHA-256
`4248130bd39ea3bc235d7a411b91b4afe6c17dbaae0ff59a0900d9aecb390348`) beside it; raw samples and
provenance stay local. No model was called, and this is a code-driven preflight probe, not an S1
turn.

## Method

- **Trials.** Three reloads per run. Each one holds the control for 1.2 s (applied 1.2004, 1.2002
  and 1.2003 s), waits 0.75 s, samples the position and 302 scene fields, unloads and reloads the
  scene, waits 1 s and samples again.
- **Movement.** A trial counts toward the position k/n only if the entity moved at least 1 world
  unit before the reload; the tolerance for "restored" is 0.01 world units.
- **Fields.** A field is compared only when its initial, pre-reload and post-reload samples are all
  comparable, and only inside counted trials. Anything missing, unsupported or non-finite stays
  `null`, and a field never compared in a counted trial is reported with `trials_compared: 0`.
- **Starting state.** Trial 1 starts at rest. Trials 2 and 3 start from the previous reload's
  post-reload state, so each trial measures one reload rather than three independent sessions.

## Position

All three trials moved the entity: 6.268, 6.132 and 6.131 world units from the trial's start. After
the reload the entity was 0.0000 units from that start in all three trials, so the reload restored
the entity position 3 of 3 times.

## Sampled fields

Of the 302 fields, 218 were comparable in all three counted trials; the other 84 are the same
unknown types reported in `2026-10-27-fpskit-freshness.md`. 209 fields did not change before any
reload and matched their initial value after it in all three trials.

Seven fields changed before at least one reload:

| Field | Changed before reload | Matched initial after |
|---|---|---|
| `PlayAudioEffect.idx` (object 5.0) | 3/3 | 3/3 |
| `PlayAudioEffect.idx` (object 5.1) | 2/3 | 2/3 (1 of the 2 changes came back) |
| `FirstPersonController.m_Move` | 3/3 | 0/3 |
| `FirstPersonController.m_Forces` | 3/3 | 0/3 |
| `FirstPersonController.m_ForceMag` | 3/3 | 0/3 |
| `FirstPersonController.m_JumpTTL` | 3/3 | 0/3 |
| `CinemachineBrain.m_LastFrameUpdated` | 3/3 | 0/3 |

Object 5.0's index followed A, D, S and W in the freshness run and came back every time here.
Object 5.1's index followed the stationary keys there; it changed in two trials and returned in one.

Two further fields had not changed before a reload but still differed from their initial value after
it in some trials: `CinemachineVirtualCameraBase.ActivationId` on the camera rig matched in none of
the three (it is an activation counter, and a reload activates the camera again) and
`FirstPersonController.m_Jump` matched in two of three. A reload creates objects again, so a mismatch
here means the value after the reload differs from the value at the start of the trial, not that the
field failed to reload.

## Limits

- Three trials, one control: the first movement candidate the stage 2 record names. A reset that
  restores the entity under that control is not shown to restore it under the others.
- A scene reload only re-creates the scene. State the game keeps outside the scene (static fields,
  `DontDestroyOnLoad` objects, a save file) is not observed, and full-state restoration stays `null`.
- Fields that change continuously at idle cannot show restoration by comparing a value before and
  after a reload. `m_Move`, `m_Forces`, `m_ForceMag`, `m_JumpTTL` and `m_LastFrameUpdated` are in
  that group, and none of them matched its initial value after a reload.
- The 84 unknown fields are unknown, not restored.
- One entity, one scene, one game, one run on one machine.
- Not established: reset by save/load or checkpoint, stepped-mode support, or any gameplay outcome.

## Validation

448 Python tests, 184 Unity-free .NET tests, 223 Unity EditMode tests and 13 Unity PlayMode tests
passed, and the exported schemas match the models. The PlayMode additions hold a discovered control
through the bridge backend, verify its release in each trial, and fail when the stage 2 record names
no movement candidate; the Python tests cover the k/n counting, the 1-unit movement threshold,
unknown fields and a missing trial. The pre- and post-run provenance files match on the repository
base commit, the branch, the game commit, the game's three pre-existing modified files and the four
changed checkout files; FPSKit's package manifest and lockfile were restored byte for byte.
