# FPSKit b04 hunt: nine sessions, bug not found (30–31 October 2026)

Date: 2026-10-30 to 2026-10-31. Full traces, frames, clips and ledgers stay local under `runs/b04-hunt/`.

## Summary

Nine sessions on FPSKit development bug `b04` did not find it, and the last two changed the picture. `b04`
clamps the character controller's step offset to its skin width, and the hunt's premise was that this makes the
main staircase's 0.2575-unit risers impossible to step onto. Sessions 1–7 never entered the staircase: their
closest sample was 4.422 units from the first riser in sessions 1 and 7, 4.425 in session 2, 1.424 in session 3,
4.421 in session 4 and 4.423 in sessions 5 and 6. Sessions 8 and 9 were navigation rounds: session 8 came within
1.673 units of the first riser but walked past the corner about 19° short of the line, and session 9 reached 0.372 units,
then alternated two turns in place for 154 turns before finally walking straight up the staircase from y 1.78
to y 8.70 in nine `move_forward` turns, with no jump. That is the first climb any session records, and it does
not show the symptom `b04` is supposed to cause: the risers were stepped or ridden up from the front. The
staircase's treads are 0.571 units deep and the player capsule's radius is 0.4, so the capsule spans a tread,
rests on step nosings and is slid up them at about 24°, inside the 45° slope limit; the clamped step offset never
has to lift the capsule over a riser. The four side staircases are 2, 4, 6 and 8 units high over the same 16-unit
depth with 0.25 risers, so their treads are 2.0, 1.0, 0.667 and 0.5 units: the first two are deep enough for the
capsule to stand on a tread, and no session has climbed one. Both sessions ended in `finish` with zero findings;
the hunt cost $4.315 across three preflights, nine sessions and two rate probes.


## Setup

- **Target.** `b04`, branch `eval/fpskit-dev/b04`, game commit `2d630da`, from the development set, which is
  not blind: its bugs may drive tuning. It clamps the character controller's effective step offset to its
  skin width, so stair risers of 0.25–0.2575 units can no longer be stepped onto; flat movement is unaffected.
- **Stairs and controller.** The main staircase starts at `(8, 0, -10)`, spans x 8–10 and z -10 to 6, and
  climbs toward +z: 28 steps of 0.2575 with 0.571 treads. Four more staircases (risers 0.25, depth 16, heights
  2, 4, 6 and 8, so treads 2.0, 1.0, 0.667 and 0.5) climb toward +x at x 18–34,
  z -34 to -26; no sampled position came within 17.4 units of them except in session 4 (3.24 units, turn
  159). The player prefab's controller has step offset 0.3, slope limit 45°, height 1.95, radius 0.4 and skin
  width 0.08 (runtime values not recorded). The player spawns at `(3.58, 0.08, -16.25)` facing +z.
- **Sessions.** S1 `liquid-d1-ultra` (resolved `d1-ultra`), frontier `muse-spark-1.3` on Meta Standard,
  recording on, a 600 s wall limit, a 0.8 s action age limit and at most 400 S1 and 15 frontier calls
  ($1.55 ledger cap) each. Sessions 1 and 2 used the Day 6 gate's generic exploration objective
  (SHA-256 `e88e3f35…`); sessions 3–7 added "Head for structures you can see."; sessions 2–9 gave S1 the
  game view. Code: session 1 `f4f3396` (with #154), session 2 `447cd18` (with #162), session 3 main
  `1e40c3d` (#165), session 4 main `109cb53` (#167), session 5 `7bfa601` (#169), session 6 main
  `c3a61b4` (#170/PR #175, #171/PR #174), session 7 main `4a2a5cc` (#177/3576d33, #179/PR #180),
  session 8 main `b5f59f3` (#182/PR #183), session 9 main `ffdf853` (#184/PR #185). Sessions 6–9 reused
  preflight-3's manifest and `calibration-3`.
- **Approval.** At 13:11 UTC the user approved one fresh Standard preflight and up to three sessions (at most
  about $2.80; balances unverified). The second preflight and each rate probe were approved separately. At
  17:24 UTC they approved a third preflight and one session (about $0.80), and at 21:47 UTC session 5 (at most
  $1.55; balances unverified). At 23:08 UTC the user approved session 6 (at most $1.55; about $0.35–0.45 expected;
  balances unverified), and at 23:22 / 23:47 UTC session 7 (at most $1.55; about $0.35–0.45 expected;
  balances unverified; launched after #179 merged). At 00:43 UTC on 31 October the user approved session 8 after
  #183 merged ("run session 8"), and at 00:58 UTC session 9 after #185 merged ("do it and run the new trial");
  both at most $1.55 with about $0.25 expected and balances unverified.
- **Measures.** Positions are the recorded camera's x and z at each turn's observation, before that
  turn's action (camera and player agreed in all 56 paired preflight inspections); none was recorded
  inside an action. Distances are horizontal, to the first riser (x 8–10 at z -10) or to a side staircase.
  Session 9's climb is read from the same camera positions, so its height is the camera's y, about 1.78 above
  the surface the player stands on.

- **Costs.** S1 costs are provider-reported per call. Frontier and preflight costs are computed from
  recorded token usage at Meta Standard rates ($1.25 input, $0.15 cached, $4.25 output per million).

## Runs

| Run | UTC date | Cost | Turns | How it ended |
|---|---|---:|---:|---|
| preflight-1 | 2026-10-30 | $0.7039 | — | 4 model calls; manifest probed 8/8 actions, 3/3 skills |
| session-01 | 2026-10-30 | $0.2424 | 133 | 600 s wall limit |
| preflight-2 | 2026-10-30 | $0.4386 | — | 4 model calls; manifest probed 14/14 actions, 3/3 skills |
| session-02 | 2026-10-30 | $0.2554 | 90 | 600 s wall limit |
| rate probe | 2026-10-30 | $0.0511 | — | 166 of 166 calls returned 200; stopped at its call cap after 57.0 s |
| rate probe follow-up | 2026-10-30 | $0.0320 | — | 100 of 100 calls returned 200; call cap, 111.7 s |
| session-03 | 2026-10-30 | $0.2924 | 142 | S1 chose `finish` at turn 141, after 276 s |
| preflight-3 | 2026-10-30 | $0.4515 | — | 4 model calls; manifest probed 14/14 actions, 4/4 skills |
| session-04 | 2026-10-30 | $0.3331 | 171 | S1 chose `finish` at turn 170, after 400 s; 215 ledger calls |
| session-05 | 2026-10-30 | $0.3998 | 94 | S1 chose `finish` at turn 93, after 391 s; 138 ledger calls |
| session-06 | 2026-10-30 | $0.3987 | 146 | S1 chose `finish` at turn 145, after ~9.5 min; 186 ledger calls |
| session-07 | 2026-10-30 | $0.3531 | 102 | S1 chose `finish` at turn 101, after ~6.4 min; 147 ledger calls |
| session-08 | 2026-10-31 | $0.2415 | 109 | S1 chose `finish` at turn 108, after ~5.5 min; 137 ledger calls |
| session-09 | 2026-10-31 | $0.1215 | 185 | S1 chose `finish` at turn 184, after ~7.7 min; 195 ledger calls |
| **Total** | | **$4.315** | | preflights $1.594, sessions $2.638, rate probes $0.083 |

The table sums to $4.3150. The d1-ultra image probe (2 calls, $0.000040) is not in
the table and does not change it at this precision. Sessions 2 and 3 used preflight-2's manifest, sessions 4–9
preflight-3's.

## What changed between runs

After session 1 (preflight-1: 0.05 s holds, about 0.25 units a step, `look_right` but no `look_left`):

- **PR #158 (#156).** Preflight asks for step holds that cover ground and both directions of every
  axis; gaps are reported, never rejected. preflight-2 picked 0.4 s steps (≈ 2.0 units), a 1.2 s
  traverse (≈ 5.53 units) and both look directions, each impulse turning the view 7.68°.
- **PR #159 (#157).** Opt-in S1 game view (`--s1-game-view`): the newest bridge frame, at most 1.0 s
  older than the observation, goes with S1's request; an older or missing frame is stated as such.
- **PR #161 (#160).** A two-call probe on `liquid-d1-ultra`: a 640×480 JPEG added 450 input tokens (257 to
  707), so the arm joined the image allowlist. It shows the image was taken in, not that S1 used it.
- **PR #163 (#162).** The launcher, `run-session.ps1`, kept its own image allowlist and refused
  `liquid-d1-ultra`; it now reads the runner's list.

After session 2, which waited 491.5 s of 600 s on pacing at 85,000 tokens/min:

- **Rate probe (#164).** Replays of session 2's S1 requests and frames on `d1-ultra`; every call
  returned 200, none 429. The unpaced probe (166 calls) hit its call cap after 57.0 s, averaging
  1,281,392 tokens/min: under a minute, so no sustained rate. The follow-up (100 calls at least 1.0 s
  apart) took 111.7 s; its 60 s windows ran from 353,640 to a peak of 478,767 tokens/min. The rule set
  before spend, 80% of the lowest window rounded down to 10,000, gave 280,000 tokens/min: an operating
  setting for this workload, not Liquid's limit, which is unknown.
- **PR #165 and the objective.** Pacing resolves by S1 arm: 280,000 tokens/min for `liquid-d1-ultra`, 85,000
  for others. Session 3's objective added "Head for structures you can see." and no mention of stairs.

After session 3, where a look step turned the view 7.68° (#166), and after sessions 4 and 5:

- **PR #167 (#166).** The bridge reports the game-view camera's yaw and pitch, calibration records the view
  change per trial, preflight is asked for a coarse turn step of about 30–60° in each direction of every look
  axis, and validation flags a measured look turn of 2°–20° as a gap. Live calibration (Unity only, 254
  trials) measured a mouse impulse at 7.680° at every hold and the right stick at 18°, 36°, 77° and 144° for
  0.05, 0.1, 0.2 and 0.4 s (at 1.2 s it wrapped).
- **preflight-3** (Standard, 4 calls, ledger estimate $0.4515; provider-reported cost null). It reused
  preflight-2's survey, discovery and frame, plus `calibration-3`, and selected 14 actions and 4 skills,
  including `coarse_turn_right`/`left` (five mouse impulses, probe ±38.40°) and stick looks of ±144° per
  0.4 s step. The probe release-verified 18 of 18, one attempt each.
- **PR #169 (#168).** Bounded factual action memory for S1 and the frontier, remaining wall time and call
  slots for S1, and turning labels that separate displacement from view rotation. Session 5 was its first live
  run: S1's median input was 15.8k tokens against 9.0k in session 4, and pacing took 276.3 of 390.3 s.
- **PR #174 (#171, f16e5c5).** Frontier requests label preflight reader notes as unverified inference; session 5's
  candidate rested on a note that called `jump_ttl` a cooldown, but the game counts time since a jump. In session 6
  the frontier read `jump_ttl` correctly as time-since-jump (#171 worked, no false timer finding).
- **PR #175 (#170, c3a61b4).** An offline layout that keeps every revised statement and citation and restores
  the original exactly, measured on session 5's trace: -29.3% final guidance (31,622 to 22,342 characters),
  -16.1% final payload and -7.5% of a session's S1 state characters (characters, not tokens). Merged as PR #175
  and live in session 6.

After session 6:

- **3576d33 (#177).** Honour `unless` on range and monotonic invariants: an excused sample holds instead of
  raising a suspicion, and an exception that cannot be scored leaves the sample unknown.
- **PR #180 (#179, 4a2a5cc).** Let the frontier point S1 at untried parts of the level with location-targeted
  exploration directives. Session 7 was its first live run.

After session 7, whose directives walked S1 beside the staircase and then off the floor's south edge:

- **PR #183 (#182, b5f59f3).** Frontier navigation guidance: never send S1 toward, across or out over ground
  whose connection to where it stands the clip does not show; approach a part from the side it is already on; and
  when it is beside the structure it reached, try that structure from where it stands instead of backing away for
  a wider view. Session 8 was its first live run.
- **PR #185 (#184, ffdf853).** Line up before moving: turn until the part sits in the centre of the view, move at
  it, and after each move turn back onto it while it drifts toward the edge of the view, because a heading that
  only passes beside the part spends the approach. Session 9 was its first live run.
- **PR #187 (#186).** Turn steps have fixed sizes and need not land on the centre: when one step overshoots and
  the step back undershoots, take the smaller step that lands nearest and move then, because two turns that undo
  each other leave S1 where it started. Session 10 will be its first live run.

## Why the bug was not found

Sessions 1–7 never sampled a climb attempt: no session walked into the front of the staircase, and in session 1
the player never got near it. Sessions 8 and 9 did reach the front, and session 9 climbed the whole flight
without jumping. Evidence of `b04` would be a forward push into a riser with short or zero net displacement, or
contact with no height gain. One such push is recorded, at an angle (session 9, turn 16), and the square-on
climb that followed raised the camera by 6.9 units. So the sessions neither exclude nor evidence `b04` on the
staircase as built; what they do show is that its risers are walkable from the front by this controller.

| Measure | Session 1 | Session 2 | Session 3 | Session 4 | Session 5 | Session 6 | Session 7 | Session 8 | Session 9 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| S1 game view | not available | 108 sent, 0 stale | 173 sent, 0 stale | 200 sent, 0 stale | 123 sent, 0 stale | 171 sent, 0 stale | 133 sent, 0 stale | 129 sent, 0 stale | 192 sent, 0 stale |
| Pacing wait, turns waited (tokens/min) | 482.6 s, 66 (85,000) | 491.5 s, 57 (85,000) | 124.4 s, 41 (280,000) | 178.0 s, 56 (280,000) | 276.3 s, 43 (280,000) | 382.4 s, 64 (280,000) | 241.1 s, 50 (280,000) | 198.9 s, 32 (280,000) | 160.4 s, 60 (280,000) |
| Actions rejected as stale | 1 | 17 | 0 | 27 | 4 | 22 | 4 | 12 | 7 |
| Turns with an `executed` outcome | 52 | 30 | 39 | 47 | 28 | 35 | 33 | 47 | 163 |
| Executed `move_forward`, of executed actions | 41 of 52 | 6 of 30 | 19 of 39 | 34 of 47 | 13 of 28 | 20 of 35 | 8 of 33 | 34 of 47 | 14 of 163 |
| Turns that changed the view's yaw | 3 | 7 | 8 | 2 | 6 | 4 | 2 | 6 | 149 |
| View furthest from start heading | 23.04° right (3 steps) | 7.68° right (1 step) | 38.4° left (5 steps) | 156.85° right (stick look) | 166.62° right (look, stick look) | 46.08° right (look, coarse turn) | 38.40° right (coarse turn) | 46.08° right (coarse turn) | 46.08° right (coarse turn) |
| Closest approach to first riser (units) | 4.422 | 4.425 | 1.424 | 4.421 | 4.423 | 4.423 | 4.422 | 1.673 | 0.372 |
| Camera y range | 1.78–3.42 | 1.78–3.68 | 1.78 | 1.78 | 1.78–3.53 | 1.78–3.45 | −3235.19–3.13 | 1.78 | 1.78–8.70 |
| Largest camera x (staircase starts at 8) | 5.081 | 7.521 | 7.550 | 26.050 (at z -41.9) | 6.150 (at z 6.3) | 3.580 (at z -16.25) | 7.550 (at z -1.76) | 7.560 (at z -5.57) | 10.256 (at z 6.73) |


- **Session 2.** The staircase was in view at every turn: steps recognizable through turn 49, mostly its
  near side after. Whether S1 used the image is unknown; its calls record a choice, not reasons. By turn
  33 the player was at `(3.85, -8.23)`, past the staircase's front edge and 4.2 units west of its side.
  Five strafe-right actions (turns 61, 67, 75, 83, 89) fell short of the calibrated 2.003 units,
  consistent with the side at x = 8: a geometric attribution, not a logged collider.
- **Session 3.** The closest approach, at `(6.63, -9.61)`, was first sampled at turn 38, after a
  `move_back` at turn 37 with the view turned 38.4° left; the position held through turn 47, when a
  `move_forward` took the player away from the stairs. S1 chose `finish` at turn 141, leaving 324 s of
  the limit unused.
- **Session 4.** S1 was nearest the first riser at turn 7 (4.421 units from its west end); a stick look at
  turn 57 turned the view 156.85° right. It reached `(26.05, -41.94)` at turn 170, passing 3.24 units from a
  side staircase at turn 159. 27 of 171 turns were stale rejections (none in session 3).
- **Session 5.** S1 was nearest the first riser at turn 4 (4.423 units from its west end); each coarse turn
  was used once (turns 17 and 21). It reached `(6.15, 6.30)` at turn 90, 1.875 units from the staircase's
  footprint, and chose `finish` at turn 93, its context stating the frontier cap exhausted and 209.7 s left.
- **Session 6.** S1 was nearest the first riser at turn 4 (4.423 units, at x = 3.58, z = -10.16) after walking
  forward beside the stairs (not at spawn; spawn itself is 7.66 units from the first riser). S1 chose `finish`
  at turn 145 (~9.5 min wall time), with 22 stale rejections and 382.4 s spent waiting on pacing (382 s). The player
  travelled west to x = -15.15. The frontier read `jump_ttl` correctly as time-since-jump (#171 worked, no false
  timer finding), and 16 frontier adoptions occurred. One candidate finding was recorded: `f-19` (jump net
  displacement 0.45 vs calibrated 1.137 units, hedged as possible measurement-window timing or ceiling block).
  S1 input tokens were median 15,457 / p95 20,584 / max 20,770.
- **Session 7.** S1 was nearest the first riser at turn 3 (4.422 units, start area). S1 chose `finish` at
  turn 101 (~6.4 min wall time, wall 23:47:42–23:54:07 UTC 2026-10-30), with 4 stale rejections and 241.1 s pacing wait (241 s).
  This was the first session where the frontier pointed at the level: 6 location-targeted exploration directives
  were issued (#179/PR #180): d-4, d-21, d-30 at the staircase-wall on the right side of the view (with d-21 scored met);
  d-44 sky slit; d-60 corridor; d-90 grey ground. S1 executed `explore_forward` 18 times (vs once in session 6).
  The player travelled south to z = 68.42 and fell out of the world (camera y about -3200, move_vec y -52.7;
  final frame shows gun, flat grey, sky, no structures; the scene file shows the 123x123 floor ends at z=61.5
  with no south boundary, so the player walked off the edge: level geometry, not a bug signal),
  finishing while falling. Zero findings were recorded. Session 7 raised no invariant-basis suspicions, so #177
  had no observable effect on it. Frontier made
  14 calls. S1 input tokens were median 14,302 / p95 17,157 / max 17,602.
- **Session 8.** The first run with the navigation guidance (#182/PR #183). S1 was nearest the first riser at
  turn 23 (1.673 units, `(6.65, -9.01)`), the first target the frontier had lined it up on: d-1 said to approach
  the wide stepped structure "from the side you are already on", and S1 turned four `look_right` steps to yaw
  30.7° and walked. The line from `(3.58, -14.18)` to the staircase's front needed about 50°, so it skimmed the
  corner, passed to `(7.56, -7.29)` and never touched a riser. Camera y stayed 1.78 for all 109 turns and `jump`
  was never chosen. It then backed away per d-25, escalated, walked north on open ground to z 32.88, and chose
  `finish` at turn 108 with 129 of 400 S1 calls and 8 of 15 frontier calls used. 32 suspicions (8 effect
  mismatch, 23 range exit, 1 drift), zero findings. Cost $0.2415: S1 $0.0684, frontier $0.1731.
- **Session 9.** The first run with the line-up guidance (#184/PR #185), and the first climb. After three
  `move_forward` S1 stood at `(7.69, -10.21)`, 0.372 units from the first riser, and a `move_forward` there
  moved it 0.106 units against a calibrated 2.003: a blocked push, but at yaw 46.1° into the staircase's corner.
  It then spent turns 17–170 alternating `coarse_turn_left` (76) and `coarse_turn_right` (77) with its position
  frozen at x≈7.65, z≈−10.12, yaw between 7.7° and 46.1°: the 38.4° coarse step overshot the centre the
  directive asked for and the step back undershot it, and it never took a 7.7° look step to land between. At
  turn 170, facing yaw 7.7°, nine `move_forward` turns carried it up the staircase from `(7.65, 1.78, -10.12)`
  to `(9.98, 8.70, 4.71)` — 6.92 units of climb at the staircase's average slope, no jump, every move executed
  with 1.8–1.9 units of horizontal travel — and at turn 180 it walked off the north end to `(10.26, 1.78, 6.73)`,
  a 6.92-unit fall to the floor. It escalated twice, waited twice and chose `finish` at turn 184. 40 suspicions
  (7 effect mismatch, 32 range exit, 1 drift), zero findings. Cost $0.1215: S1 $0.0307, frontier $0.0908.

- **Turning.** Every yaw change in sessions 1–3 is ±7.68°, one look impulse. The front riser lies 35°–46°
  right of the spawn heading, about 41° at its centre (computed from the spawn position and the geometry
  above): one 38.4° coarse turn, or about 5 look steps. Sessions 4 and 5 changed yaw with three coarse turns
  (±38.40°), two 7.68° looks and three stick looks (right +156.85° and +158.94°, left -144.14°), where the
  right stick was calibrated at 144.28°; why the right looks differ is unknown. Session 6 changed yaw on 4 turns
  (one 7.68° look right, three 38.40° coarse turns; reaching 46.08° right and -30.72° left). Session 7 changed yaw
  on 2 turns (one 38.40° coarse turn right at turn 13, one coarse turn left at turn 45). Session 8 changed yaw
  on 6 turns (coarse turns and looks, 46.08° right at most). Session 9 changed yaw on
  149 turns: the 154-turn standstill between 7.7° and 46.1° described above, then a single heading held at 7.7°
  for the whole climb. The first heading change came at turn 57 (session 4), turn 17 (session 5), turn 22
  (session 6), turn 14 (session 7), turn 15 (session 8) and turn 9 (session 9), after the player had passed z -10
  in every session but 8 and 9.
- **Findings.** Session 1: `f-21` (`jump_ttl` never resets), `f-36` and `f-67` (0.05 s moves overshoot
  calibration), `f-50` (a reader stuck at zero). Session 2: `f-22` (`jump_ttl` free-runs) and `f-39`
  (jump net 1.43 against a calibrated 1.78 units). Session 3: `f-77` (`jump_ttl`). Sessions 4 and 5: `f-47`
  and `f-19` (candidates, not reproduced). Session 6: candidate `f-19` (jump net 0.45 vs calibrated 1.137,
  hedged as measurement-window timing). Session 7: zero findings (no invariant-basis suspicions, so #177 had no
  observable effect; no false timer finding after #171 / PR #174). None concerns stairs. The replay of `f-36` held its check in 1 of 5
  attempts. `f-77` is unsupported as a defect: `jump_ttl` counts time since the last jump by design,
  unbounded while none fires. `f-21`, `f-22`, `f-47` and `f-19` read the same counter; only `f-77` is reviewed.
  Sessions 8 and 9 recorded zero findings each; their 32 and 40 suspicions are range-exit, drift and
  effect-mismatch bases only, none promoted. Session 9's seven `move_forward` effect mismatches are the climb and
  the block: 2.07–2.30 units of travel against a 2.003-unit flat calibration (the vertical component lengthens
  the path), and 0.106 units on the push at turn 16.
- **Where the loop's time went.** A churn analysis of 8 sessions (b04 sessions 1–3 and five Day 6 gate
  sessions, not 4 and 5) found 90% of S1's waits (278 of 310) made with a help request open, pacing sleeps
  at 45–83% of the loop, and S1 choosing `finish` soon after the frontier call cap in 4 of the 5 sessions
  that reached it. Sessions 4 and 5 did too (turns 150 and 86), choosing `finish` 20 and 7 turns later.
  Session 6 reached the 15-call frontier cap and S1 chose `finish` at turn 145 (turn 146). Session 7 had 14
  frontier calls before S1 chose `finish` at turn 101 while falling.
- **Not isolated.** The sessions differ in game view, pacing, step sizes, look directions, manifest, objective
  and, in sessions 5–9, what S1 and the frontier were shown (#169, #170, #171, #177, #179, #182, #184). Each is
  one run, and sessions 3–9 were development observations, not paired comparisons. Issue #166 attributed the
  miss to the small look step; sessions 4–7 offered coarse turns and still did not reach the front; sessions 8
  and 9 changed the frontier's wording and did reach it.

**Why the flight is climbable.** The main staircase's mesh is generated by `Assets/FPSKit/Utility/Stairs.cs`,
which rebuilds the MeshCollider from the same stepping mesh: 28 steps of 0.2575 with 0.571 treads. The player
capsule's radius is 0.4, so it cannot settle on a 0.571 tread; it rests on two nosings and the contact
resolution slides it up the line between them, at atan(0.2575/0.571) = 24.3°, inside the 45° slope limit. A
step offset is only used when the controller has to lift the capsule over a riser, so with this capsule and
tread geometry `b04` has nothing to act on. The four side staircases share the 16-unit depth and 0.25 risers but
are 2, 4, 6 and 8 units high, so their treads are 2.0, 1.0, 0.667 and 0.5 units; on the first two the capsule
can stand on a tread and meets a vertical riser, which is where a clamped step offset would show. No session has
climbed one. This is a reading of the scene file and the controller prefab, not something the runs measured: it
says where to look next, not that `b04` is absent from the build.

## What's next

- **Merged PRs.** #170 merged as PR #175 (c3a61b4); #171 merged as PR #174 (f16e5c5); #177 merged as 3576d33;
  #179 merged as PR #180 (4a2a5cc); #182 merged as PR #183 (b5f59f3); #184 merged as PR #185 (ffdf853);
  #186 merged as PR #187.
- **Spend.** `approval-plan-2.json` covered preflight-3 and session 4; `approval-session-05.json` covered session 5;
  `approval-session-06.json` covered session 6 ($0.3987 spent of $1.55 cap); `approval-session-07.json` covered
  session 7 ($0.3531 spent of $1.55 cap); `approval-session-08.json` covered session 8 ($0.2415 of $1.55) and
  `approval-session-09.json` session 9 ($0.1215 of $1.55). All approvals on record are used up ($4.315 total
  spend across nine sessions, three preflights and two rate probes). Any claim about why S1 rarely turns or about
  exploration directives needs a separately approved, paired evaluation; none is proposed here.
- **Next check.** The geometric reading above points at the side staircases rather than the main flight. A scripted
  push into a 0.25 riser with a 1.0-unit tread (no model calls) would test `b04` directly; a session aimed there
  would first need S1 to travel about 20 units to x 18–34, z -34 to -26, which no session has done.

## Sources

Local, uncommitted, under `runs/b04-hunt/`; no trace content is copied here:

- `session-01/` to `session-09/`: `run.json`, `trace.jsonl`, `calls/` ledger
- `preflight/`, `preflight-2/`, `preflight-3/`: `attempts.jsonl`, `manifest.json`; `preflight-3/report.md`,
  `probe-3/`, `calibration-3/view-summary.json`
- `analysis-02/notes.md`, `analysis-03/review-03.md`, `analysis-04/`, `analysis-05/`, `analysis-churn/churn.md`,
  `analysis-06-guidance/NOTE.md`, `reproduce-01-f36/reproduce.json`
- `rate-probe/summary.json`, `rate-probe-followup/run/summary.json`
- `approval-plan.json`, `approval-plan-2.json`, `approval-preflight-2.json`, `approval-preflight-3.json`,
  `approval-session-01.json` to `-09.json`, `approval-rate-probe.json`,
  `rate-probe-followup/approval-rate-probe-followup.json`; `objective.txt`, `objective-session-03.txt`

Public: PRs #158, #159, #161, #163, #165, #167, #169, #174, #175, #180, #183, #185 and #187 (merged);
commits 3576d33, b5f59f3 and ffdf853;
issues #156, #157, #160, #162, #164, #166, #168, #170, #171, #177, #179, #182, #184 and #186;
[image probe](2026-10-30-d1-ultra-images.md),
[rate probe](2026-10-30-liquid-d1-ultra-rate.md) and
[decision-context audit](2026-10-30-decision-context-audit.md).
