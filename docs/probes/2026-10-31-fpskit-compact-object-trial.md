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

## Staircase rerun with every change on (`climb-stairs-02`, 60 s)

The same objective and settings as `climb-stairs-01`, plus:
- `--pacing-burst 0.5`;
- HTTPS connection reuse;
- `--finish-label conditional` and `--s1-instructions policy/2`, which the finish-wording test supports.

The approval is `approval-trial-stairs-2.json`.

**It ran the whole minute. The ending was `wall_limit`, and `finish` never rose above 0.14.**
- **Speed:** 43 turns in 59.8 s, **42.2 decisions a minute** against 30–31 before. S1 p50 latency was
  297 ms (383–437 ms before), with 14.0 s of pacing.
- **Climb:** turns 10–14 walked toward the flight. A running jump at turn 15 got onto the stairs (feet
  2.28), and a second at turn 17 reached **feet height 5.32** at turn 18. That is about 20 risers, against
  1.02 and 2.19 before; the top is about 7.3.
- **Then:** still about 23° off the flight's axis, S1 walked off the side at turns 19–21. It escalated
  twice, then chose `restart_play_mode` at turn 26.
- **Stuck:** on the second approach it alternated `mouse_turn_left` and `mouse_turn_right` in place for 9
  turns, as it had for its first 9 turns, then escalated and waited out the minute.
- **Help:** both frontier requests were cancelled, one by the restart and one by the session's end, before
  an answer arrived. Help cannot return within a 1-minute session.

**Totals:** 46 S1 calls, $0.0176; 2 frontier calls cancelled, with no cost recorded. The recording has
677 frames, none missed. Unity was closed afterwards.

**What is left:** S1 never squares up to the staircase's axis, and it flips between left and right turns
in place for 18 of 43 turns. Both are decision problems, not speed problems. A continuous-input design is
in progress on a separate branch.

## Continuous-input trial (`climb-continuous-01`, 180 s)

Settings:
- **S1:** `--continuous-input` and `--s1-compact-state object`, with `--pacing-burst 0.5`, pooled HTTPS,
  `--finish-label conditional` and `policy/2`.
- **Objective:** `objective-climb-stairs-3min.txt`.
- **Frontier:** at most 3 calls.

The approval is `approval-trial-continuous.json`. The first launch failed before Unity started, because of
a bug in the trial launcher (a switch was splatted as a string), and cost nothing.

**Mechanism: it works.** There were 26 leased holds with leases of 0.1–5.97 s, and 16 of 16 release stops
were verified.

**Behaviour:** worse precision than the 1-minute run.
- **Speed:** 104 turns in 177.6 s (34.8 decisions a minute, with 70.5 s of pacing over 3 minutes). The
  ending was S1's `finish` at about 178 s.
- **Choices:** 60 of 104 turns were `mouse_turn_left`/`mouse_turn_right`, including the first 25 turns
  (28 s) spent turning in place. 17 were `move_forward`.
- **Overshoot:** a held move covered 6–10 units before the next decision, against about 2 units per step.
  With a decision every ~2 s and walking at about 5 units/s, S1 overshot the long staircase, went to the
  dark side flights, and at one point reached x ≈ 50.
- **Climb:** running jumps took it to the top of the 2-unit side flight (feet 2.08 at about 80 s). The long
  grey flight was never climbed.
- **Help:** it escalated 4 times. Answers arrived in about 9–14 s and were adopted. It restarted the level
  twice.

**Totals:** S1 109 calls, $0.0415; frontier 3 calls, $0.0654. The recording is 3 generations (1,770
frames, none missed), joined as `recording/climb-continuous-01-full.mp4`, with a key-moments sheet.

**What it shows:** continuous input only helps when decisions come faster than the character covers the
precision a task needs. At about one decision every 2 s it is right for traversal and wrong for lining
up on a 2-unit-wide flight. A version where S1 chooses per move between a step and a held walk would keep
both.

## Step-or-held and pattern-fact trial (`climb-held-pattern-01`, 180 s)

Settings: as `climb-continuous-01`, plus the per-move choice between a step and a held walk (part of
`--continuous-input` from 36b25ea) and `--pattern-facts`. The code was 948cc0d, and the approval is
`approval-trial-held-pattern.json`.

- **Speed:** 97 turns in 179 s, which is 32.2 decisions a minute, with S1's p50 at 312 ms. The mean billed
  input was 10,221 tokens, against 9,061 in `climb-continuous-01`, so 13% more: the four held variants and
  their labels, plus the pattern line when it shows. The run ended at the wall limit, and S1 never chose
  `finish`.
- **Step or held: S1 uses both.** It chose 16 held walks and 9 steps.
  - Held walks did the long runs. Eight in a row at yaw 0 covered 18 units in 5 s; decisions came about
    every 0.6 s at the start, inside the burst.
  - Steps did the precise part. Seven steps at yaw 37° took it from the spawn to the dark steps (feet 1.07
    at x 9.7, z −8.5), and the next step took it off them.
- **Pattern fact: shown, mostly not acted on.** It appeared on 22 turns. Where a cycle of four or more
  stood, S1 continued it 16 times out of 20 (80%), against 32 of 36 (89%) without the fact. The longest
  cycle fell from 29 to 13, and turns and looks fell from 70% to 62% of executed choices. The trajectories
  differ and there were restarts, so this is an observation, not a test. It reads as no clear effect
  (`docs/design/oscillation.md`).
- **Climb: worse.** Feet height peaked at 1.07 on the dark steps. That compares with 2.08 in
  `climb-continuous-01` and 5.32 in `climb-stairs-02` (stepped input, 60 s). The long grey flight was
  never reached.
  - S1 restarted the level 3 times, and each restart put it back into the turn cycle at the spawn.
  - It ended with six running jumps in place at x 10, z −4.
- **Help:** 3 escalations, with 2 answers adopted and 1 cancelled by a restart. The answers were bug
  hypotheses about the readings (forces, and the yaw cycle), not directions.

**Totals:** S1 101 calls, $0.0434; frontier 3 calls, $0.0423. The recording is 4 generations, joined as
`recording/session.mp4`, with a key-moments sheet.

**What it shows:**
- The step-or-held choice works as designed, and S1 takes steps where precision counts.
- Stating the cycle did not stop it. The objective asks S1 to centre the staircase's foot, and no
  heading one choice from the spawn does. That tolerance is tighter than the 38.4° turn step, which is a
  classic hunting condition.
- The next cheap check is an objective with a deadband of at least half a step, such as "anywhere in the
  middle half of the view". It changes no code.

## Wider-tolerance trial (`climb-tolerance-half-01`, 165 s of 180)

Settings: as `climb-held-pattern-01`, except the objective (`objective-climb-stairs-3min-half.txt`). Its two
alignment phrases were widened:
- "is in the centre of the view" became "is ahead of you, anywhere in the middle half of the view";
- "rise straight ahead of you" became "rise ahead of you, anywhere in the middle half of the view".

The code was 13921ac, and the approval is `approval-trial-tolerance-half.json`.

- **Hunting: over after 5 turns.** S1 alternated between 0° and 38.4° five times, then chose a held walk
  at 38.4°.
  - The longest cycle was 5 turns, against 13 and 29 in the two runs before.
  - At cycle points it continued once and broke three times.
  - Turns were 49% of executed choices, against 62% and 70%.
- **Climb: the top of the long staircase in 15 s.** It walked to the foot, turned to 0° (the flight's
  axis), and walked and jumped up: feet 2.0 at 12 s, 6.4 at 14 s, 8.5 at 15 s. That is the highest of any
  trial; the best before was 5.32. The next held walk carried it off the far end (feet 0.1 at 18 s).
- **Afterwards:** 3 escalations, all answers adopted, then 11 waits and some wandering. At about 120 s a
  held walk took it off the level's edge. It fell for about 40 s, reaching feet −2,104, and kept choosing
  moves and turns until it chose `restart_play_mode` at 165 s.
- **Ended inconclusive, not through S1.** The restart failed reading the host's `result.json`: errno 13,
  a Windows sharing violation while the host replaced the file. Fixed on `fix/session-control-read-race`
  (7e624cd), where a denied read is retried for about 0.5 s.
- **Speed:** 77 turns in 165 s, which is 27.6 decisions a minute. There were 12 stale rejections, most
  while falling, and S1's p50 was 406 ms.

**Totals:** S1 81 calls, $0.0368; frontier 3 calls, $0.0691. The failed session left the recorder's video
unencoded, so `recording/session.mp4` was encoded afterwards from its 1,616 frames. There is a key-moments
sheet.

**What it shows.** This is one run, not proof, but it is the predicted effect. With a tolerance of at least
half a turn step, S1 stopped turning at a reachable heading and walked. With held walks it then climbed
the whole flight in seconds.

It also shows two new failure modes:
- a held walk overshoots the top;
- S1 does not react quickly to falling off the level.

A frozen-frame probe across several tolerance widths is being built to measure the effect turn by turn.

## Wider tolerance with send-anchored pacing (`climb-tolerance-send-01`, 180 s)

Settings: as `climb-tolerance-half-01`, plus `--pacing-anchor send`. The code was 5a9fb00, and the approval
is `approval-trial-tolerance-half-send.json`. The run completed.

- **Hunting: short again.** S1 alternated 6 times, then walked at 38.4°. The longest cycle was 7 turns,
  and turns and looks were 35% of executed choices.
- **Climb: missed.** At the foot of the long flight S1 was still at 38.4°, with the flight rising on the
  left of the view. It walked on instead of turning to the flight's axis (0°). It jumped across onto the
  dark steps (feet 3.1 at 9 s) and spent the rest of the run on the ground. In `climb-tolerance-half-01`
  it turned to 0° at the same point and climbed to the top. The two tolerance runs differ at exactly this
  decision.
- **Pacing: the anchor works.** `ready()` now returns a median 407 ms before "previous reply plus
  spacing", which is the reply latency taken off the wait. Sends came every 1.84 s (32.6 a minute). The same
  run paced from the reply would have taken 2.28 s (26.3 a minute), so the anchor bought 24%.
  - The absolute rate did not rise against earlier runs. Requests were larger (11,463 billed tokens on
    average, so 1.64 s of spacing), and the provider was slower (S1 p50 469 ms).
- **Stale rejections: provider latency spikes.** 18 turns were rejected because the observation was over
  1.0 s old at execution. Those calls had a median latency of 1,539 ms, against 406 ms for executed turns.
  - The earlier runs today had 1, then 12. The second of those was paced from the reply, so this is not
    the anchor.

**Totals:** S1 100 calls, $0.0481; frontier 3 calls, $0.0731. The recording is `recording/session.mp4`,
with a key-moments sheet.

## Measured turn sizes with the original "centre" wording (`climb-turn-sizes-01`, 180 s)

Settings: as `climb-tolerance-send-01`, but with `--turn-sizes` and manifest 3 (manifest 2 with
calibrations refreshed by a both-signs calibration), and the *original* objective ("in the centre of the
view"), so that any change could be put down to the turn sizes. The code was 191fb8f, and the approval is
`approval-trial-turn-sizes.json`. The run completed.

- **Speed: the fastest yet.** 113 turns in 179 s (37.5 decisions a minute), with S1's p50 at 344 ms and
  only 3 stale rejections. The single-input turns are quick.
- **Hunting: finer, not gone, and worse in share.** 99 of 109 executed choices were turns, against
  62–70% before.
  - S1 used the 18° stick turn 79 times, 36° 12 times, 72° 7 times and 144° once. It **never chose the
    7.7° mouse turn**.
  - For most of the run it swung between headings about 18° apart (21↔39°, 32↔50°, 24↔42°, 41↔59°)
    around the staircase near 27–40°.
  - The longest cycle was 19 turns. At cycle points it continued 33 times and broke 10.
- **Climb: none.** At 163 s it finally walked at 40.7°, and ten held walks carried it from the spawn past
  the staircase to x 49, z 41. It chose `finish` on the last turn. There were no escalations.

**Totals:** S1 113 calls, $0.0495; no frontier calls.

**What it shows.** Finer actuators alone do not stop hunting. S1 hunts with the step it actually uses (18°),
and "centre of the view" asks for a tolerance tighter than half of that (9°). Offering ten turn options
against four also pulled S1's choices toward turning. By the deadband rule, the next check is turn sizes
with the "middle half" wording, whose ±20° covers half of the 18° step with room to spare.

## Pointing turns with the "centre" wording (`climb-point-turns-01`, 180 s)

Settings: as `climb-turn-sizes-01`, with `--point-turns` in place of `--turn-sizes`. The code was daa6af1, and
the approval is `approval-trial-point-turns.json`. The run completed.

- **Speed:** 115 turns in 178 s (38.4 decisions a minute), S1 p50 312 ms, and no stale rejections.
- **Hunting: still there, and the pointing vocabulary explains it.** S1 chose the strip skills 101 times and
  held walks 12 times. It swung between headings about 25° and 46° around the foot (41°) until 159 s.
  - Scoring each choice against the foot's true strip shows the problem. With the foot in the centre strip
    (32 decisions), S1 chose "face strip 4" (turn left) 22 times and walked only 8 times.
  - The centre strip was not offered: the brief left it out, because facing it is not a turn. So S1's
    natural answer, "it is in strip 5", had no choice, and it took the neighbouring strip, turning away.
  - Off centre it pointed as in the probe: true strip 4 got 4 sixteen times and 3 ten times; true strip 7 got
    7 ten times and 6 three times.
- **Climb: none.** At 159 s it walked at 40° and crossed the foot diagonally at x 8–13.

**Totals:** S1 116 calls, $0.0502; frontier 1 call, $0.0303.

**What it shows.** S1's pointing works, but an action vocabulary without the fixed point cannot settle. "The
target is straight ahead" must be a choice in the same terms S1 perceives in: strip 5. The proposed fix is
"go toward strip k" for all nine strips, where each choice turns by the strip's measured angle and then
takes one measured step. Strip 5 is then a step straight ahead, and even an imperfect point makes progress.

## Go toward view strip k (`climb-point-moves-01`, 154 s)

Settings: as `climb-point-turns-01`, with `--point-moves` (each strip choice is a measured turn and then one
measured 2.0-unit step; strip 5 is a step straight ahead). The code was 4a8bed1, and the approval is
`approval-trial-point-moves.json`.

- **Approach: no hunting, straight onto the staircase.** The longest cycle was 6 turns, and no turn-in-place
  choice was made.
  - From the spawn, one pointing move and six "go toward strip 5" steps put S1 on the long flight, head-on in
    the 4 s frame, with feet at 2.7 by 7 s. No earlier run reached the flight that directly.
  - One anomaly: the first composed turn (strip 8, measured 28.9°) turned the view about 60°. Four later
    strip-8 and strip-1 moves matched their measured totals within about 2°. The cause is unknown.
- **Climb: off the side.** S1 kept choosing "straight ahead" at its 23° approach heading. The flight runs at
  0°, so it crossed the 2-unit width and stepped off the east side at 2.7. It never re-aimed along the
  flight.
- **Afterwards:** 3 escalations with 15 waits, then 35 jumps in place. It moved west and jumped up another
  structure (feet 4.7), and chose `finish` at 154 s.
- **Speed:** 30 decisions a minute. The long labels and accumulating guidance raised the mean billed input to
  12,619 tokens.

**Totals:** S1 83 calls, $0.0440; frontier 3 calls, $0.0818.

**What it shows.** Pointing plus a step solves *reaching* a target generically: every choice makes progress
and the loop has its fixed point. *Following* a narrow path is a different skill. On the flight, "straight
ahead" still looked right to S1, and nothing told it that the edge was one step to the right.
