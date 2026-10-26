# Playtest bridge (Unity package)

Loopback JSONL transport between the playtest core and the Unity Editor. This is the
`com.playtest.bridge` package; reference it from a test game's `Packages/manifest.json` with a
`file:` path. It is never copied into the game's `Assets/`.

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
batch mode. Use `Tools/Playtest Bridge/Start`, `Stop` and `Status` to control it. The lock file
appears in `Temp/` once it is listening.

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
