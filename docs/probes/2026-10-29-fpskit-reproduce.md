# FPSKit: replaying a frontier candidate finding (#38)

A live check of `reproduce` (`docs/design/reproduce.md`). It made no model calls.

- **Game.** FPSKit `25e43fb` (base build, no seeded bug), Unity `6000.5.4f1`, wired to this checkout. The
  manifest was the Day 4 base manifest (`78a0449a…`), the one the source session used.
- **Finding.** The #132 validation session (`runs/issue132/run-01`, local) predates candidate-finding
  recording. Its frontier answer at request turn 17 proposed "move_forward blocked with zero displacement
  against wall", with `repro_check` `public.movement_vec unchanged`, citing bridge seq 83. That answer was
  only in the call ledger. A local copy of the trace has it appended as a `finding` entry, mapped the way
  `FrontierHelp` now maps it: bridge seq 83 to trace entry 133. The original trace is unchanged.
- **Plan.** 8 inputs completed before trace entry 133 (all `move_forward`, 0.4 s leases, 15.1 s from first to
  last). 2 rejected inputs were skipped. The evidence observation came 15.67 s after the first input.
  `reproduce` runs one check per run, so the same plan was run three times: once with the finding's check,
  and twice with an operator check given by `-ReproCheckPath`. Each run made 3 attempts.

## Results

| Run | Check | k / n | Unknown | Result |
|---|---|---|---|---|
| a | `movement_vec unchanged` (the finding's) | 0 / 0 | 3 | inconclusive: the check held after every reset, before any input |
| b | `movement_vec changed` (operator) | 0 / 3 | 0 | not reproduced: the vector read `[0, 0, 0]` at all 48 readings |
| c | `jump_count changed` (operator) | 3 / 3 | 0 | reproduced: 0 after each reset, then 1 during the replay |

- **Resets.** 9 scene reloads, all completed. The host kept serving numbered reloads within one PlayMode
  session.
- **Inputs.** 72 of 72 replayed inputs completed with release verified. Each started within 0.028 s of
  its recorded offset. No attempt diverged.
- **Run b is not a failed replay.** The source session read `movement_vec` as `[0, 0, 0]` at all 18
  observations through the evidence. The reader is zero whenever no input is held, and `reproduce`, like
  the session, reads between inputs.
- **Run c matches the source session.** There, `jump_count` moved between 0 and 1 seven times before the
  evidence, with no jump input. The replay showed the same change in every attempt.

## What this shows

The reset, the replay at recorded offsets, the readers and the k/n bookkeeping work live on FPSKit. A
frontier candidate whose check holds at rest ends as `unknown`, not as a reproduction.

This is not #38's done-when: the base build has no seeded bug. That needs a candidate finding from a session
on a seeded build, which is the Day 6 gate (#41).
