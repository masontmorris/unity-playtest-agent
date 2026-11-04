# Final detection tuning pass (#244), 4 November 2026

The last bounded attempt before the feature freeze to find more of the seeded bugs. It was tuned on the clean
builds only: FPSKit `bridge-v0` and DragonBlaze `upa/clean`. Nothing was chosen from a held-out result or the
answer key. The baseline is the 4 November held-out record (`docs/probes/2026-11-04-heldout-hunts.md`): FPSKit 1 of
3 (b05), DragonBlaze 0 of 2. What changed is in `docs/design/expectations.md`, section "Final tuning pass".

Every hunt below is one `playtest-hunt` run with the same settings as the 4 November record: 3 live sessions of
600 s, S1 `liquid-d1-ultra`, the frontier `muse-spark-1.3` (Standard tier) with up to 12 calls, and replays of 5
attempts. Full traces stay local under `runs/hunts/`.

## What was tried, and what the clean builds showed

Two of the issue's three directions:
- **Declared sizes (direction 2).** The hunt now makes the focused magnitudes call. On the 4 November clean
  DragonBlaze hunt, no declared size was checked at all.
- **Presses a rule can count (direction 3).** A press-test option for each input an action rule presses, and sweep
  presses that keep their measured end.

Contact rules with no press (direction 1) were left out: the distance that counts as touching is a collider's size,
and no authored value gives it.

The clean runs found four faults in the new measurement, each fixed before the next run:

| Run | What it showed | Fix |
|---|---|---|
| DragonBlaze `clean-02` (stopped in session 2) | The press test's own readings were empty: an executor observation carries no reader values. The scorer read each empty window as "did not follow", so 76 of 76 attack presses failed, and the frontier filed a false finding. | The press test reads the session's field readers. A press whose reader was never read is `unread`, never failed. |
| DragonBlaze `clean-03` (stopped in session 2) | Attack's projectile reader changed in only 1 of 2 fresh guard trials, in every table (it follows one pooled projectile). Dash's flag lasts 0.2 s, shorter than a reading after release. The frontier filed both. | A new verdict, `intermittent`: the guard tables saw the input change the reader in some trials of a cell and in every trial of none. `while_held` and `intermittent` rows are left out of the live rule lines, as unmeasured ones already were. |
| FPSKit `clean-01` (replays cut short) | The frontier filed look_down with the pitch at 80 twice (f-107 replayed 0 of 5; f-126 was not replayed). 80 is the declared clamp in look_down's own direction. The scorer had set those presses aside as at the reader's limit, but the line the frontier reads did not say so. | A rule's line now states how many presses began at the reader's limit, the limit's value and which way the input moves it. |

## Clean hunts on the final code

| Hunt | Sessions (ending, turns, press tests) | Findings and replay | Cost |
|---|---|---|---|
| DragonBlaze `clean-04` | wall limit 119, 45; 162, 34; 157, 41 | f-69, f-81, f-108: interact near the NPC never starts dialogue, each **5 of 5**: the known base-game defect | $0.70 |
| FPSKit `clean-02` | wall limit 257, 30; 154, 40; 140, 48 | f-190, f-201, f-67: interact at the dispenser changes no dispenser timer (see below). f-67 **4 of 4** (1 unknown); f-190, f-201 0 of 0, 5 unknown (no replayed press reached the region) | $0.90 |

DragonBlaze `clean-04` ran on the commit before the limit-line text. That change touches only text the frontier
reads; DragonBlaze's rules had no press at a reader's limit.

**Counted presses.** On the 4 November clean DragonBlaze hunt, the two attack rules were unmeasured: attack was
pressed twice in three sessions. The dash rules counted 15 and 17 presses, jump 1 and jump_pad 12. On `clean-04`,
each rule counted 104 to 136 presses. All held, except:
- dash's flag reaching true: `while_held`;
- attack's projectile lifetime: `intermittent`;
- the dialogue rule: a mismatch in 15 of 15 presses, the known defect.

On FPSKit `clean-02`, aim_toggle held in 112 presses, and the looks held in 31 to 88 presses each except look_up
(mixed, 2 of 32). Jump's count was mixed (18 of 56).

**Declared sizes.** These are scored on four guard tables per hunt:
- DragonBlaze: 4 of 4 held (move speed 9.92 to 10.87 for a declared 10; dash while moving 14.72 to 15.67 for 15).
- FPSKit: 8 of 8 held (move speeds 5.00 to 5.01 for 5).

**The clean FPSKit dispenser finding is a false finding.** This one is not fixed. The rule says interact within 3
of `PickupDispenser-BlasterA` changes its timer, citing `interactMaxDistance` 3.0. It was unmeasured in all three
earlier clean sessions (no press inside the region). Here it failed 15 of 15, the frontier filed it three times,
and f-67 replayed 4 of 4: on the clean build, interact at the dispenser changes no timer.
The interact distance is probably a raycast's reach, so the press also needs the item in view, which a
distance-only situation cannot express. The 4 November held-out FPSKit hunt filed the same kind of finding before
any of this tuning (f-64, f-91, "interact by a dispenser"). So it is not new to this pass, but the clean build is not
free of false findings.

Clean spend, including the stopped runs: about $3.33.

## Held-out hunts on the frozen code

One `playtest-hunt` per game on `d21c81a` (#245), with the same builds, manifests and settings as the 4 November
record: DragonBlaze `eval/dragonblaze/all`, FPSKit `eval/fpskit/all`. Both started before noon because the code was
already merged and frozen; nothing changed between them.

| Hunt | Sessions (ending, turns, press tests) | Findings and replay | Cost |
|---|---|---|---|
| DragonBlaze `heldout-02` | wall limit 87, 36; 126, 32; 114, 33 | f-99, f-69: interact near the NPC never starts dialogue (the known base-game defect); f-99 **5 of 5** | $0.55 |
| FPSKit `heldout-02` | wall limit 158, 39; 108, 48; 291, 21 | f-88, f-64: aim_toggle never enables aiming, each **5 of 5**; f-74, f-77: the dispenser rule, 1 of 5 and 0 of 0 (5 unknown) | $1.16 |

Declared sizes: 4 of 4 held on DragonBlaze, 8 of 8 on FPSKit. Unmet expectations (code only, nothing the frontier
filed): DragonBlaze 3, the projectile lifetime above its declared 5 once (5.099) and the two dialogue rules; FPSKit 4,
three aim_toggle guard predictions and the dispenser rule.

## Blind score

A separate scorer read `keys/SCHEMA.md`, the keys and the known-defect lists; only ids and counts came back.

| Build | Seeded bugs | Matched by a filed finding | Matched only by an unmet expectation | 4 November |
|---|---|---|---|---|
| FPSKit held-out | 3 | 1 (b05: f-88, f-64) | 0 | 1 (b05) |
| DragonBlaze held-out | 2 | 0 | 0 | 0 |

**No change.** Every filed finding and unmet line outside b05 matched nothing: the dialogue findings (not on the
eval repo's DragonBlaze known-defect list), the dispenser findings, and the projectile lifetime line.

What it says, without the key: the two directions tried here, checked declared sizes and many more counted presses
per rule, measured more than before and found nothing new. The missed bugs are not in a move or verb speed the
declarations fix, and not in what a press of a rule's input leaves behind in rest, moving or air states, at least
as far as these rules and readers can show. Contact situations with no press (direction 1), and rules the
frontier did not write, stay unexamined.

Spend for the whole pass, stopped runs included: about $5.04 (clean $3.33, held-out $1.71), within the $8 approved.
