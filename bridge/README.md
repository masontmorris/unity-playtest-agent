# Playtest bridge (Unity package)

The Unity bridge package is licensed under the [Apache License 2.0](LICENSE).
See [NOTICE](NOTICE) for its copyright notice.

Loopback JSONL transport between the playtest core and the Unity Editor. This is the
`com.playtest.bridge` package; reference it from a test game's `Packages/manifest.json` with a
`file:` path. It is never copied into the game's `Assets/`.

The bridge is game-agnostic by design: it carries generic verbs and facts, never a title,
control, field or genre. Game knowledge lives only in the preflight-generated manifest, and the
test games are external subjects.

The bridge is built in layers:

| Layer | Verb | Status |
|---|---|---|
| Transport, hello/status, project lock | `hello` | Landed (#14) |
| Continuous observation stream | `observe` | Landed (#15) |
| Bounded actions, freshness, input leases | `act` | Landed (#16) |
| Stop, release verification and clock | `stop`, `clock` | Landed (#17) |
| Capture to an offscreen render target | `frame_ref` (part of `observe`) | This issue (#18) |
| Core skeleton, bridge client, fake game | — | Landed (#19) |
| Editor-only project inspection | `inspect` | Stage 0–1 (#21) |

## Layout

```
bridge/
  package.json                  UPM package manifest
  Runtime/Protocol/             wire DTOs and strict JSON reading (no Unity dependency)
  Runtime/Actions/              action catalog, input lease, clock interface (no Unity dependency)
  Runtime/Lock/                 project lock file (PID, port, token)
  Runtime/Transport/            TCP listener, JSONL framing, token, single session
  Runtime/Unity/                Unity status, observation, input, clock and capture sources
  Editor/                       Editor lifecycle and Tools/Playtest Bridge menu
  Editor/Session/               supervised session mailbox, state machine, persistence
  Tests/EditMode/               EditMode tests (the acceptance check)
  Tests/DotNet~/                Unity-free fast tests; ignored by Unity
```

## Wire protocol

The contracts are the Python models in `src/playtest/contracts/`; `schemas/` is exported from
them. The bridge speaks `upa/1`. Every line is one JSON object, UTF-8, `\n` terminated.

1. Core connects to `127.0.0.1:<port>` from the lock file.
2. Core sends `hello` with the token from the lock file.
3. The bridge replies `status` and holds the session.
4. A second caller gets `{"code":"busy"}` immediately; a bad token gets `auth`; a different
   `upa/N` gets `protocol`.

The lock file lives at `<project>/Temp/playtest-bridge.lock`, is written only while the bridge
runs, and is not committed. It contains the owning PID, the port, the protocol and a random
per-session token. The token is never logged.

## Running the bridge

Open the test game in Unity 6000.5.4f1. The bridge starts with the Editor unless Unity runs in
batch mode; batch runs stay idle and the EditMode tests call `BridgeEditorHost.Start()`
themselves. Use `Tools/Playtest Bridge/Start`, `Stop` and `Status` to control it. Start is
idempotent. Status logs the running state, protocol, bridge version, port, pid, lock path and the
last error.

It listens on an ephemeral loopback port, so nothing needs configuring. On start it writes
`<project>/Temp/playtest-bridge.lock` with the owning pid, the port, the protocol and a fresh
per-session token. The token is only ever written there: it is never logged, committed or shown
by Status.

Stop, Editor quit and the Editor's before-reload callback all stop the server and release the
lock, so a script recompile leaves no stale lock or leaked socket behind. If another process
already holds the project lock, Start stops its own server and records a warning with the owner's
pid and port instead of throwing; the message is available as `BridgeEditorHost.LastError`.

## Supervised Play Mode session

A live session can be hosted by the Editor instead of by a PlayMode test. The wrapper starts Unity
with `-executeMethod Playtest.Bridge.Editor.PlayModeSessionHost.Start` and no `-runTests` or
`-quit`; that class owns Play Mode from then on, so ending a play instance never kills the client
process and the client never owns the Editor's lifetime. Python is the client and speaks to the
bridge over the ordinary loopback transport. The old bench host
(`Tests/PlayMode/BenchRecordingPlayModeTests.cs`) is unchanged and still serves the bench protocols.

The host is inert unless `UPA_SESSION_CONTROL_DIRECTORY` is set, and while it is set
`BridgeEditorHost`'s auto-start stays suppressed so nothing except the host starts a bridge. The
control directory holds the mailbox, and the host never carries a token through it:

| File | Written by | Meaning |
|---|---|---|
| `config.json` | the wrapper | run id, project path, catalog path and SHA-256, wall deadline |
| `host-state.json` | the host | `phase`, `generation`, `scene_path`, `scene_handle`, `ready_utc`, `error`, `capabilities` |
| `request.json` | the client | one restart ticket: run id, ticket id, `from_generation` |
| `result.json` | the host | that ticket's `accepted`, `stopped`, `starting`, `ready`, `failed` or `cancelled` state |
| `cancel.json` | the client | cancels the ticket in flight |
| `shutdown` | the wrapper | out-of-band end of the session; not an S1 choice |

`host-state.json` phases are `starting`, `ready`, `stopping`, `stopped` and `failed`. Records are
read and written strictly: unknown members and wrong types are refused. A ticket from another run, one
already answered and one still in flight are each logged once and left alone, because their result
record is not this run's to overwrite. A ticket that is stale, premature (the host is not ready) or past
the wall deadline is published as `failed` with the reason rather than silently dropped. A record that
cannot be parsed at all is logged once and never acted on. Absent measurements stay `null`; nothing is
defaulted to zero or to success.

The host executes exactly the one ticket S1 selected and nothing else. It never invents a request,
never retries a failed operation, and never reports readiness it did not observe. An accepted
restart releases the bridge and its lock, leaves Play Mode, waits for `EnteredEditMode`, re-enters,
waits for `EnteredPlayMode`, loads build scene 0 with an explicit single-mode load, re-validates the
pinned catalog, mints one new generation and starts a new bridge with a new token and lock. Only
then does `result.json` reach `ready`. Budgets and the wall deadline persist across a restart.
The in-flight phase is written to `SessionState` before each transition and restored after the next
domain load; the play-mode callback restores it before handling an event, because Unity can raise
`EnteredEditMode` or `EnteredPlayMode` before the first Editor update.

It fails closed, exiting non-zero, when it cannot prove the state it is in: a `failed` transition, a
domain reload that resumes a `ready` machine (the bridge, token and lock died with the reload, so
that is an accidental disconnect), `EnteredEditMode` during startup that no ticket asked for, a
missing or altered catalog, or a scene that does not load. Cancelling publishes `cancelled` and then
ends the run the same way, because a cancelled ticket's generation, scene and bridge are gone and no
later phase can be reached.

Each play instance mints one canonical lower-case UUID generation before its bridge starts, and the
frames of that instance are written under `<project>/Temp/playtest-bridge-frames/<generation>/`, so
a restart never mixes two instances' frames. An observation's `frame_ref` is always inside the
generation directory of the instance that produced it.

## Inspecting a project

The authenticated `inspect` request runs on the Editor main thread and reads project data without
writing to the project. Request `inventory` in Edit mode; request `discovery`, `transforms` and
`fields` in Play mode. An incompatible mode returns an error. The response carries its stage,
request sequence, data, caps and errors. A cap includes the limit, observed count, returned count
and whether it truncated; unknown counts and truncation remain null.

The `transforms` stage samples every loaded scene's GameObjects once: their discovery ids, parent
id, active state, scene path and world-space position `[x, y, z]`, plus the sampling `frame`,
`real_time_s` and `game_time_s`. `position_unit` is `unity_world_unit`. A position that cannot be
read stays null with an error; it is never replaced by zero. This is a sample only: calibration
that presses controls twice and compares displacement against an idle baseline is a separate step.

The `fields` stage is the repeated freshness sample. It runs the same bounded discovery collection
as `discovery`, with the same component, field and binding limits, so its `game_objects` and
`fields` rows carry exactly the discovery ids and coverage. It returns only those two collections
with their two caps and errors, plus the sampling `frame`, `real_time_s`, `game_time_s` and
`time_unit` (`seconds`). It does not repeat source excerpts, IL or input-binding rows on every
tick, does not write to the project and does not generate input. Request it repeatedly in Play mode
to measure how a field changes between samples; unknown coverage stays null.

Field values come back as `number`, `integer`, `boolean`, `enum`, `string`, `null`, object
references and input actions. `Vector2`, `Vector3`, `Vector4` and `Quaternion` are exact component
arrays (`vector2`, `vector3`, `vector4`, `quaternion`), and `Vector2Int` and `Vector3Int` are integer
arrays (`vector2_int`, `vector3_int`). A vector with a non-finite component is reported as
`special_vector`, with the finite parts as numbers and the others named, because JSON has no `NaN`
or `Infinity`. Any other type is `unsupported` and carries its type name; it is never reported as
zero or unchanged.

The source excerpts are unranked authored scripts, including scripts in local or embedded
packages. The package does not scan IL. `discovery` reports `il_inputs`: the assemblies that
contain authored scripts, every assembly file the Editor loaded, and whether that selection was
complete. Preflight runs the IL write scan outside Unity (`tools/ilscan`) and merges its sections
in place of `il_inputs`. That scan reports direct field stores and unresolved cases; it does not
prove that a field is configuration or that a method ran. Review its scope, caps and errors
before using an absence as evidence. Snapshot sizes over the transport limit fail explicitly. Full dumps can
contain third-party source and must remain local. This stage does not create a manifest
fingerprint; a later fingerprint must include the recorded caps.

With the bridge running, save each stage outside the inspected project:

```powershell
uv run playtest-inspect --project "C:\path\to\game" --stage inventory --output runs/preflight/survey.json
# Enter Play mode and load the scene under test before the next request.
uv run playtest-inspect --project "C:\path\to\game" --stage discovery --output runs/preflight/discovery.json
uv run playtest-inspect --project "C:\path\to\game" --stage transforms --output runs/preflight/transforms-1.json
uv run playtest-inspect --project "C:\path\to\game" --stage fields --output runs/preflight/fields-1.json
```

The command refuses to overwrite a file. For `discovery` it also runs the IL write scan, which
needs the .NET 10 SDK on `PATH`, and saves the merged snapshot. For a repeatable batch capture
using the first enabled build scene, see `scripts/unity/capture-preflight.ps1`.

## Actions

`act` executes one named action or skill from a project catalog, not a compiled-in control. The
catalog is `<project>/Temp/playtest-bridge-actions.json`, written by the test harness or by
preflight. It is data and is not committed. An action holds one to three simultaneous inputs for a
lease; a skill runs one to eight ordered steps, each holding its inputs and then waiting:

```json
{
  "actions": {
    "chord": {
      "backend": "input_system",
      "inputs": [
        { "device": "Keyboard", "control": "w" },
        { "device": "Gamepad", "control": "leftStick", "x": 0.3, "y": 1.0 }
      ],
      "default_lease_s": 0.4
    },
    "sequence": {
      "backend": "input_system",
      "steps": [
        { "inputs": [{ "device": "Keyboard", "control": "space" }], "hold_s": 0.05, "wait_s": 0.2 },
        { "inputs": [{ "device": "Mouse", "control": "delta", "x": 0.5, "y": 0 }], "hold_s": 0.1 }
      ],
      "max_s": 1.0
    }
  }
}
```

The original single-key shorthand (`device`, `control`, `default_lease_s`) still loads as one input.
The catalog is validated before anything runs: unknown members, counts, durations (an action hold
and `max_s` are at most 3600 s, a skill's declared holds and waits must fit inside `max_s`) and the
device vocabulary below.

| Device | Controls | Values |
|---|---|---|
| Keyboard | any `Key` name | none |
| Mouse | `leftButton`, `rightButton`, `middleButton`, `forwardButton`, `backButton` | none |
| Mouse | `delta` | `x`, `y` in [-1, 1]; one relative impulse of x * 64 and y * 64 pixels, applied once |
| Gamepad | `buttonSouth`/`North`/`East`/`West`, `leftShoulder`, `rightShoulder`, `leftStickPress`, `rightStickPress`, `start`, `select` | none |
| Gamepad | `leftStick`, `rightStick` | `x`, `y` in [-1, 1], written to the device state unchanged |
| Gamepad | `leftTrigger`, `rightTrigger` | `x` in [0, 1] and `y` = 0 |
| Gamepad | `dpad` | `x`, `y` each -1, 0 or 1 |

Everything else is rejected by name: mouse `scroll` (its units differ by platform), `position` and
any absolute pointer, touch, XR, joysticks, and child control paths such as `dpad/up`. Nothing is
normalized, clamped, remapped or inferred.

The bridge looks up the action, checks `based_on_seq`, `max_age_s`, the declared `depends_on`
paths and the play state atomically, then validates the act itself without touching a device:
non-empty `params` are rejected (`invalid_params`), a skill's `lease_s` must be absent or exactly its
`max_s`, and every input of every step must be executable. Only after that does a new act release
the previous run and report it `cancelled`. A rejected act never disturbs a running one. An action's
`lease_s` is capped at 3600 s; `lease_s` is never reinterpreted as a step hold.

Execution is verified from device state, never assumed. Inputs of one step are queued together, per
device, as one state. The bridge reports `running` only after a device read, inside an input update
the game can observe, showed every input applied; that read is the input's start. The hold ends with
a release, and the input's end is the read that showed it neutral. A step is released and verified
before the next step is applied and before its wait begins, so steps never overlap and none is
skipped. Any failed apply or unverified release fails the run, releases everything and stops the
remaining steps. A newer act, `stop`, a scene change, the skill's `max_s` deadline (`timed_out`) or a
disconnect cancels the whole sequence and its timers; nothing is resumed or replayed.

Every `execution` record carries `applied_inputs`, one receipt per applied input: `step_index`,
`device`, `control`, the declared `x` and `y`, `input_start_wall_s`, `input_end_wall_s`,
`applied_duration_s`, `applied_delta_x/y` and `release_verified`. Durations are measured from the
two device reads and never copied from the configured hold; a mouse delta has no duration, and its
receipt carries the pixel delta the device read back, which must equal the declared one or the apply
fails. Anything unmeasured is an explicit `null`. The record's own `applied_duration_s` runs from
the first confirmed apply to the last confirmed release.

Device work happens on the Unity main thread. A caller on another thread waits for it holding no
lock the main thread needs; a caller on the main thread (scene change, host shutdown) runs it inline
with a manual input update and never waits on itself. A mouse delta and a key press edge are not
flushed early, so the game sees them in its own input update. Absent devices are created and removed
by the bridge: the keyboard at start, the mouse and gamepad when an action first declares them. A
physical device that exists is used as it is. In batch mode the Input System focus settings are
scoped so injected input reaches the game loop and restored on stop; an interactive Editor is left
alone. Fields samples carry `keyboard_state` (unchanged) and a generic `input_state` for the
keyboard, mouse and gamepad.

## Stop and clock

`stop` cancels the active lease, releases every held input, verifies the release against the
device state, sends one `stopped` result with the released inputs, and closes the session. Every
stop reason is accepted. A dropped connection releases and verifies too, with no result because
the channel is gone. Scene changes stop the active session; the bridge stays listening for the
next one. A stop raised on the main thread flushes the queued release before verifying, so the
scene-change and host-shutdown paths report the device state rather than a guess.

`clock` acknowledges with `status`; the next observation reports the effect. `pause` stops the
game clock, `resume` returns to realtime, `set_scale` changes the scale, and `step` advances game
time by the requested interval and returns to paused. The observation's `clock_mode` is
`stepped` while paused/stepping and `realtime` otherwise. `status.clock_control` is true.

## Capture

Capture is offscreen: every enabled screen camera renders into a dedicated render target, then
the Screen Space - Overlay canvases are lent a temporary camera and blended on top. Nothing reads
the Game view back buffer, so a hidden or unfocused Game view cannot replace the frame with its
clear colour. The composite is read back and encoded as JPEG at 640 px wide; a worker thread
writes the bytes at 10 frames per second into a bounded window under
`<project>/Temp/playtest-bridge-frames/`, which holds the latest 120 frames by default.
`frame_ref` is the absolute path of the latest complete file, so a frame is never named before
its bytes are on disk.

At start the capture runs a self-test: a temporary camera and a top-half overlay canvas go through
the same composite, the JPEG is decoded again and both halves are checked. The result appears in
`status.capture` and `status.capture_self_test` (`passed`, `failed` or `unknown`). The self-test
also runs outside play mode, which is when the Editor host starts; the EditMode runner measures
that path with `-Graphics`, while the default no-graphics run measures the fail-closed result.
Without a graphics device (`-nographics`) the self-test fails closed: `capture` is false and
`observe` with `with_frame` is rejected. `with_frame` is only meaningful when capture is
available; observations without it never carry `frame_ref`.

Capture is on by default while the project plays. Tests that need it must run Unity without
`-nographics`; the PlayMode test script does that by default and accepts `-NoGraphics` for
machines without a device.

### Game view

Every observation carries `view`, where the game-view camera points in world space:
`{"yaw_deg": ..., "pitch_deg": ...}`. The game-view camera is the first of the screen cameras the
capture composites (lowest depth, an overlay camera after a base camera of equal depth), so the
angles describe the frame the capture writes; `UnityGameView` owns that order and the capture
uses it too. Both angles come from the camera's forward vector: yaw is `atan2(forward.x,
forward.z)` in [-180, 180] and a positive change turns right, and pitch is `asin(-forward.y)` in
[-90, 90] and a positive change looks down, as Unity's euler x does. They are engine facts, not
game fields, and they sit beside `public` rather than in it, so what a decision-maker is shown
under `public` is unchanged. `public.camera.rotation` stays the raw euler angles of Unity's main
camera (the first enabled camera when none is tagged main), which need not be the game-view camera.

A view that cannot be read is `null` (omitted on the wire) with a `view` entry in
`reader_errors`: no screen camera draws, or the camera looks straight up or down where a heading
does not exist. It is never a heading of zero. A reading is a direction; a turn is the difference
of two readings with yaw wrapped to [-180, 180).

## Probe

`public.probe` is a measured geometry sample in the anchor's own frame: what is directly under and
in front of the anchor. It carries raw facts only. Nothing is labelled passable, climbable or
blocking, and a reading that could not be taken is `null`, never `0`.

```json
{
  "version": 1,
  "anchor": {"kind": "path", "name": "Entity", "position": [0.0, 0.0, 0.0]},
  "under": {"forward": 0.0, "lateral": 0.0, "hit": true, "point": [0.0, 0.0, 0.0], "delta_y": -0.02, "normal": [0.0, 1.0, 0.0], "inside": false},
  "samples": [
    {"forward": 0.6, "lateral": -0.35, "hit": true, "point": [0.1, 0.25, 0.6], "delta_y": 0.25, "normal": [0.0, 1.0, 0.0], "inside": false}
  ],
  "blockers": [
    {"height": 0.15, "max_distance": 1.8, "hit": true, "distance": 0.38, "point": [0.0, 0.15, 0.38], "normal": [0.0, 0.0, 1.0]}
  ]
}
```

Every field is present on every entry. The anchor is resolved once, cached, and re-resolved when
the cached reference is destroyed: `UPA_PROBE_ANCHOR` as a hierarchy path for `GameObject.Find`
(`kind: "path"`), otherwise the first enabled `CharacterController`
(`kind: "character_controller"`), otherwise the first enabled `Rigidbody` (`kind: "rigidbody"`).
With none of them, `probe` is absent and `reader_errors` carries `no anchor: set
UPA_PROBE_ANCHOR or have an enabled CharacterController or Rigidbody in the scene`.

The nine `samples` are downward casts on a 3x3 grid: `forward` along the anchor's forward
direction and `lateral` along its right, both with the vertical part removed (a direction with no
horizontal part falls back to the world axis). They are ordered forward `0.6`, `1.2`, `1.8`, and
within each forward lateral `-0.35`, `0.0`, `0.35`. A cast starts 6 m above the sample point and
runs 12 m down. Triggers and the anchor's own colliders are ignored.

`under` is the same kind of reading taken straight down from the anchor, so it says what the anchor
stands on and how far the anchor is above it (`under.delta_y`, measured against the anchor's own
height). Every grid `delta_y` is measured against the surface under the anchor instead: flat ground
is `0`, and a surface one riser up is that riser's height, which is what a step limit can be
compared with. When the ground under the anchor cannot be measured, every grid `delta_y` is `null`
rather than a height above nothing.

A surface under the point is `hit: true` with `point`, `normal` and `delta_y`. A point with nothing
under it is `hit: false` with `point`, `delta_y` and `normal` null. A point inside geometry, which
is an obstruction taller than the cast or an overhang, has no readable surface from above: it is
`hit: true` with `inside: true` and those three readings null.

The two `blockers` are forward casts at `height` `0.15` and `0.9` above the anchor, 1.8 m long
(`max_distance`), along the same horizontal forward. A hit reports `distance`, `point` and
`normal`; a miss reports all three as null.

The probe is re-sampled at most every 0.1 s of unscaled time, and the cached reading is reused
between samples. A sample that throws is dropped for that tick: no `probe` is published and the
`probe` reader error says why.

## Tests

Three layers:

| Layer | What it covers | Command |
|---|---|---|
| EditMode | protocol, transport, observations, the probe grid, lock, status, stop/clock and the fail-closed capture path; `-Graphics` measures the self-test outside play mode | `./scripts/unity/run-editmode-tests.ps1 -ProjectPath "C:\path\to\project"` |
| PlayMode | stream, act, clock, stop, real input release, capture (self-test, status, frame window, frame_ref) | the same command with `-TestPlatform PlayMode` |
| Unity-free | pure C# transport, seconds, no Editor | `dotnet test bridge/Tests/DotNet~` |

Acceptance (EditMode, run from the test game project):

```powershell
./scripts/unity/run-editmode-tests.ps1 -ProjectPath "C:\path\to\project"
```

PlayMode measurement, against the same project and the same `testables` entry:

```powershell
./scripts/unity/run-editmode-tests.ps1 -ProjectPath "C:\path\to\project" -TestPlatform PlayMode
```

`-TestPlatform` defaults to `EditMode`. `PlayMode` enters Play mode in batch mode and runs nine
tests: the stream measurement, act, clock, a main-thread stop with a real held key, input release,
the capture self-test, the host's capture status, the JPEG frame window, and the `frame_ref` round
trip. The stream test loads the
first enabled scene from the project's Build Settings additively, makes it active, starts the
loopback server with the Unity sources, then subscribes at 20 Hz and measures: 45-75 observations
in a 3 s window, a 40-67 ms median interval (Windows timer resolution rounds a 50 ms wait to
~62.5 ms in batch mode), no gap over 250 ms, observations at most 150 ms old on arrival, strictly
increasing `seq`, an advancing `frame`, the running scene, `playing`, `time_scale`, a non-null
`ui` object and `reader_errors` as an array. It then unsubscribes and requires silence apart from
at most one in-flight observation. Additive loading keeps the test runner's own scene alive, and
the scene is unloaded during cleanup. A stream test needs at least one enabled scene in Build
Settings, and every run follows the same one-Editor-at-a-time rule.

Both suites need `com.playtest.bridge` in the project's `Packages/manifest.json` under
`testables`; without that entry neither test assembly is compiled or discovered. The PlayMode
assembly lives in `Tests/PlayMode/` and references `Playtest.Bridge`, `Unity.InputSystem` and
`UnityEngine.UI` plus the test framework, so it compiles in the Editor without Editor-only APIs.
PlayMode defaults to a graphics device; pass `-NoGraphics` only when the capture tests cannot run.

Fast transport tests (no Unity, seconds):

```powershell
dotnet test bridge/Tests/DotNet~
```

Unity-only tests are excluded from the fast harness by the `Unity*.cs` naming rule.

The fast harness also runs the executor and the lease against a simulated keyboard, mouse and
gamepad: atomic multi-input apply, device-read verification, a stuck input, step ordering, timer
cancellation and the main-thread rules. It cannot prove behaviour on a real device; the PlayMode
`InputDevicesPlayModeTests` do that and need a Play mode run. The shared
`fixtures/contracts/action_catalog` files are run by `ActionCatalogFixtureTests` once the Python
contract exports them; until then that test reports inconclusive rather than passing.

## Input probe host

`Tests/PlayMode/InputProbePlayModeTests.cs` hosts the bridge for the offline named-declaration
input probe (no model calls). With `UPA_INPUT_PROBE_OUTPUT` unset it only checks that an invalid
catalog is refused. With it set, it loads the first enabled build scene, starts the bridge with the
catalog in `UPA_INPUT_PROBE_CATALOG` (capture included), runs
`python -m playtest.preflight.input_probe --project <project> --plan <plan> --output <output>` with
`UPA_INPUT_PROBE_PLAN`, `UPA_INPUT_PROBE_PYTHON` and `UPA_REPO_ROOT`, and cleans up. The output must
be a new file outside the project. The harness forwards the plan path and never reads the plan.
Only one Editor may run a project at a time, so run it alone.
