# Playtest bridge (Unity package)

Loopback JSONL transport between the playtest core and the Unity Editor. This is the
`com.playtest.bridge` package; reference it from a test game's `Packages/manifest.json` with a
`file:` path. It is never copied into the game's `Assets/`.

The bridge is game-agnostic by design: it carries generic verbs and facts, never a title,
control, field or genre. Game knowledge lives only in the preflight-generated manifest, and the
test games are external subjects.

Issues #14–#19 build it in layers:

| Layer | Verb | Status |
|---|---|---|
| Transport, hello/status, project lock | `hello` | Landed (#14) |
| Continuous observation stream | `observe` | Landed (#15) |
| Bounded actions, freshness, input leases | `act` | Landed (#16) |
| Stop, release verification and clock | `stop`, `clock` | This issue (#17) |
| Capture to an offscreen render target | `frame_ref` (part of `observe`) | #18 |
| Core skeleton, bridge client, fake game | — | Landed (#19) |

## Layout

```
bridge/
  package.json                  UPM package manifest
  Runtime/Protocol/             wire DTOs and strict JSON reading (no Unity dependency)
  Runtime/Actions/              action catalog, input lease, clock interface (no Unity dependency)
  Runtime/Lock/                 project lock file (PID, port, token)
  Runtime/Transport/            TCP listener, JSONL framing, token, single session
  Runtime/Unity/                Unity status, observation, input and clock sources
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
request's `lease_s` sets the hold. `frame_ref` is unavailable until capture (#18), so a
`depends_on` on it fails closed. The v0 backend is Input System keyboard; the action catalog keeps
every game-specific binding out of the package. In batch mode it scopes the Input System focus
settings so injected keys reach the game loop, and restores the previous values on stop; an
interactive Editor is left alone.

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

## Tests

Three layers:

| Layer | What it covers | Command |
|---|---|---|
| EditMode | protocol, transport, observations, lock and status | `./scripts/unity/run-editmode-tests.ps1 -ProjectPath "C:\path\to\project"` |
| PlayMode | the continuous stream, measured against a running scene | the same command with `-TestPlatform PlayMode` |
| Unity-free | pure C# transport, seconds, no Editor | `dotnet test bridge/Tests/DotNet~` |

Acceptance (EditMode, run from the test game project):

```powershell
./scripts/unity/run-editmode-tests.ps1 -ProjectPath "C:\path\to\project"
```

PlayMode measurement, against the same project and the same `testables` entry:

```powershell
./scripts/unity/run-editmode-tests.ps1 -ProjectPath "C:\path\to\project" -TestPlatform PlayMode
```

`-TestPlatform` defaults to `EditMode`. `PlayMode` enters Play mode in batch mode and runs one
test that loads the first enabled scene from the project's Build Settings additively, makes it
active, starts the loopback server with the Unity sources, then subscribes at 20 Hz and measures
the stream: 45-75 observations in a 3 s window, a 40-67 ms median interval (Windows timer
resolution rounds a 50 ms wait to ~62.5 ms in batch mode), no gap over 250 ms,
observations at most 150 ms old on arrival, strictly increasing `seq`, an advancing `frame`, the
running scene, `playing`, `time_scale`, a non-null `ui` object and `reader_errors` as an array.
It then unsubscribes and requires silence apart from at most one in-flight observation. The test
sends no `stop`; that verb is a later issue. Additive loading keeps the test runner's own scene
alive, and the scene is unloaded during cleanup. A PlayMode run needs at least one enabled scene
in Build Settings, and the same one-Editor-at-a-time rule as any other run.

Both suites need `com.playtest.bridge` in the project's `Packages/manifest.json` under
`testables`; without that entry neither test assembly is compiled or discovered. The PlayMode
assembly lives in `Tests/PlayMode/` and references only `Playtest.Bridge` plus the test framework,
so it compiles in the Editor without Editor-only APIs.

Fast transport tests (no Unity, seconds):

```powershell
dotnet test bridge/Tests/DotNet~
```

Unity-only tests are excluded from the fast harness by the `Unity*.cs` naming rule.
