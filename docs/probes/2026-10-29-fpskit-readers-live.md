# FPSKit: manifest readers and suspicion facts in a live session (2026-10-29)

One 3-minute `playtest-run` session on the FPSKit **base** build, with the manifest's readers installed in every
observation and the watch stating reader lines and suspicions (#125, #37). It checks the plumbing against a real
Editor. It is not a bug-finding result: no seeded bug was present. The full trace stays local (`runs/day6-readers/live-01/`).

## Setup

- Game: `FPSKit-bridge-v0` on its base commit (`25e43fb`), wired to this checkout with `scripts/unity/wire-fpskit.ps1`.
  After the run Unity was closed, no lock file remained, and only the three by-design files were modified.
- Manifest, entity and calibration: the **Day 4 base preflight, reused and not regenerated** (manifest SHA-256
  `78a0449a…`, the same as the Day 4 gate run). The run's plan and summary say so (`manifest_origin`).
- Objective: the Day 4 text, byte for byte (SHA-256 `fb6be333…`). It says the session ends after 10 minutes; this one
  ended after 180 s by the wall limit.
- S1: `mammoth-1`, `-MaxCalls 120`, approval reservation $0.001 a call ($0.12 at most). No frontier calls. Pacing at the
  default 85,000 tokens a minute. S1's billed cost is unknown (the reservation bound is the only figure).

## What it showed

| | |
|---|---|
| Status | `complete` (wall limit), 58 turns: 51 executed, 7 `rejected_stale`; adherence 1.0; release verified |
| Readers | 16 declared, 16 resolved at install; 59 field readings (1 at install, 1 a turn); 0 failed samples; 0 reader errors |
| Observations with readers | 58 of 58 carry all 16 values, none null |
| Readers changing in the trace | `move_vec`, `forces_vec`, `force_mag` every observation; `view_bob` 49 changes; `jump_count` 21; `respawn_a_ttl` and `respawn_b_ttl` 9 distinct values each (−1 at load, 4.62–4.72 after a pickup, resting near 0 afterwards); constant: `movement_vec`, `yaw`, `pitch`, `dash_blend`, `crouch_blend`, `aim_flag`, `aim_blend`, `sliding`, `paused` (S1 only moved forward and back) |
| Reader facts in S1's context | every context has a header line and 16 reader lines (current, previous, range of the earlier readings); 17–22 fact lines in all |
| Reader staleness | the readers were read a median 0.16 s of game time before the observation (maximum 0.22 s) |
| Suspicions raised | 15: 8 `effect_mismatch` (`move_forward` 6, `move_back` 2), 7 `range_exit` (`move_vec`, `forces_vec`, `force_mag` once each; each TTL reader twice), 0 `invariant` |
| Suspicions stated to S1 | 15 of 15, each exactly once, in the context of the turn it was raised (an effect one in the next) with its `trace_seq` |
| S1 calls | 58 (cap 120); 58 counted token measurements, 0 estimated |
| S1 tokens | input median 4,234 a call (min 3,602, max 4,361; Day 4: median 3,329): about +900 a call, +27% |
| Pacing | 13 turns waited, 123.9 s of the 180 s, longest 43.1 s |
| S1 choices | `move_forward` 31, `move_back` 27; no escalation (maximum P(escalate) 0.0017); no help entries |
| Trace | `playtest-trace verify`: problems=0, adherence clean |

## What to read into it

- The plumbing works against a real Editor: the Day 4 manifest's descriptors matched the live scene for all 16 readers.
- The two TTL readers behave on the base build as the gap analysis said: −1 before a pickup, about 4.7 after, then
  resting at about −2e-5 and −2e-4. The literal rule "leaves the range it has shown" raised once at the first pickup
  (−1 to 4.62) and again at a second pickup that reached a slightly higher 4.68. That second one is true by the rule
  and says nothing about a defect.
- The effect allowance (stage 7 formula, 0.057 world units for `move_forward`) flagged 8 of 51 executed actions: 6
  `move_forward` overshoots of 0.07–0.16 against a calibrated 2.008, and 2 `move_back` undershoots of 0.06–0.10 against
  2.004. Stage 7 had already found the same direction on this build (2.065 against 2.008). They are facts about the
  calibration, not about bugs.
- The reader facts cost about 900 input tokens a call. At the pacing limit that is about 22% fewer turns a minute
  (58 turns in 180 s, against 251 in 605 s on Day 4). Shorter lines would buy some of it back.
- S1 saw 15 suspicion facts and never asked for help, as on every earlier run. That is S1's choice. Nothing in code
  opens help, and the objective used here carries no sentence about when to ask.

## Not shown

Only the base build ran. A seeded-bug build, a fresh preflight, a frontier call and a reproduction attempt were out of scope.
