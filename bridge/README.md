# Playtest bridge (Unity package)

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
  Tests/EditMode/               EditMode tests (the acceptance check)
  Tests/DotNet~/                Unity-free fast tests; ignored by Unity
```

## Wire protocol

The contracts are the Python models in `src/playtest/contracts/`; `schemas/` is exported from
them. The bridge speaks `upa/0`. Every line is one JSON object, UTF-8, `\n` terminated.

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
packages. The IL scan reports direct field stores and unresolved cases; it does not prove that a
field is configuration or that a method ran. Review its scope, caps and errors before using an
absence as evidence. Snapshot sizes over the transport limit fail explicitly. Full dumps can
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

The command refuses to overwrite a file. For a repeatable batch capture using the first enabled
build scene, see `scripts/unity/capture-preflight.ps1`.

## Actions

`act` executes a named action from a project catalog, not a compiled-in control. The v0 catalog
is `<project>/Temp/playtest-bridge-actions.json`, written by the test harness for now and by
preflight later. It is data and is not committed:

```json
{
  "actions": {
    "example_action": {
      "backend": "input_system",
      "device": "Keyboard",
      "control": "w",
      "default_lease_s": 0.25
    }
  }
}
```

The bridge looks up the action, checks `based_on_seq`, `max_age_s`, the declared `depends_on`
paths and the play state atomically, then applies it. A new act releases the previous lease and
reports it `cancelled`; the new lease reports `running` and then `completed` (or `failed` if
release cannot be verified). Unknown actions, stale observations, changed fields, a non-playing
project and backend failures are rejected with the contract's reject reasons. `lease_s` is capped
at 3600 s. In v0 `params` are free-form and ignored; the catalog's `default_lease_s` or the
request's `lease_s` sets the hold. A `depends_on` on `frame_ref` fails closed until a frame
exists. The v0 backend is Input System keyboard; the action catalog keeps every game-specific
binding out of the package. In batch mode it scopes the Input System focus settings so injected
keys reach the game loop, and restores the previous values on stop; an interactive Editor is left
alone.

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

## Tests

Three layers:

| Layer | What it covers | Command |
|---|---|---|
| EditMode | protocol, transport, observations, lock, status, stop/clock and the fail-closed capture path; `-Graphics` measures the self-test outside play mode | `./scripts/unity/run-editmode-tests.ps1 -ProjectPath "C:\path\to\project"` |
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
