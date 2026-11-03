# Expectations: the oracle is written before the measurement

A finding needs an oracle, something that says what should have happened. The post-session review formed it after
the fact: one Muse call read the whole trace and decided what was wrong. On the 2026-11-03 blind v2 pass every one of
its ten claims was false. It turned reader flicker and lag into stories, misread a sign, contradicted the guard table,
and claimed effects of inputs that had nothing in range. The comparable systems that work keep the two steps apart
(survey of 2026-11-03):

- **TITAN** (LLM agent for MMORPG testing, 2025) found 82% of seeded bugs. Its logic bugs come from a measured counter
  against the game's own text ("a quest needing six kills completed after five"), with crash, stuck and timing
  monitors beside it.
- **Wuji** (NetEase): crash and stuck are generic; "logical bugs require specific assertions".
- **Repository-aware metamorphic relations** (Unity AR, 2026): an LLM writes relations from class and method context
  before anything runs; a critic keeps or discards them; tests pass or fail mechanically. It killed every
  non-equivalent mutant (wrong sign, swapped axis, scaling).
- **Vision-model glitch benchmarks** (GlitchBench, VideoGameQA-Bench): about 80% on curated frames, many false positives,
  poor at finding when a glitch starts. Not used here.

## What is predicted, and from what (`playtest.runner.expectations predict`)

One Muse call per build. Its packet carries only what the game declares before play:
- each verb's, move's and the rise's stated intent and controls;
- the readers, each with the field it reads and the methods that write that field (from the IL scan);
- the authored values in the scene (`declared_configuration`) and in the assets the entity's fields reference
  (`referenced_assets`: a text-serialized `.asset` such as a player config, read at the build under test with
  `--project` and `--ref`, no checkout).

It carries no source text, because a seeded line would read as intended, and no measurement, because noise would read
as a bug. The answer is pre-registered: saved with its cost and request hash before any scoring.

| Kind | Scored against | Says |
|---|---|---|
| prediction | the guard tables | in this state (rest, a move's name, moving for every move, air) this verb changes this observable, in this direction, or reaches a declared value |
| range | every reading of play sessions | this reader stays within a declared bound |
| transition | reading pairs in play sessions | when this reader changes so, that one follows within `within_s` |
| duration | episodes of a reader's value in play sessions | once it takes this value it keeps it for the declared seconds |
| magnitude | the guard tables | a move's speed, the rise's height, or a verb's speed while a move is held, is the declared size |

Code screens the answer. A prediction is set aside, kept but never scored, when it names a verb or an observable
nobody measures, when `reaches` comes without a value, or when it needs a condition the guard states do not give (a
target in range, ammunition). That last rule alone removes the v2 class of "interact with nothing in range".

## How code scores it (`score`, `score-play`)

A guard-table cell is one of:
- **held**: the effect was seen, the right way, in any trial of the cell (`trial_changed`: an effect seen in some trials and not others is a flicker or a lagging reader, not a missing one);
- **missing, reversed, wrong_value**: it failed;
- **ambient**: the reader changes by itself in that state, so no press can show it;
- **masked**: the state without the verb already changes it (a stick's input reader while a key direction is held);
- **at_limit**: a trial began with the reader where the verb's presses end anywhere (a view already pitched to its
  limit cannot pitch further);
- **not_airborne**: every in-air press went in before the body had left the ground;
- **unmeasured**: no session measured that cell.

A reader that changes by itself is credited to a press only when the press moves it against its own trend (a reset),
so a predicted direction on it is scored as a change: the measured direction is never the prediction's to test.

A prediction failed in more than half of the sessions that could show it is a **mismatch**: the game's own
declaration says one thing, the measurement another, k of n times from a clean start. Fewer failures is **mixed**.

The guard rows carry what this needs: each trial's readings before its press (`starts`), the keys the state's
baseline changed (`baseline_changed`), the furthest the body got (`peak_displacement`, so a jump that lands where it
started still moves the body), and for the air state how the press was timed (`timing`).

The in-air press goes in at the rise alone's highest reading, not at half the calibrated airtime. Readings take about
0.3 s each (readers plus an observation), and DragonBlaze's jump is up at 0.2 s and down by 0.8 s, while calibration's
coarse holds put its airtime at 1.2 s: every press planned at 0.6 s went in on the ground, so before this no in-air
cell on DragonBlaze measured anything.

In play, a range fails at any reading outside its bound. A transition is checked at every trigger, from the reading
before it to the first reading at or after `within_s`. A trigger is not counted when nothing could show the
consequence:
- the session ended inside the window;
- a reading gap in it was over 4× the session's usual interval (a respawn or a reload nobody read; ordinary slow
  turns stay within 3×);
- it came inside the previous trigger's window (a second hit while the first one's invulnerability lasts).

A **magnitude** comes from the guard tables' own measurements: a move's speed is its baseline displacement over the
press length (`held_s`) in every cell that chords it; the rise's height is the rise-alone peak in the air cells; a
verb's speed is its cell's displacement over the press. Its band is wider below than above for the rise
(-25%/+10%), because a 0.3 s reading rarely lands on the apex; speeds take ±15%, a verb's speed ±20%. The press
lengths are in the packet, as plan parameters, so a declared speed can be turned into what the table will show.

A **duration** is an interval check, sound under readings about 0.8 s apart. An episode's true length lies between its
first and last reading with the value and the readings just outside it. A declared length outside that interval, with
a tenth of itself as slack, is a violation. An episode at a session's edge, or with an unread stretch beside it, is
not counted.

## Investigating what failed sometimes (`investigate`)

A transition that held at most of its triggers and failed at some is **mixed**: not a finding, but a lead. One Muse
call per such rule gets every counted event: whether it was followed, every reader just before and at the trigger,
the last input and how long before the trigger it ended, and the time into the session. It proposes up to four
conditions that would separate the failures (a reader against a value, or the last input). Code tests each against
every event as a 2×2 count with a one-sided Fisher exact p.

The frontier proposed the condition after seeing these same events, so a separation is a lead for a directed test,
not a finding. `supported` needs at least three failures, every one under the condition, and p < 0.05. A condition
that does not separate is reported as that, which is the usual answer for noise.

## In play (`--expectations`)

With `--measure-guards`, a session scores the predictions against its own guard table before play and saves
`expectations-unmet.json`. Each unmet line reaches S1 as a measured fact every turn and the frontier with the guard
facts. Nothing is decided in code: S1 weighs it as any other fact.

## What it found on the held-out builds (2026-11-03)

The clean builds' tables and traces were scored with the same predictions as a false-positive check. That check is
for measuring this method only; a run never uses a control.

| | Held-out | Clean |
|---|---|---|
| FPSKit, guards (7 held-out, 6 clean fresh sessions) | aim_toggle at rest: aiming and the declared aim FOV 50 both missing 7/7 | 0 mismatches |
| FPSKit, magnitudes (3 each) | move speed 5.01 for a declared 5; dash forward 9.55 for 10.5; crouch 3.07 for 2.9: held | held |
| FPSKit, play (11 held-out, 13 clean sessions) | pitch within its declared ±80, jump count within its declared 2: held | held |
| DragonBlaze, guards (3 each, per-trial keys, in-air presses at the measured apex) | 0 mismatches | 0 mismatches |
| DragonBlaze, magnitudes (3 each) | move speed 9.9–10.2 for a declared 10; dash 15.2 for the 15 Muse derived from dash 20 for 0.2 s and speed 10: held | held |
| DragonBlaze, play (11 held-out, 12 clean sessions) | health within 30: held. Invulnerability after a hit: 22 of 24, mixed. Invulnerability lasting the declared 1.5 s: 16 of 28, mixed. Dash flag lasting 0.2 s: 1 of 2, mixed | 6 of 6; 3 of 6, mixed; no episode seen whole |

The investigation found nothing to support for any mixed rule. The invulnerability transition's two failures both came
right after the agent's `sweep` option, which separates them at p = 0.004, but two failures is below the three
`supported` needs. No condition separated the long invulnerability episodes, which the clean build shows too. The one
long dash was S1 pressing dash every 0.7 s for 7 s: a run of fresh dashes, not a stuck flag.

Blind score (a separate scorer reading the answer key, reporting matched ids and counts only): FPSKit 1 of 3 (b05),
DragonBlaze 0 of 2, and neither DragonBlaze lead matched a seeded bug. Asked only yes or no, the scorer said that in
both games an unmatched seeded bug concerns something these checks measured and found holding. Those checks were not
tuned afterwards: a fix chosen from that answer would be fitted to the key.

The first version of the prompt also predicted direction-specific effects ("dash works in every direction"), and the
scorer had none of the cell outcomes above. Scored on the clean builds, it raised 6 FPSKit and 7 DragonBlaze
mismatches, every one an artifact or a design rule. Later clean-build false positives fixed the same way: a jump's
grounded flag and a pooled projectile's lifetime seen in one trial of two (now scored by any trial, `trial_changed`), a
dash speed assumed backward too (the magnitude prompt now carries the direction rule). Each fix is generic and came
from clean-build false positives, not from the answer key: the clean builds were this method's development set.

## Live: the loop with S1 deciding (`runs/fpskit/live-expect-01`)

One held-out FPSKit hunt with `--expectations`, 300 s, S1 d1-ultra, the frontier capped at 6 calls ($0.71 in all). The
session's own guard table left two expectations unmet (aim_toggle: aiming never true, field of view never toward 50);
both lines were in every one of S1's 251 requests. S1 escalated at turn 7. The frontier's first hypothesis was "aim
toggle has no effect at rest", with what would confirm and what would refute it, and it directed about 20 aim_toggle
presses in different situations. The run filed candidate findings f-41, f-51 and f-81 with the authors' declared values
as the expected source. Their replays need a repro check the frontier did not give; the clean-reset reproduction is the
guard measurement itself, 7 of 7 fresh sessions.

## Not done

- A **supported** condition from the investigation does not yet become a directed session (S1 steered to bring the
  condition about, then the rule checked again). The live loop above directs tests from the frontier's own hypotheses.
- The frontier's live findings have no replay `repro_check`.
- Calibration's measured speeds are not compared with declared ones beyond the guard tables' own (opposite moves were
  within 1.5% on both builds).
