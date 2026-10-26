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
| Transport, hello/status, project lock | `hello` | This issue (#14) |
| Continuous observation stream | `observe` | #15 |
| Bounded actions, freshness, input leases | `act` | #16 |
| Capture to an offscreen render target | `capture` (part of `observe`) | #17 |
| Stop and release verification | `stop` | #18 |
| Pause, step and time scale | `clock` | #19 |

## Layout

```
bridge/
  package.json                  UPM package manifest
  Runtime/Protocol/             wire DTOs and strict JSON reading (no Unity dependency)
  Runtime/Lock/                 project lock file (PID, port, token)
  Runtime/Transport/            TCP listener, JSONL framing, token, single session
  Runtime/Unity/                Unity snapshot for the status reply
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

## Tests

Acceptance (EditMode, run from the test game project):

```powershell
./scripts/unity/run-editmode-tests.ps1 -ProjectPath "C:\path\to\FPSKit-bridge-v0"
```

Fast transport tests (no Unity, seconds):

```powershell
dotnet test bridge/Tests/DotNet~
```

Unity-only tests are excluded from the fast harness by the `Unity*.cs` naming rule.
