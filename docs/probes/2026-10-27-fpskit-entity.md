# FPSKit preflight stage 2

The active preflight probe identified `FirstPersonController` in the enabled FPSKit scene as
the one input-bound responding entity. Positions and displacements are in Unity world-space
`x, y, z` coordinates and Unity world units. A physical unit such as metres was not measured.
The within-run object id was `scene:Assets/FPSKit_TestScene.unity@1::5`; the loaded-scene index
is part of the id, so it is not a cross-run persistent identifier.

The final run used FPSKit commit `25e43fb`, Unity `6000.5.4f1`, and the first enabled build
scene. The game had pre-existing changes to its package manifest, package lock, and
`TimeManager.asset`; their hashes matched before and after the run. The bridge was wired to
this checkout for the run and the original package files were restored byte for byte. The local
record is `runs/preflight/issue22-fpskit-05/entity.json` (SHA-256
`6e8a80caaf48a4a6befc277bd654fee84c5a19a3775b63ecb5b0eb62d007b59f`); changed-file and dirty-game
hashes, the Unity result, and the log are retained beside it.

Four discovered keyboard controls were pressed twice each, using a 0.4-second lease. Their
terminal execution records and the final stop all verified release. Each trial sampled five
idle positions over 0.432–0.498 seconds; action snapshot windows were 0.412–0.465 seconds.
The entity's largest measured idle displacement was zero at those sample times. The two
measured world-space displacements for each control were:

| Control | Trial 1 | Trial 2 |
|---|---|---|
| A | `(-2.144, 0, 0)` | `(-2.035, 0, 0)` |
| D | `(2.048, 0, 0)` | `(2.025, 0, 0)` |
| S | `(0, 0, -2.032)` | `(0, 0, -2.036)` |
| W | `(0, 0, 2.067)` | `(0, 0, 2.038)` |

Discovery returned 62 objects, 215 components and 26 live bindings without truncation. Each
transform sample returned 62 objects of a 10,000-object limit. The candidate cap was 32;
four were found and none were truncated. The object set remained the same across discovery and
the sampled windows. A separate Unity camera also moved: it had no discovered input or physical
body, and its displacement matched the input-bound entity in two non-parallel directions. Its
maximum difference from the entity was 0.0247 world units across the eight trials. The raw
responding roots and all comparisons remain in the local record. Matching movement supports the
viewpoint classification under the recorded thresholds; it does not prove a hierarchy or rule
out all unobserved motion.

The first run returned `ambiguous` because the camera and player were both responding roots.
The explicit camera comparison was added afterward and checked in subsequent runs; the final
run above includes the later object-set and timing checks. Sampled idle windows can miss motion
between samples. Candidate selection currently covers discovered vector-valued Input System
bindings usable by the keyboard backend. Gamepad sticks and mouse delta were not driven; legacy
input and bindings not discovered by stage 1 are not established by this result. This is entity
discovery, not action calibration, freshness classification, or a gameplay outcome.

Validation: 355 Python tests, 179 Unity-free .NET tests, 211 Unity EditMode tests, and 11 Unity
PlayMode tests passed. The full PlayMode suite checks the preflight host boundary by default;
the separately requested active measurement passed in the final run. The experiment is a
code-driven preflight probe, not an S1 turn. Its path is discovered key → bridge `act` →
terminal execution record → verified release and `stop`; it does not route a runtime S1 choice.
Live adherence review is not applicable: no live S1 trace was available. No model calls were made.
