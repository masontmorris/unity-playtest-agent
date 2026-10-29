# FPSKit live session: the Day 4 gate run

On 28 October 2026 `mammoth-1` played FPSKit through the production turn loop and the live session
runner (`playtest-run`, #29) for one 2-minute pilot and two gate attempts. Every run wrote its run
record and an append-only trace with the #30 writer, and every trace replays with no problems. The
second gate attempt ran the full 10 minutes. This records what the runs measured; it is not a
benchmark of play quality.

## Day 4 gate (#32)

"A 10-minute FPSKit run: no crashes, 100% adherence, no code-selected actions, every action with
expected vs observed effect." Judged on `gate-02`:

| Criterion | Result |
|---|---|
| 10-minute run | 601.4 s of turns; the session ended on its wall limit |
| No crashes | status `complete`; the stop released input and the bridge verified it |
| 100% adherence | 411 of 411 turns, 0 violations from `check_adherence` |
| No code-selected actions | `playtest-trace verify` re-derived every turn from its entries: 0 problems |
| Expected vs observed on every action | 286 of 286 executed actions carry both, but they disagree badly after turn 30 (below) |

All five hold as written. The fifth holds only in that both values were recorded and shown to S1;
see "Observed effects diverge after about 22 holds", which is the main finding.

## Runs

| | `pilot-01` | `gate-01` | `gate-02` |
|---|---:|---:|---:|
| Wall limit | 120 s | 600 s | 600 s |
| Ending | wall limit | error at 177 s | wall limit |
| Status | complete | inconclusive | complete |
| Turns | 81 | 125 | 411 |
| Executed | 77 | 107 | 286 |
| Rejected as stale (`stale_age`) | 4 | 18 | 124 |
| Rejected, no usable answer | 0 | 0 | 1 (transport error, turn 208) |
| Adherence violations | 0 | 0 | 0 |
| Replay problems | 0 | 0 | 0 |
| Effects with expected and observed | 77/77 | 107/107 | 286/286 |
| ...within 0.5 world units of calibration | 22 | 23 | 22 |
| First turn more than 5 units off | 26 | 24 | 30 |
| S1 calls (cap) | 81 (100) | 125 (500) | 411 (500) |
| S1 latency median / p90 / max | 265 / 329 / 938 ms | 265 / 516 / 750 ms | 281 / 688 / 21,094 ms |
| Tokens counted (input + output) | 155,846 | 240,413 | 786,285 |
| Peak tokens in any 60 s | 80,438 | 84,654 | 84,702 |
| Pacing waits | none | 3 turns, 0.9 s | 39 turns, 31.1 s (longest 7.8 s) |
| Choices | walk_forward 42, strafe_left 38, strafe_right 1 | 48 / 58 / 19 | 153 / 170 / 87 |

`gate-01` stopped at 180 s on the Unity Test Framework's default `UnityTest` timeout, which ended
Play mode under the running session; the next inspection was refused ("the requested stage requires
Play mode"). Its 125 turns are adherence-clean and replay with no problems. The host test now carries
a `Timeout` longer than its own 1,800 s deadline (`230ef65`). When the timeout ended Play mode, Unity
re-saved `ProjectSettings/TimeManager.asset` (the same 0.02 s fixed timestep), so that run also reports
a changed game worktree. `pilot-01` and `gate-02` ended normally and report none.

## Findings

### Observed effects diverge after about 22 holds

In all three runs the first 22 or 23 holds measured within 0.5 world units of their calibration (for
example 6.02 against a calibrated 6.00 for a 1.2 s hold). Then one hold measured about 37 units, and
every hold after it measured 74–86 against a calibrated 6.0, to the end of the run. In `gate-02` that
began at turn 30, about 45 s in. The applied hold stayed 1.2 s and release was verified each time.

S1 was given the mismatch as a fact each turn ("net displacement between inspection seq 146 and 150:
74.240 world units; calibrated for a 1.2-second hold: 6.002"). It kept choosing to walk and strafe and
never chose wait, escalate, delegate, jump or finish. Code did not act on the mismatch, as law 2
requires.

The cause is **unknown**. The pattern fits the entity leaving the walkable level and moving fast and
steadily, such as falling at a capped speed, but the probe records only the magnitude of net
displacement between two inspections, not positions, so the direction is not in the record. It recurs
at a similar point in every run, each from a fresh scene load, so it is systematic rather than noise.
Recording the before and after positions in the effect entry would settle it.

### Stale rejections follow S1 latency

124 of 411 `gate-02` turns (30%) were rejected as `stale_age` under the 0.5 s window, against 4 of 81 in
the pilot. Stale turns' S1 calls took a median 547 ms (minimum 265); executed turns' calls took a
median 250 ms (maximum 469). The rejections cluster in the minutes when Baseten's latency rose (p90
718–907 ms there, 437–516 ms elsewhere). No input was sent for a rejected action and nothing was
substituted. This is the S1 decision record's latency condition (`docs/design/s1-model.md`), seen live.

### Pacing kept the token rate under the cap

Baseten caps `mammoth-1` at 100,000 tokens a minute. The runner paced to 85,000: `gate-02` waited 39 times
for 31.1 s in total, peaked at 84,702 tokens in a 60 s window and received no 429. Calls took a median of
1,936 tokens. One call failed with a transport error after 21 s; its turn was rejected and not retried.

### Not exercised

S1 never chose wait, escalate or delegate, so the help path, `pending_help` and `while_pending` were not
exercised live. They are covered only by the fake-game tests. Jump, step_forward and jump_held were
offered and never chosen.

## Cost and accounting

617 S1 calls in total: 81 for the pilot (approval cap 100), 125 for `gate-01` (cap 500) and 411 for
`gate-02` (cap 500, approved separately after `gate-01`). Baseten returned no cost field, so the billed
cost is **unknown**; the reservation bound was $0.001 a call, $0.617 in all. The Baseten balance was not
verified. No frontier or Meta model was called. The API key appears in none of the retained files (the
writer counted 0 redactions).

## Inputs and provenance

- Code: branch `feat/turn-loop-trace`, runner at `6ff088e` for `pilot-01` and `gate-01`, `230ef65` for
  `gate-02`. Prompt SHA-256 `09c230bac3612523c5f6c6dd7ba675d8d2db318bc77ce94d433a7cbfaa1d4895`.
- Game: FPSKit `25e43fb`, Unity 6000.5.4f1, scene `FPSKit_TestScene`, input `input_system`, bridge 0.1.0.
  The worktree carried its usual three modified files (bridge wiring, package lock, and Unity's own
  re-serialization of the fixed timestep), plus the Test Framework's temporary init scene while running.
- Manifest, reused from the #28 preflight: SHA-256 `e7694bed9274ebb0bef52a2dc26cf6a58f4b728d011735427ee97387b2692fa7`;
  entity record `ff8e79bc197500b82ed38b49ea41f09005d9aa4cb8a4c11bd6b9c6855c278ca7`; calibration record
  `5d8bd091da5dafe84b7cde5c0e031ad3166eec93e15f65f2322875445a62debb`. Its calibration predates the #108
  lease fix. Seven executable actions were offered; three press actions and two skills were not, because
  the bridge cannot run them.
- Objective: "Explore the level for the whole session using only the offered choices: move around, try
  each control in different places, and watch what changes. The session ends by itself after 10 minutes;
  choose finish only if nothing is left to try." (The pilot's said 2 minutes.)
- Approvals: pilot `04bb5b2627218088f4da03494dcb19fbb7d4ea17f8e82025f568f76ff460265b`, gate
  `f9bd430ab46a794b9f2282df53dbd4cf95b7b56cd9b748d026895d48a6082e87`, gate re-run
  `6e9c3c7b5f88b9cd145c0a518f8a818c6798373bec08abd6107312323d3e13b4`.
- Traces (local only): `pilot-01` `21127c1a706cfeac…`, `gate-01` `d3ab862de4a527c5…`, `gate-02`
  `1a45e27d71c20915…`. The traces, call ledgers, summaries and Unity logs stay under `runs/`.
