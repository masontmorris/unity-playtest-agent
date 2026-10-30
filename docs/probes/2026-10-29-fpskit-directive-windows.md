# FPSKit: directive windows and deadlines that start when S1 sees them (2026-10-29)

Issue #133. On the Day 5 gate run, directive `d-38` was scored `not_met` while all three attempts in its window
were rejected as stale. The record could not tell "tested and not met" from "never tested". This note records
what the window check found in the earlier traces, the fix (`36cae45`), and one live validation run.

## What the earlier traces showed

Each directive's window was rebuilt from the trace: the turns from the first context that stated it up to its
outcome.

- **Day 5 gate run-02.** `d-38` had 2 inputs sent, 0 completed and 2 rejected as stale. The new record would
  classify it `action_rejected`.
- **#132 validation run.** 13 of 14 directives had **no turns at all** in their window. They were scored on the
  same turn S1 first saw them.
  - 11 of 14 deadlines had already passed by then.
  - Deadlines were 5–15 game seconds after the escalation, but the answer reached S1 up to 50 s later: the
    frontier call plus the next turns' pacing.
  - So 8 `not_met` outcomes were untested, and 5 `met` outcomes came with no input from S1.

The deadline counted from the escalation's observation. `to_directive` documents "from when S1 received it".

## Fix

Details are in `docs/design/frontier-runner.md`, "When a directive's test never ran".
- **Deadline.** The frontier's relative deadline now counts from the observation that first states the directive
  to S1.
- **Window.** A closing directive records a `window` (`DirectiveWindow`):
  - turns;
  - inputs sent and completed;
  - inputs rejected, by the bridge's reason;
  - the rest (failed, cancelled, timed out, unrecorded);
  - waits and escalations.
- **Untested `not_met`.** One whose window completed no input gets `error_class` `no_attempt` or
  `action_rejected`.
- **Untested outcomes in general.** Any outcome with no completed input says so in its reason.
- **Where it reaches.** S1's fact line, the next frontier request and the trace all carry it.

## Validation run

Setup:
- One session (`runs/issue133/run-01`, local), with the same build, inputs, objective, Standard frontier tier and
  recording as the #132 validation run.
- Approval: the owner approved $1.05.

**Checks fixed before the run:**
1. Every closed directive's record has a window.
2. No deadline passes before S1 first sees its directive (the #132 run had 11 of 14).

| | #132 validation (before) | #133 validation (after) |
|---|---|---|
| Directives closed | 14 | 5 |
| Window recorded | none | **5 of 5**: passes |
| Deadline passed before S1 saw it | 11 of 14 | **0 of 5**: passes |
| At least one input completed in the window | 1 of 14 | 2 of 5 |
| Outcomes | 6 met, 8 not_met | 5 met |

- **The three `met` outcomes without input.** Their setpoints already held when S1 first saw the directive: the
  frontier asked for a value that was already true. Each record says "no input completed in its window", with a
  window of 0 turns. Before this change, such a `met` looked like a success of the directive.
- **The two tested ones.** `d-60` sent 3 inputs: 2 completed and 1 was rejected as stale. `d-84` sent 1 input,
  which completed.
- **Integrity.** The session was `complete`, and S1 chose `finish` at turn 105 (about 418 s). Adherence was 1.0,
  `playtest-trace verify` found 0 problems, every held input was verified as released, and the game worktree
  did not change.
- **Recording.** 3,953 frames, none missed, 399.6 s of video.
- **Spend.** Frontier: 5 calls, all HTTP 200, $0.052 measured. S1: 111 calls, billed price unknown, at most
  $0.111 reserved.

## Not this change's result: stale actions went back up

Stale rejections reached 31 of 105 turns, and 34 of 78 action attempts. The #132 run had 3 of 82 turns. The context
was not the cause: S1's input was smaller than on the #132 run.

| | #132 validation | #133 validation |
|---|---|---|
| S1 input, median / max tokens | 5,566 / 7,678 | 5,050 / 6,185 |
| S1 call, median | 531 ms | 594 ms |
| S1 call, p90 | 891 ms | 1,375 ms |
| S1 call, p95 | 1,109 ms | 1,657 ms |
| Median call, by third of the run | — | 515, 484, 875 ms |

Baseten's latency varies from run to run, and the 0.8 s age limit holds only while it stays low. That is #136.
The directive windows make the effect visible: `d-60`'s record counts its one stale rejection.
