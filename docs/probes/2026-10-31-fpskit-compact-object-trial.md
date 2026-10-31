# FPSKit live trial: the compact S1 state's object form on d1-ultra (31 October 2026)

**Question.** Does `--s1-compact-state object` work in a live FPSKit session with the newest working agent
setup, and what does it save?

**Answer.** It works. Both sessions ran with no errors, adherence 1.0 and no stale rejections. On each
session's own contexts S1 was billed **15.5–15.6% fewer tokens** than the night layout (`dup-state-ref/1`)
would have cost, and 28.5% fewer than the default `full` layout. But **both sessions ended after about
45 s, when S1 chose `finish`** after one pass through the controls. The objective says to play the whole
session and to "choose finish only if nothing is left to try", and the coverage fact still listed untried
actions. Whether the compact format causes the early finish is not known (below).

## Setup

The latest working run, `b01-elephant-01`, with S1 switched to d1-ultra and the compact object state:
- **Game:** FPSKit `eval/fpskit-dev/b01` (`e293092`).
- **Inputs:** manifest 2 (`1c1d0970…`, preflight on `eval/fpskit-dev/all` with #217 and #226; 22 executable
  declarations), objective `objective-explore-4.txt`.
- **Options:** coverage facts, game view, frame recording.
- **Frontier:** `muse-spark-1.3` on Meta Standard, at most 4 calls.
- **S1:** `liquid-d1-ultra`, `--s1-layout full --s1-compact-state object`, pacing 420,000 tokens per minute,
  max age 1.0 s, wall limit 180 s.
- **Code:** `feat/s1-optimization` `e106a38`, which is `4966ee7` plus opt-ins; the bridge is identical to
  `origin/main`. FPSKit was rewired to this worktree with `wire-fpskit.ps1`.
- **Approvals:** `runs/s1-trial/approval-trial.json` and `approval-trial-2.json`. The second covers the rerun,
  made because the first session ended at 44 s. Liquid's balance is unverified.

## Results

| | Run 1 (`compact-object-01`) | Run 2 (`compact-object-02`) |
|---|---:|---:|
| Turns (executed + finish) | 23 (22 + 1) | 24 (23 + 1) |
| Turn span | 43.9 s | 44.7 s |
| Decisions per minute | 30.1 | 30.9 |
| S1 latency, p50 / max | 437 / 625 ms | 383 / 844 ms |
| Pacing waits | 19.5 s | 20.7 s |
| S1 billed tokens | 218,311 | 228,609 |
| vs the night layout on the same contexts | **−15.5%** | **−15.6%** |
| vs the `full` layout on the same contexts | −28.5% | −28.5% |
| S1 cost | $0.00917 | $0.00960 |
| Frontier calls | 0 | 0 |
| Adherence; stale rejections | 1.0; 0 | 1.0; 0 |
| Monitor suspicions | 13 | 9 |
| Game checkout changed | no | no |

The "vs" rows price each call's own context under the other layout, using the billing fit in
`docs/design/s1-token-budget.md` (indented state + question block + view). The saving is larger than the
−11.6% offline estimate on night contexts, because these short sessions had no frontier guidance block.

## The early finish

Both runs walked through the controls in one pass, about 20 different actions: movement, jump, crouch, dash,
dash_forward, primary and secondary fire, aim, interact, airborne fire and interaction, turns and looks.
Then each chose `finish` at a near-tie: 0.11 against `move_forward` 0.10, and 0.15 against 0.10. At that
point the coverage fact still listed actions not yet sent.

What this shows about the cause:
- **Possibly not the format.** elephant-1, with the same objective and coverage facts, also chose `finish`
  early, at 1:45.
- **No baseline exists.** d1-ultra had never run with objective 4 and coverage facts before, and the night
  d1-ultra sessions (objectives 1–2, no coverage facts) never finished early.
- **The format could still tip it.** In the text-form bench, compact formats raised `finish` from 1 to 4–6
  calls in 200, and these finishes were near-ties.

A matching control session, the same settings with `--s1-compact-state off` and `--s1-layout dup-state-ref/1`,
would separate the two. It was not run.

Artifacts are local under `runs/s1-trial/` (inputs, approvals, launcher, both session directories with
`run.json`, `trace.jsonl` and recordings). The launcher is `runs/s1-trial/run-trial.ps1`.

## Staircase trial (`climb-stairs-01`, 60 s)

The same setup with three changes: the objective `objective-climb-stairs.txt`, coverage facts off, and the
frontier capped at 2 calls. The objective:
- names the long staircase as ahead and to the right of the start;
- asks S1 to line up, walk into each step and jump only if blocked;
- says not to choose `finish` unless standing on top.

The approval is `runs/s1-trial/approval-trial-stairs.json`.

**S1 reached the staircase and started climbing, then walked off its side and finished early:**
- **Turns 0–8:** a few alternating mouse turns, then `move_forward` diagonally north-east from the spawn
  ([3.58, 0.08, −16.25]).
- **Turns 9–10:** onto the flight, x 8.6–9.9 and z −10.0 to −8.6. The feet height rose 0.08 → 0.40 → 1.02,
  about four 0.2575 risers.
- **Turn 11:** still heading about 38° east of the staircase's axis (north), S1 walked off the side of the
  2-unit-wide flight (x 11.1), and its height fell back to 0.08.
- **Turns 12–16:** north-east on flat ground, with one running jump.
- **Turn 17:** `finish` at 0.23 against `move_forward` at 0.21. Its probability had risen 0.11 → 0.16 →
  0.20 → 0.23 over the last turns.

**Totals:** 18 turns in 30.1 s, 17 executed, adherence 1.0, 0 frontier calls, 13.0 s of pacing. S1 billed
162,741 tokens, 15.5% fewer than the night layout on the same contexts, for $0.00684. Unity was closed
afterwards and the game checkout unchanged.

That is three early `finish` choices in three compact-object sessions, each a near-tie. The `finish` choice
reaches S1 labelled only "Finish", so the completion condition sits only in the objective. Whether the
compact format contributes is still untested; it would take the matching control with the format off.

## Control: the same staircase trial with the compact format off (`climb-stairs-control-01`)

Everything as in `climb-stairs-01` except `--s1-compact-state off` and the night layout `dup-state-ref/1`;
the approval is `approval-trial-stairs-control.json`. **S1 behaved the same way, so the early `finish` is not
caused by the compact format:**
- **Turns 0–9:** ten `look_right` in place.
- **Turns 10–14:** `move_forward` north-east, about 30° off the staircase's axis, then a running jump at
  its foot.
- **Turns 15–16:** on the flight. The feet height rose 0.08 → 1.49 → 2.19, about eight risers, higher than
  the compact run's 1.02, with the running jumps helping.
- **Turn 17:** off the side of the flight (x 10.5), height back to 0.08.
- **Turn 19:** `finish` at 0.27 against `move_forward` at 0.25.

The `finish` probability rose 0.05 → 0.08 → 0.16 → 0.24 → 0.27 once S1 had left the stairs, the same
pattern as the compact run (0.11 → 0.16 → 0.20 → 0.23).

**Totals:** 20 turns in 40.5 s, 19 executed, 0 frontier calls, 24.4 s of pacing. S1 billed 216,460 tokens
(10,823 per call, against 9,041 per call for the compact run on different contexts) for $0.00909.

With one run in each arm this is anecdotal. Three things hold in both arms:
- S1 reaches the flight quickly but never squares up to its axis, so it walks off the side of a 2-unit-wide
  staircase.
- Once it is back on flat ground, `finish` creeps up to a narrow first place, though the objective says to
  finish only on top.
- The `finish` choice is labelled only "Finish".

The early finish belongs to d1-ultra's decisions under this objective, not to the request format.
