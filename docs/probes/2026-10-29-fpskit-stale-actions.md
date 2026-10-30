# FPSKit: S1's actions stay fresh after #132 (2026-10-29)

Issue #132. On the Day 5 gate run (`docs/gates/2026-10-29-day5.md`), 100 of 146 turns reached the bridge stale.
That included every turn in the second half, so S1 could observe but not act. This note records:
- what caused it;
- the fix (`df9121d`);
- one live validation run against a check fixed before the run.

## Cause

Turn timing in run-02's trace:
- S1's context was built within 2 ms of the observation.
- The action reached the bridge 10–40 ms after S1 answered.

So an action's age is S1's decision time, and that tracked the size of S1's input:

| Run | S1 input tokens | Median S1 call |
|---|---|---|
| 29 Oct reader run | 3.6k–4.4k | 360–390 ms |
| Day 5 gate run-02, first third of calls | 3.6k–5.8k | 422 ms |
| Day 5 gate run-02, rest | 5.8k–5.9k | 484 ms |

That is about 60 ms per 1,000 tokens. The growth was S1's `guidance`, at 0 → 5,553 characters by turn 60:
- each adopted frontier answer stayed in every later context in full;
- the frontier restates every open hypothesis, reworded, in each answer.

Measured at the bridge (observation to act), the age was:

| Run | Median | 95th percentile |
|---|---|---|
| 29 Oct reader run | 0.41 s | 0.65 s |
| Day 5 gate run-02 | 0.52 s | 0.75 s |

The age limit was 0.5 s. A stale rejection there buys no fresher decision: the next turn is just as slow.

## Fix

Details are in `docs/design/turn-loop.md`, "The default age limit".
- **Age limit.** `--max-age-s` and `-MaxAgeSeconds` now default to 0.8 s, from the measured decision latency.
- **Guidance.** It lists a hypothesis in full only when its id is new or its status changed. A same-status
  restatement is listed by id (`restated_hypotheses`). The directive is named by id, not copied.
- **Directive facts.** Every directive's line, with its current or final outcome and reason, stays in S1's facts
  on every turn (bible law 4). Before, the outcome appeared only on its closing turn. After that, S1 saw only a
  copy frozen at `open`.

## Validation run

Setup: one session (`runs/issue132/run-01`, local).
- Same build, inputs, objective (help sentence included), Standard frontier tier and recording as gate run-02.
- New defaults; the wall limit was 600 s.
- Approval: the owner approved $1.05 (300 S1 calls, 15 frontier calls).

**Check fixed before the run:** at most 10% of turns rejected as stale (run-02: 100 of 146, 68%).

| | Gate run-02 (before) | Validation (after) |
|---|---|---|
| Turns rejected as stale | 100 of 146 (68%) | **3 of 82 (3.7%)**: passes |
| Action attempts rejected as stale | 101 of 119 | 6 of 19 |
| Escalations / frontier calls | 3 / 3 | 14 / 14 (cap 15) |
| Directives and outcomes | 3: met, unknown, not_met | 14: 6 met, 8 not_met |
| Guidance at the end | 5,553 chars for 3 answers | 6,223 chars for 14 answers |
| S1 input tokens, median / max | 5,831 / 5,910 | 5,566 / 7,678 |
| S1 call, median / p90 | 484 / 578 ms | 531 / 922 ms |
| Ending | wall limit (600 s) | S1 chose `finish` at turn 82 (about 400 s) |

- **Integrity.** `playtest-trace verify` found 0 problems. Adherence was 1.0, and every held input was verified
  as released at the stop. The game worktree did not change.
- **Recording.** 3,795 frames, none missed, 383.9 s of video.
- **Spend.** Frontier: 14 calls, all HTTP 200, $0.171 measured. S1: 96 calls, billed price unknown, at most
  $0.096 reserved.

## What it shows, and what it does not

- **Stale turns.** The predeclared check passes. The second-half collapse did not recur.
- **Guidance compaction holds up.** Fourteen answers took about 6,200 characters. Run-02's answers averaged
  1,850 characters each, so 14 full copies would be about 26,000. Only 7 hypotheses appeared in full; the rest
  were restatements listed by id.
- **Context still grows, more slowly.** Each escalation adds about 740 characters: its new hypotheses, a guidance
  entry and one directive line. Fourteen escalations took S1's input to 7,678 tokens and its p90 call to 922 ms.
  - 3 of the 6 rejected attempts were continuations chosen while help was pending. Those are bound to an
    observation two S1 calls old (0.98–1.33 s).
  - The other 3 came after the context had grown (0.85, 0.93 and 1.79 s).
  - The frontier call cap bounds this. Longer or more escalation-heavy runs will need windowing, which must
    keep unresolved hypotheses and negative results visible.
- **S1 behaved differently: one run, not a comparison.** It escalated 14 times instead of 3 and waited out much of
  the pending help (52 waits).
  - Its directives now stay in view with their outcomes, and 8 of them were `not_met`.
  - Whether the visible outcomes or its actions now executing changed its behaviour is not established.
  - Escalation remains S1's choice, and nothing in code opens help.
- **Open follow-up.** Several of the 8 `not_met` outcomes may again be untested setpoints: deadlines passing while
  S1 waited. That is #133, which will record whether a directive's test ever executed.
