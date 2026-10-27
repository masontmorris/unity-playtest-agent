# FPSKit preflight stage 3

The active calibration measured how the stage 2 entity, FPSKit's `FirstPersonController`, responds
to each keyboard control the bridge can drive, which fields in its scene change at idle and while a
key is held, and how the game behaves under pause, step and scene reload. Positions and
displacements are Unity world-space `x, y, z` coordinates in Unity world units; a physical unit was
not measured. The within-run entity id was `scene:Assets/FPSKit_TestScene.unity@1::5`.

The final run used FPSKit commit `25e43fb`, Unity `6000.5.4f1`, and the first enabled build scene.
The game had pre-existing changes to its package manifest, package lock, and `TimeManager.asset`;
their hashes matched before and after the run, no file in this checkout changed during it, and the
original package files were restored byte for byte. The local record is
`runs/preflight/issue23-fpskit-04/record.json` (SHA-256
`5ce341670229d5277f103d43fd2b3f7c8b4a9e88155267e64460d6a4c21eedc9`). The raw samples, reset
evidence, Unity result and log, and pre/post provenance are beside it; they contain game field
values and stay local. The record's `plan` block holds the settings (holds, repeats, tolerances,
caps); every other number was measured in the run.

No model was called. The probe is code-driven, not an S1 turn: each key found in stage 1 is sent
once through the bridge `act` path with verified release, then `stop`. Live adherence review is not
applicable.

## Control effects

Eight keys were pressed for five requested holds, twice each: 80 valid trials of 82. Each cell is
the mean net displacement of the entity between a read just before the input and one just after it,
minus the measured idle drift, in world units, with the sample variance of the two repeats in
brackets.

| Key | 0.05 s | 0.1 s | 0.2 s | 0.4 s | 1.2 s |
|---|---|---|---|---|---|
| A | 0.335 (0.00171) | 0.536 (0.00195) | 1.034 (0.00003) | 2.042 (0.00018) | 6.035 (0.00166) |
| D | 0.264 (0.00022) | 0.501 (0.00000) | 1.032 (0.00002) | 2.047 (0.00014) | 6.023 (0.00021) |
| S | 0.289 (0.00257) | 0.535 (0.00241) | 1.034 (0.00020) | 2.048 (0.00023) | 6.022 (0.00020) |
| W | 0.255 (0.00007) | 0.539 (0.00233) | 1.044 (0.00011) | 2.042 (0.00092) | 6.038 (0.00237) |
| Space | 1.100 (0.00000) | 1.428 (0.00052) | 1.840 (0.00001) | 2.032 (0.00266) | 0.000 (0.00000) |

- **Movement.** A, D, S and W displace the entity along its own right or forward axis by 5.0 ± 0.1
  world units per second of applied hold for holds of 0.2 s or longer (4.76–5.20 at the two
  shortest holds). None is inverted relative to that basis, using the Input System's
  up/down/left/right part names: all 10 signed projections per key were positive. The variance
  comes from two repeats per cell; it describes this run and is not a tolerance band.
- **Minimum hold.** Each movement key and Space responded in both repeats at the shortest tested
  hold, 0.05 s requested (0.050–0.068 s applied). The threshold was not bracketed from below and no
  shorter hold was tested, so this is an upper bound on the minimum, not the minimum.
- **Dead zone.** Unknown. The keyboard backend supplies binary input, so there is no analog range to
  test. The record holds `null`, not zero.
- **Space** shifts the entity vertically by 1.10, 1.43, 1.84 and 2.03 units at 0.05–0.4 s. At 1.2 s
  the net is 0.000 because the entity had landed again before the post-input read. The key did act;
  a net-displacement measure cannot see motion that returns to its start.
- **F, Ctrl and Shift** produced no net displacement at any hold (largest 1.2e-7 units). That says
  nothing about effects that are not positional.
- **1 and 2.** FPSKit logged a `NullReferenceException` on the first press of each (stack frames
  `FirstPersonController.NextAttachment` and `PreviousAttachment`). Those trials are marked invalid,
  the keys were not pressed again, and no effect numbers exist for them. The cause was not
  investigated; with one occurrence each and no reproduction attempt they remain observations and
  are not promoted to findings.
- **Other bindings.** 15 discovered bindings (11 gamepad, 4 mouse) cannot be driven by the keyboard
  backend. They are recorded as unknown, not tested.

## Field freshness

302 fields in the entity's scene were sampled; the 10 fields of Unity's test-runner scene are
excluded. A change is exact inequality between adjacent samples of a comparable value, with no
tolerance. The idle window had four samples (three intervals, 0.19 s apart). Each of the 16 held
windows (8 keys, two repeats, at the 1.2 s hold) kept three samples (two intervals, 0.17–0.20 s
apart) taken while Unity's input update reported the key held; in every window the first of four
replies predated the key showing as held and was discarded. The requested spacing was 0.1 s, but
each sample reads the whole scene, so the achieved spacing is the one quoted. "Continuous" means a
change in every adjacent interval of a window, with at least two in a row.

| Class | Fields |
|---|---|
| No change observed, idle and held | 186 |
| Unknown | 107 |
| Changed at idle and while held | 5 |
| Changed only while held | 4 |
| Changed only at idle | 0 |

| Field | Idle | Held |
|---|---|---|
| `CinemachineBrain.m_LastFrameUpdated` | continuous | continuous |
| `PlayAudioEffect.idx` (object 5.1) | continuous | continuous |
| `PlayAudioEffect.idx` (object 5.0) | no change observed | single change |
| `FirstPersonController.m_ForceMag` | continuous | continuous |
| `FirstPersonController.m_JumpTTL` | continuous | continuous |
| `FirstPersonController.m_Jump` | single change | single change |
| `FirstPersonController.m_NextFoley` | no change observed | continuous |
| `FirstPersonController.m_ViewBobTime` | no change observed | continuous |
| `FirstPersonController.m_ViewBob` | no change observed | single change |

- **Unknown is not unchanged.** Of the 107 unknown fields, 69 hold a type the field reader does not
  serialize (including 13 `Vector3`, 7 `Vector2`, 2 `Vector4` and one `Quaternion`, plus structs,
  arrays and masks), 24 are object references, 11 are input actions, 2 are destroyed references,
  and 1 was null at idle but held an unsupported type once input began. Vector-valued state such as
  velocity is outside this measurement. (Measured since: see `2026-10-27-fpskit-freshness.md`.)
- **No change observed** means no change in the sampled intervals. It does not show that a field is
  constant or configuration, and nothing between samples was observed.
- **Held windows pool all eight keys**, so a held class is not attributed to one key; the record
  keeps each window's key.
- **Thin evidence for "continuous".** The first run to complete (below) classified the same 302
  fields identically except the held class of `m_Jump`, which was continuous there and a single
  change here. Two intervals per window do not separate those cases reliably.

## Clock and reset

- **Pause, step, resume (one cycle).** While paused, the entity's displacement was 0.0 and game time
  did not advance across 0.29 s of real time. Of the five fields that changed at idle, a camera
  frame counter kept changing under pause and the other four stayed fixed between two paused
  samples. A 0.2 s step advanced game time by 0.20006 s, and resume restored normal time.
  Stepped-mode support stays `null`: animation, physics and the game's own timers were not
  measured, and one cycle is one trial.
- **Scene reload (one trial).** The harness unloaded and reloaded the scene. The entity was 0.231
  units from its starting position before the reload and 0.0 after it (tolerance 0.01). Of the 7
  sampled fields that changed before the reload, 4 matched their initial value afterwards and 3 did
  not (the camera frame counter, `m_ForceMag` and `m_JumpTTL`); a camera activation id that was
  unchanged before the reload also differs after it. Full-state restoration stays `null`: sampled
  fields and one position do not establish it.
- **Save/load and checkpoints** were not exercised and stay `null`.

## A defect found in the first full run

The first run to complete reported W at 0.1 s as 0.997 units, nearly double A, D and S (0.54–0.56
in that run). Both of its W trials began mid-air at the apex of the preceding jump (height 2.04 and
2.07 against a resting 0.08). The quiet-window check accepted a baseline from a single still
interval, and motion that returns to its start nets to zero between two reads (idle drift 0.009 and
0.007, under the 0.01 tolerance). The falling arc added about −0.8 vertically. The check now
requires two consecutive still intervals, a regression test replays the apex, and in the final run
all 80 valid baselines are at resting height with zero drift. Excluding that cell, the 39 other
cell means of the two runs agree within 0.062 units. The first run's record
(`runs/preflight/issue23-fpskit-03/record.json`, SHA-256
`e8b848fd30cbb6b9a906a1f4009d2ab9f9945ccfd5e547a61e68cf1ffb7b88db`) stays local.

Two earlier attempts ended inconclusive without a combined record. In one, FPSKit's number-key
exception halted the probe; it is now recorded as a failed trial. In the other, the sampler could
not see the held key from Editor callbacks, which read a different input buffer from the game's; it
now reads the game's input update.

## Validation

429 Python tests, 184 Unity-free .NET tests, 221 Unity EditMode tests and 13 Unity PlayMode tests
passed, and the exported schemas match the models. The default PlayMode suite checks the
calibration entry point without measuring; the measurement above was the separately requested run.

Not established by this record: stepped-mode support, reset by save/load or checkpoint, dead zones,
analog, gamepad or mouse controls, freshness of vector-valued fields, per-key attribution of held
field changes, or any gameplay outcome.
