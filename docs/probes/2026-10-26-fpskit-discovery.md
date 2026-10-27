# FPSKit preflight stages 0–1

Issue #21 produced an EditMode survey and a PlayMode discovery snapshot for the clean FPSKit
bridge fork at commit `25e43fb`, using Unity `6000.5.4f1`. The project had existing local
changes to its package wiring and `TimeManager.asset`; the package files were restored after
the capture. The full snapshots and their file hashes remain local under
`runs/preflight/issue21-fpskit-02/`. They contain third-party source and are not committed.
No model calls or control measurements were made.

| Collection | Returned | Limit | Truncated |
|---|---:|---:|---|
| Registered packages | 65 | 1,024 | No |
| Authored scenes | 1 | 4,096 | No |
| Authored scripts | 29 | 20,000 | No |
| Input action assets | 0 | 4,096 | No |
| Legacy axes | 30 | 4,096 | No |
| Loaded scenes | 2 | 4,096 | No |
| Scene objects | 62 | 100,000 | No |
| Components | 215 | 4,096 | No |
| Reflected fields | 312 | 30,000 | No |
| Live bindings | 26 | 10,000 | No |
| Source excerpts | 29 files / 80,843 characters | 20,000 / 750,000 | No |
| IL methods scanned | 183 | 20,000 | No |

The survey reported `both` input handling and no collection errors. The loaded scenes include
the game's enabled scene and Unity's temporary test-runner scene. The source excerpts contain
all 29 authored scripts in this sample, rather than a ranked or method-level slice of the
input-to-motion path. Bindings from live component fields and standalone input assets are
reported; nested managed objects are not recursively searched for bindings.

The IL scan recorded 191 direct field–method pairs and listed 263 field descriptors. It also
reported 51 field-address operations that could be indirect writes and four methods whose bodies
were unavailable. Its completeness flag is false and the total direct-write and issue counts are
unknown for the unobserved effects. These are static facts, not proof that a method ran or that a
field is configuration or runtime state. Caps and errors are included in each snapshot; this
stage does not create a manifest fingerprint. Of the 312 live fields, 168 have a matching
declaration in the scanned assemblies. A missing match is not evidence that a field has no writes.

Validation on this checkout: 329 Python tests and 174 Unity-free .NET tests passed. The full Unity
suites passed 202 EditMode and 10 PlayMode tests before the final field-declaration link; the
capture and focused scene tests checked that link afterward. This evidence supports the captured
inventory and discovery facts only. There is no S1 turn, finding, reproduction, calibration or
gameplay outcome to evaluate here.
