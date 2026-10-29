# FPSKit mouse, gamepad, chords and skills

Offline validation for [#110](https://github.com/masontmorris/unity-playtest-agent/issues/110)
and [#111](https://github.com/masontmorris/unity-playtest-agent/issues/111): all 12 named-declaration
attempts completed with exact input receipts and verified release. No model calls were made
or paid for. This was a caller-authored measurement plan, not an S1 turn or a model-selected
preflight run.

## Provenance

- Bridge checkout: `feat/input-actions`, base `a3bad76`, with uncommitted implementation changes.
  The local `implementation-state.json` records file hashes.
- FPSKit: MIT, commit `25e43fbf4cc0`, Unity `6000.5.4f1`, Input System `1.19.0`.
- Protocol: `upa/1`; one bridge session. First enabled scene loaded once, with no reset between
  attempts. The twelve actions had unique IDs and their terminal freshness references matched
  the exact before-observation sequence. No retry, fallback or substituted declaration was used.
- Plan: three repeats each of `look`, `chord`, `skill`, and `axis`, declared before the run.
  Zero model calls were stated before execution; cost for this probe was $0.
- Local artifacts: `runs/input-actions/fpskit-20261028-01/`. `probe.json` SHA-256:
  `19dad64721a7c82a1f0ad174991001ac7dd9335caee822cc9fdf2bc0d42b3e07`.

## Measurements

The counts below mean completed exact-declaration executions, not reproduced game defects.
Displacement is the raw before/after position difference, including the plan's post-input
settle interval; no idle-baseline subtraction or physical-unit conversion is claimed.

| Declaration | Result | Measured effect | Measured program duration |
|---|---|---|---|
| Mouse delta `(0.125, 0.0625)`, one `(8, 4)` pixel impulse; 0.15 s lease | 3/3 | Entity yaw +0.959962Â° each time; zero positional displacement | 0.149944â€“0.157525 s |
| Keyboard `w+a`, 0.2 s hold | 3/3 | 1.003660â€“1.049445 Unity world units | 0.200370â€“0.201541 s |
| `w` for 0.2 s, release, wait 0.1 s; `w+a` for 0.15 s, release, wait 0.05 s; bound 1.5 s | 3/3 | 1.622755â€“1.687941 Unity world units | 0.501925â€“0.513157 s |
| Gamepad `leftStick=(0.5, 0.25)`, 0.2 s hold | 3/3 | 0.543731â€“0.636402 Unity world units | 0.199962â€“0.201411 s |

The look action's scalar yaw reader changed by approximately +0.959962 and its pitch reader
by âˆ’0.333313 in each repeat. The entity body's transform pitch stayed zero; this does not
independently measure camera pitch. Each mouse receipt read back exactly `(8, 4)` pixels.
Pointer duration remains null, as an impulse's neutral-reset instant was not timed. Held-key
and axis durations came from device apply/release observations, not configured holds.

All chord receipts had common apply and release timestamps. The skill's receipts covered
both steps in order, the measured inter-step wait and the final wait. Every terminal record,
every receipt, each independent fresh neutral-state sample, and final stop verified release.
The before/after runtime-error samples were active and reported zero observed errors.

There are 24 archived frame references at 640Ã—480, backed by 22 distinct image files after
hash deduplication. Every file survived Editor exit and matched its SHA-256. Frame records
identify the bridge composite capture, consumer, sequence, frame, game time and wall time.
These are input/effect evidence, not an independent render-defect diagnosis.

## Manifest proof

`build-probe-manifest.py` derives a caller-authored integration manifest from the frozen catalog
and discovered physical bindings. It does not invoke or impersonate stages 4â€“6. Its individual
effect calibrations, freshness, dead zones, sensitivity and reset remain explicitly unknown.

Before attaching execution evidence, its executable catalog is empty. Applying the saved real
receipts enables exactly `look`, `chord`, `skill`, and `axis`. A changed, untested negative look
declaration remains unsupported and is excluded from `supported-catalog.json`. The local
`manifest-proof-review.json` records this check. This verifies evidence binding/export; it is
not evidence that a model would select these declarations or complete a game objective.

## Ownership review

Verdict: pass for the changed actuator paths. The support gate exposes proven declarations;
it does not choose among them. No escalation or pending-help policy changed.

| Path | Binding/outcome | Source |
|---|---|---|
| Selected action | Exact selected name and params; configured action lease | `src/playtest/core/execution.py:46` |
| Selected skill | Exact selected name and params; default action lease does not replace its bound | `src/playtest/core/execution.py:62` |
| Invalid/stale request | Rejected before device execution; no replacement choice | `bridge/Runtime/Transport/BridgeServer.cs:836` |
| Accepted device failure | One failed terminal record with available receipts and cleanup evidence | `bridge/Runtime/Actions/BridgeActionLease.cs:654` |
| Stop/supersede/expiry | Cancel/release the selected program; no later step or replay | `bridge/Runtime/Actions/BridgeActionLease.cs:258` |

`tests/test_execution_binding.py` catches skill-bound replacement and repair of a rejected
request. The existing decision/adherence/loop checks also passed. Frame-enabled observation
adds capture evidence; no history/window/context shortening was introduced. Live S1 trace
adherence remains not applicable to this offline probe.

## Checks and limits

- `uv run playtest-export-schemas`; `uv run pytest`: 829 passed, including contract drift,
  intended invalid-fixture rejection, name blindness and ownership checks.
- `dotnet test bridge/Tests/DotNet~/Playtest.Bridge.DotNetTests.csproj --nologo`: 352 passed,
  zero skipped, including the shared catalog/execution fixtures.
- Unity EditMode: 401/401; PlayMode: 20/20; opt-in FPSKit probe harness: 1/1.
- The first EditMode run was 399/401. Both failures are retained in `editmode.xml`: a Mono
  floating-point equality assumption and a wall-clock timer-jitter assertion. The first now
  uses a 1 Âµs precision allowance; the second checks that scheduled delay deducts record work,
  while retaining measured duration and timestamp checks. `editmode-02.xml` is the passing run.
- The external project's package manifest, package lock and time settings were restored
  byte-for-byte. Its final git status exactly matched the saved initial status.

Three repeats in one batch-mode session do not establish general reliability, interactive
focus routing or physical-controller compatibility. No full model-driven preflight, S1 game
run, objective completion, defect finding or reproduction claim is made. Adherence review
of S1 turns is not applicable. Unprobed directions, bindings and response curves stay unknown.
