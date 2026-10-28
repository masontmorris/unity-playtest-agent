# Bridge lease deadline investigation (#107)

Local investigation of [#107](https://github.com/masontmorris/unity-playtest-agent/issues/107)
on base `4cbfcca`, with the uncommitted bridge fix described below. Unity 6000.5.4f1,
Windows, FPSKit, the unchanged manifest and calibration from `issue28-fpskit-02`.
No model calls. Editors were serialized; each stage 7 run loaded a fresh scene.
All full reports, logs and test results stay local in `runs/preflight/issue107/`.

## Confirmed cause and measurements

`BridgeActionLease.TryStart` recorded the input start **before** building and emitting
the `running` record, but armed a timer for the **full** requested lease **after**
that synchronous work. The deadline therefore included record construction/emission
time in addition to the requested hold. The release call was not waiting for a frame.

Temporary Stopwatch timestamps bracketed record work, timer construction, callback
entry, and release. Logging happened after the release/end measurement. The
instrumentation was removed before the three acceptance runs.

| Instrumented baseline | Requested | Record work before arming | Timer construction | Callback entry since start | Release call | Applied hold |
|---|---:|---:|---:|---:|---:|---:|
| Stage 7 baseline 1 | 1.2 s | 11.1462 ms | 0.0176 ms | 1292.9735 ms | 1.1722 ms | 1.2944668 s |
| Stage 7 baseline 2 | 1.2 s | 10.9395 ms | 0.0235 ms | 1211.8038 ms | 0.9099 ms | 1.2123396 s |
| Cold action-harness action | 0.4 s | 13.1916 ms | 0.0170 ms | 414.2477 ms | 1.0564 ms | 0.4144717 s |
| Warm action-harness replacement | 0.3 s | 0.1162 ms | 0.0121 ms | 300.5207 ms | 0.0062 ms | 0.3014272 s |

Both local baseline stage 7 reports were `inconclusive`, with input reproduction
false. Baseline 1 also had about 81.8 ms of excess callback scheduling delay beyond
record work plus the requested lease. Its cause was not isolated; it is retained,
not attributed to record emission. Managed timer scheduling remains subject to jitter.
Stopwatch diagnostics and the existing wall-clock measurement are separate clocks.

The repeatable cold/warm difference explains why stage 7 exposed the deadline bug:
its action is the first one on this path. In the existing calibration record, trial 1
(`a`, 0.05 s) already overshot by 12.0722 ms (0.0620722 s applied). The four matching
`w`, 1.2 s trials were trials 74, 82, 90 and 98, long after the path was warm. Their
applied holds were 1.2003762, 1.1999869, 1.2001671 and 1.2018000 s. In the action
harness, record work fell from 13.1916 ms cold to 0.1162 ms warm.

A regression test amplified record work to 350 ms: the old bridge applied a requested
0.8 s for 1.1557867 s (default lease) and 1.1577456 s (explicit lease). Three of the
four new cases failed before the fix; the measured-cancellation case already passed.
This isolates the deadline error independently of the game, frame rate and timer
resolution. No platform timer-resolution change is needed for this correction.

## Fix and scope

The bridge now measures monotonic elapsed time from input start and arms the timer
only for `requested lease - elapsed`. If record work already consumed the lease,
it releases immediately before `TryStart` returns. It still records the actual wall
start and end around the backend release and computes their difference; no reported
duration is substituted or clamped to the lease. An unavoidable overrun remains
visible. The request's explicit lease still overrides the catalog default.

Four new test cases cover explicit/default durations with slow record emission,
already elapsed expiry, and measured cancellation. The timer assertions deliberately
use a wide margin around the amplified defect, avoiding a flaky 10 ms unit-test gate.
The real stage 7 runs retain their existing 10 ms gate.

Actuator audit: the validated action passed to `TryApply` is unchanged; both expiry
branches release that action. Invalid/stale requests, supersede, stop and disconnect
paths are unchanged. No choice, escalation or alternative input is introduced. The
bridge remains generic and Python/core files are unchanged. Diff audit: pass. No S1
turn ran, so live S1 adherence replay is not applicable.

## Three acceptance runs (all retained, no reruns)

The originally reported stage 7 holds were 1.2206274 and 1.2202735 s, overshoots of
20.6274 and 20.2735 ms. After the fix:

| Report | Applied hold (s) | Overshoot (ms) | Input reproduced | Effect comparison | Overall status | Script exit |
|---|---:|---:|---|---|---|---:|
| `stage7-1.json` | 1.2029208 | +2.9208 | true | matched | different | 1 |
| `stage7-2.json` | 1.2020185 | +2.0185 | true | matched | verified | 0 |
| `stage7-3.json` | 1.2014874 | +1.4874 | true | matched | verified | 0 |

Run 1's overall failure is retained: the sampled controller field `m_Jump` was 1
before the action and 0 after reload; calibration had classified it as reload-stable.
Position was restored, and the hold and action effect passed. No reload/verdict rule
was changed to hide that mismatch. Runs 2 and 3 restored all counted fields. The
lease reproduction claim is supported, 3/3; a claim that every whole-stage check
passed would be false. This result alone does not close the Day 4 gate.

Precision elsewhere was preserved: the warm action-harness replacement requested
0.3 s, measured 0.3014272 s before, and logged 0.301 s after (the existing harness
summary rounds to 1 ms). The cold 0.4 s action improved from 0.4144717 s to a logged
0.402 s. The final .NET slow-record cases measured 0.8065923 and 0.8056131 s for
0.8 s requests. Unity's equivalent cases measured 0.8483878 and 0.8099859 s; both
passed the deliberately broad regression bound. These synthetic tests establish the
deadline correction, not a universal 10 ms scheduling guarantee.

## Commands and results

`$game` denotes the explicitly supplied FPSKit project; `$source` is the unchanged
parent run directory `runs/preflight/issue28-fpskit-02`; `$out` is this checkout's
absolute `runs/preflight/issue107` directory. Before each Unity invocation, no Unity
process or project `Temp/UnityLockfile` existed. The game was wired to this checkout
with `scripts/unity/wire-fpskit.ps1` before opening Unity.

```powershell
dotnet test bridge/Tests/DotNet~ --artifacts-path "$env:TEMP/upa-dotnet-issue107"
# 188 passed, 0 failed (includes all new cases).

powershell -ExecutionPolicy Bypass -File scripts/unity/run-editmode-tests.ps1 `
  -ProjectPath "$game" -TestPlatform PlayMode `
  -TestFilter 'Playtest.Bridge.Tests.ActPlayModeTests;Playtest.Bridge.Tests.StopPlayModeTests;Playtest.Bridge.Tests.ClockPlayModeTests'
# 4 passed: action/supersede/stale, main-thread stop, clock, real key release.

powershell -ExecutionPolicy Bypass -File scripts/unity/run-editmode-tests.ps1 `
  -ProjectPath "$game" `
  -TestFilter 'Playtest.Bridge.Tests.BridgeActionLeaseTests;Playtest.Bridge.Tests.BridgeActTests;Playtest.Bridge.Tests.BridgeActEdgeTests;Playtest.Bridge.Tests.BridgeStopTests'
# 40 passed, 0 failed under Unity/Mono, including the new cases.

uv run pytest tests/test_decisions.py tests/test_adherence.py tests/test_name_blind.py -q
# 30 passed. No full pytest suite or version matrix was run.

# Issued separately and serially for n = 1, 2, 3, with no runtime changes between:
powershell -ExecutionPolicy Bypass -File scripts/unity/verify-preflight.ps1 `
  -ProjectPath "$game" -ManifestPath "$source/manifest.json" `
  -CalibrationPath "$source/record.json" -Action walk_forward `
  -OutputPath "$out/stage7-$n.json"
# Results and exit codes are all reported above, including run 1's failure.
```

Final review: no calibration/source records or Python verdict rules were edited;
temporary instrumentation is removed. No commits, pushes or PRs were made. FPSKit
remains wired to this checkout. Its manifest, package lock and TimeManager are the
three expected modified files; nothing was committed in the game project.

## Independent recheck

The orchestrator reviewed the diff and repeated the acceptance check on the same uncommitted fix,
with FPSKit still wired to the worker's checkout. `dotnet test` passed 188 of 188. Three more
stage 7 runs, issued serially with no reruns, wrote to a separate local directory:

| Report | Applied hold (s) | Overshoot (ms) | Input reproduced | Effect comparison | Overall status |
|---|---:|---:|---|---|---|
| recheck 1 | 1.2048659 | +4.8659 | true | matched | verified |
| recheck 2 | 1.2015592 | +1.5592 | true | matched | different |
| recheck 3 | 1.2018866 | +1.8866 | true | matched | verified |

Recheck 2 failed overall for the same reason as run 1 above: `m_Jump` did not return after the
reload. Across the six runs after the fix, every hold reproduced the calibration and every effect
matched; 2 of 6 reloads left `m_Jump` different although this calibration's three reload trials
had restored it. The previous calibration had classified it as not reload-stable, so three
trials do not settle its stability. That is a reload-classification question, not a lease one.
