# FPSKit number keys: two real defects, reproduced k/n

Preflight calibration (#23) saw FPSKit log a `NullReferenceException` once each on the 1 and 2 keys
and did not investigate. Issue #91 records them before any blind run is scored. They are real
upstream defects, not seeded ones (#31), so a correct report of either must not count as a false
positive. This note reproduces each exception k/n under a stated reset, records the stack and the
condition, and names the eval's known-defect entries.

The runs used FPSKit commit `25e43fb`, Unity `6000.5.4f1` and the first enabled scene,
`Assets/FPSKit_TestScene.unity`. The probe ran from this branch on base `4b52ab4`, uncommitted at
the time; it was committed unchanged.
The local reports are:

- `runs/repro/issue91/digit1/repro.json` (SHA-256
  `4737aedbf09e92af16636a115979828759f685ebb39e58728983dd3a952abc07`);
- `runs/repro/issue91/digit2/repro.json` (SHA-256
  `8ff8ac9e256ff2f2409e0037c42adfdb0acad9a7ef6ff2f6060be4dd1c620ee8`);
- `runs/repro/issue91/control-f/repro.json` (SHA-256
  `8ce1e6cc420f32e77fa392a5632b80b05f9d22acdfbebdd49b78dc3f15eafda9`).

Each report's plan, handshake files, provenance and Unity log sit beside it and stay local. No
model was called. The probe is code-driven, not an S1 turn: the caller's plan names the one key,
and every press is sent once through the bridge `act` path with verified release.

## Plan

The plans were written before the runs and are hashed in each report.

- **Probe.** `scripts/unity/repro-runtime-error.ps1`, added by this change.
- **Reset.** A scene reload before every attempt: the PlayMode host unloads the scene and loads it
  again additively. A 1.0 s idle window with no input follows, and it must log no error.
- **Presses.** 5 attempts × 3 presses. Each press holds the key 0.1 s, and presses are 1.0 s apart.
  A press window closes 0.5 s after the press returns.
- **Predicate.** An `Exception` whose message contains `NullReferenceException` and whose stack
  contains `FirstPersonController.NextAttachment` for key 1, or
  `FirstPersonController.PreviousAttachment` for key 2.
- **k/n.** Valid attempts whose *first* press after the reload matched, over the valid attempts.
- **Control.** Key F (interact, with nothing in range) under the same plan and the key 1 predicate.
  Expected: no match and no error.

## Results

| Key | First press after reload | Presses matched | Errors per press window | Idle windows | Other errors |
|---|---|---|---|---|---|
| 1 | **5/5** | 15/15 | exactly 1 | 0 errors in 5 | 0 |
| 2 | **5/5** | 15/15 | exactly 1 | 0 errors in 5 | 0 |
| F (control) | 0/5 | 0/15 | 0 | 0 errors in 5 | 0 |

- **Validity.** Every attempt was valid, and every press window was complete. No error went
  unretained, so each window's count is exact and its events account for all of it.
- **Timing.** Applied holds were 0.100–0.141 s (key 1), 0.100–0.105 s (key 2) and 0.100–0.106 s
  (F). A reload took about 1.1 s from request to completion. The first press came 1.0–1.2 s after
  the reload completed.
- **Clock.** Real time and unpaused, with time scale 1 in all 45 press observations.
- **One error per press.** With a 0.1 s hold, a key stays down for several frames, yet each press
  logged exactly one exception. Holding the key does not repeat it.

## Stack

Every matched window held the same event. For key 1:

```text
NullReferenceException: Object reference not set to an instance of an object
FPSKit.FirstPersonController.NextAttachment () (at ./Packages/net.peeweek.fpskit/Runtime/Controller/FirstPersonController.cs:655)
FPSKit.FirstPersonController.LateUpdate () (at ./Packages/net.peeweek.fpskit/Runtime/Controller/FirstPersonController.cs:243)
```

For key 2:

```text
NullReferenceException: Object reference not set to an instance of an object
FPSKit.FirstPersonController.PreviousAttachment () (at ./Packages/net.peeweek.fpskit/Runtime/Controller/FirstPersonController.cs:669)
FPSKit.FirstPersonController.LateUpdate () (at ./Packages/net.peeweek.fpskit/Runtime/Controller/FirstPersonController.cs:245)
```

## Condition

This section is read from the game's source at `25e43fb`, not measured.

- **The call.** `LateUpdate` calls the next or previous attachment method on the frame its action
  is pressed. That is why a held key throws once.
- **The faulty guard.** Each method returns early only when no attachment is current *and* more
  than one is held. With none held, it falls through and reads the index of the null current
  attachment.
- **Why it always holds.** The controller prefab sets no initial attachment, and the test scene
  does not override it. The scene also has no attachment pickup, so no attachment can be held. The
  condition therefore holds from load onward, which fits 15/15 presses on each key.
- **The skipped frame.** The exception ends that frame's `LateUpdate` before the controller applies
  its movement, so that frame's movement and gravity are skipped. This was not measured.
- **Gamepad bindings.** The same two actions are bound to gamepad D-pad right (next) and left
  (previous). They were not pressed.

## What this supports

Both exceptions are real defects already present at the game's base commit `25e43fb`. Each
reproduces 5/5 from a scene reload, and on every press. They do not come from the probe: the
idle windows and the F control logged nothing.

Not established:

- the gamepad bindings;
- behaviour with an attachment held;
- the skipped frame;
- any other build.

The private eval repo now lists both defects as `fpskit-k01` (key 1) and `fpskit-k02` (key 2) in
`known/FPSKit.json`. Its matching rules score a claim that matches one as `known`, which is neither
a true positive nor a false positive. Before using these entries on any other build, the scorer
reruns them there.

## Validation

- **Probe tests.** The probe's focused tests pass, including a lint that keeps game words out of
  its code, tests and script.
- **Unity.** In each of the three runs, the Unity 6000.5.4f1 PlayMode test passed (1/1).
- **Game checkout.** Before and after each run, FPSKit's HEAD was `25e43fb` and only the three
  files that are modified by design differed. No game file changed.
