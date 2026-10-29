# FPSKit on today's main: fresh preflight and the Day 4 gate re-run

On 29 October 2026 `mammoth-1` played FPSKit through the production turn loop and the
manifest-catalog session runner (`playtest-run`, #109) for one 10-minute gate run on `main`
`9b423d0`. The run used a fresh same-day preflight (new survey, discovery, entity, calibration,
frame and contributor manifest), exact execution proof for 15 of 17 model-selected declarations
from a zero-model-call input probe, and the same generic objective as the 28 October `gate-02`
run. The run wrote its run record and an append-only trace with the #30 writer, and the trace
replays with no problems. This records what the runs measured; it is not a benchmark of play
quality, and the comparison with `gate-02` below is an unpaired observational delta, not a
controlled A/B: code, manifest, holds and S1's choices all differ.

## Day 4 gate (#32)

"A 10-minute FPSKit run: no crashes, 100% adherence, no code-selected actions, every action with
expected vs observed effect." Judged on `session-gate`:

| Criterion | Result |
|---|---|
| 10-minute run | 605.6 s first observation to stop; the session ended on its wall limit |
| No crashes | status `complete`; the stop released input and the bridge verified it |
| 100% adherence | 251 of 251 turns, 0 violations from `check_adherence` |
| No code-selected actions | `playtest-trace verify` re-derived every turn from its entries: 0 problems |
| Expected vs observed on every action | 185 of 185 executed actions carry both, and they agree (below) |

All five hold as written, and the fifth holds in the strong sense this time: 173 of 185 agree
within 0.5 world units and none differs by more than 5.

## Runs

| | `session-gate` | `gate-02` (28 Oct, for reference) |
|---|---:|---:|
| Wall limit | 600 s | 600 s |
| Ending | wall limit | wall limit |
| Status | complete | complete |
| Turns | 251 | 411 |
| Executed | 185 | 286 |
| Rejected as stale (`stale_age`) | 66 | 124 |
| Rejected, no usable answer | 0 | 1 (transport error) |
| Adherence violations | 0 | 0 |
| Replay problems | 0 | 0 |
| Effects with expected and observed | 185/185 | 286/286 |
| ...within 0.5 world units of calibration | 173 | 22 |
| First turn more than 5 units off | none | 30 |
| S1 calls (cap) | 251 (500) | 411 (500) |
| S1 latency median / p90 / max | 329 / 844 / 7,594 ms | 281 / 688 / 21,094 ms |
| Tokens counted (input + output) | 830,524 | 786,285 |
| Peak tokens in any 60 s | 83,756 | 84,702 |
| Pacing waits | 77 turns, 382.5 s (longest 41.7 s) | 39 turns, 31.1 s (longest 7.8 s) |
| Choices | move_forward 95, move_back 82, jump 8 | walk_forward 153, strafe_left 170, strafe_right 87 |

The preflight chain ran the same day: survey and discovery with 0 errors and nothing truncated
(312 fields, 26 bindings offered), entity `identified`, calibration `measured_with_gaps` (23
controls, 254 trials in 304.8 s of a 1,032.9 s budget), a 640x480 game frame from a separate
caller-authored probe session, and stages 4-6 in 4 contributor calls (the first actions attempt
was rejected on validation: two axis inputs needed a hold time). The manifest selects 16 readers,
14 actions, 3 skills, no progress fields and 3 invariants. Stage 7 on `move_forward` returned
`different` (below); the input probe then proved 12/12 actions and 3/3 skills with zero model
calls, and the updated manifest validates with 15 executable declarations.

## Findings

### The post-turn-30 divergence is gone

In `gate-02` the first 22 holds measured within 0.5 of calibration, then every hold measured
74-86 against a calibrated 6.0 to the end of the run. Here all 185 executed actions measured
near their calibration for the whole 10 minutes: move actions at 2.00-2.06 against 2.00-2.01
calibrated, a largest single difference of 0.8, and no turn more than 5 off. The 74-unit pattern
never appeared.

Why it went away is **unknown**. Candidates are the #108 lease fix, the fixed calibration (#122),
the shorter model-selected holds (0.4 s here against 1.2 s there), and S1's own behaviour: it
alternated forward and back, so the entity oscillated instead of drifting steadily the way
`gate-02`'s walk/strafe mix did. This run shows the pattern absent under the new stack; with one
run and all of those changing at once, it does not prove which change mattered, or that the old
pattern could not recur.

### Jump measures low against its calibration, steadily

All 8 jumps measured 0.44-0.52 against a calibrated 1.104 for the 0.05 s `space` tap, a steady
~0.66 offset with no drift. Both values are measured; the cause of the gap is unknown. It is
bounded and stable, unlike the `gate-02` blow-up.

### S1 used three single keys and nothing else

Of 15 offered executable declarations (4 single keys, jump, 2 gamepad moves, 2 chords, 3 mouse
looks, 3 skills) plus wait, escalate, delegate and finish, S1 chose only `move_forward` (95),
`move_back` (82) and `jump` (8). Input kinds used: keys 185; mouse look 0; chords 0; skills 0;
gamepad 0. The objective asked it to try each control in different places; it never chose wait,
escalate, delegate or finish either, so the help path, `pending_help` and `while_pending` remain
unexercised live, as in `gate-02`. Every executed action was a single control at a calibrated
hold, which is why all 185 carry a calibrated expectation; chords, skills and unmeasured values
have none by design, but none was chosen.

### Stage 7 missed by 0.0001 with a rotated basis

Stage 7 on `move_forward` (first eligible action in manifest order) returned `different`: the
0.4 s hold measured 2.065 against a calibrated 2.008 with a predeclared tolerance of 0.0572, a
miss of 0.0001. Input was reproduced (0.405 s against 0.400-0.403) and the scene reload restored
the entity position exactly (0.0). The report also finds the live entity basis rotated relative
to the calibration trials, so spawn orientation varies between scene loads. A near-miss on a
tight tolerance, recorded as evidence, not a pass.

### Stale rejections follow S1 latency, as before

66 of 251 turns (26%) were rejected as `stale_age` under the 0.5 s window. Stale turns' S1 calls
took a median 727 ms (minimum 391); executed turns' calls took a median 313 ms (maximum 469).
The split at the window edge is clean, matching `gate-02`.

### Pacing held under a heavier context

The 19-choice context costs a median 3,330 tokens a call against `gate-02`'s 1,936, so the
runner waited 77 times for 382.5 s in total (longest 41.7 s), peaked at 83,756 tokens in a 60 s
window and received no 429. All 251 calls returned HTTP 200 with no problem; none was a
`while_pending` follow-up.

### Not exercised

Mouse look, chords, skills, gamepad moves, strafe, wait, escalate, delegate and finish were
offered and never chosen. `interact` and `fire` were never offered: the manifest gives them no
hold, so no exact declaration exists to probe or execute; they stay `execution_supported: false`
for a later probe.

## Cost and accounting

Preflight stages 4-6 made 4 `muse-spark-1.3-contributor` calls (12 approved) with an estimated
token cost of **$0.050883** ($0.0244 + $0.0009 + $0.0247 + $0.0009; prompt 242-246k tokens, two
calls cache-hit); provider-reported cost is unavailable. The session made 251 S1 calls (cap 500).
Baseten returned no cost field, so the billed cost is **unknown**; the reservation bound was
$0.001 a call, $0.251 in all. The Baseten balance was not verified. No frontier or Meta model
was called during the session. The API key appears in none of the retained files (the writer
counted 0 redactions).

## Inputs and provenance

- Code: `main` `9b423d0` (with the #108 lease fix, executable mouse/gamepad/chord/skill input
  #115, the manifest-catalog session runner #109 and fixed calibration #122). Prompt SHA-256
  `09c230bac3612523c5f6c6dd7ba675d8d2db318bc77ce94d433a7cbfaa1d4895`, identical to `gate-02`.
- Game: FPSKit `25e43fb`, Unity 6000.5.4f1, scene `FPSKit_TestScene`, input `input_system`,
  bridge 0.1.0. The worktree carried only its three by-design modified files (bridge wiring,
  package lock, fixed-timestep re-serialization) and reports no change from the run.
- Manifest SHA-256 `0cabbc1bdeb5a2df…` at selection, `78a0449a285700f5…` after the input probe;
  entity record `de5cea51abc962fc…`; calibration record `e47405eed18a6a00…`; served catalog
  `d1df731107dea5e4…` (15 declarations); screenshot `97a1bba5ad14b45b…` (640x480, game frame
  4323 at 1.98 s, separate probe session).
- Objective: "Explore the level for the whole session using only the offered choices: move
  around, try each control in different places, and watch what changes. The session ends by
  itself after 10 minutes; choose finish only if nothing is left to try."
- Approval: 500 S1 calls at $0.001 reservation ($0.50), balance unverified per the run brief.
- Traces (local only): `session-gate` `adcadc60335fb742…`. The trace, call ledger, summary and
  Unity log stay under `runs/day4-main/`, with the preflight records, probe outputs and this
  run's analysis helpers.
