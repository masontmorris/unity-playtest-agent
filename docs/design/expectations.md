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

## The rise in the air, and guards (2026-11-03)

The rise is no verb (its controls are the rise's), so the guard table never pressed it in the air. With
`--measure-guards` a session now also runs the options layer's air-rise probe, a rise and then the rise pressed again
at its measured apex, and keeps it in the table as `rise_air`. The rise may then be predicted in the air only, by
body_motion: held when the second press lifted the body higher by more than a quarter of a rise. On both clean
builds the second jump doubled the height (FPSKit 2.0 to 4.0, DragonBlaze 2.7 to 5.4), 3 of 3 sessions each, and
FPSKit's clean predictions included it from its declared `maxJumps 2`.

A missing effect that the same input shows in another state (at rest, while moving, in the air) is now **guarded**,
not a mismatch: the input works, only not here, which is a rule of the game or a bug the declarations must decide.
It stays in the report, beside the line saying where the input did work, and is not stated to S1 as unmet. Another
move of the same state does not count: an effect present while one direction is held and missing while its mirror
is, stays a mismatch, as an input with no effect anywhere does (b05 stays a mismatch on the held-out build, 7 of 7).
Clean DragonBlaze's predictions said its dash works at rest; it works only while a direction is held, 3 of 3, and
is now guarded rather than a false mismatch.

Nine of FPSKit's clean cells cannot be shown at all: look_up and look_down while moving or in the air begin with the
view already pitched to its clamp by the presses before them. They are reported at_limit, never missing. Measuring
them would need the view brought back between cells, which a name-blind table has no input known to do.

## Directed tests: what a press must cause in play (2026-11-03)

The guard table only creates rest, moving, air and a second jump, so game logic that needs a situation, or happens
after a press, was out of its reach. Action rules (`predict --part actions`) say what pressing an input must make
happen: "when this input is pressed and, if needed, a reader satisfies a condition just before, that reader must
follow within within_s". A condition no reader can show (an NPC in range) is kept as a situation to bring about.
`test-plan` turns the rules into a session objective, written by code and saying nothing the rules do not, and S1
pursues it with the frontier. Hunts had pressed a verb about once a session; directed sessions got 62 to 91 presses
the game received.

How a press is counted:
- every press the game received, alone or inside a chord or option whose controls include the input's, matched
  with the value each control is driven to (one stick pushed up and down are two inputs);
- not counted when its condition did not hold just before it, when the next press of the same input lands in its
  window, when an unread stretch lies inside it, or when its reader began at the furthest value the session read in
  the direction the input's own followed presses moved it (a view at its pitch clamp);
- `--press-readings` reads every reader just before and just after each input, into its effect record: S1's own
  readings come about every 0.8 s, and an effect a press leaves behind for a moment fell between them (jump, attack).

A failed rule is read against the same build's guard tables, which say what a reading can show and never the
verdict: changed only while held (gone by the reading just after release, `trial_persisted`), a reading after a press
cannot show it (while_held); changed in another measured state, a guard (guarded); never changed anywhere, a
mismatch. A mismatch whose only oracle is the names of the action, fields and methods is labelled the weakest.

Two fixes outside the scorer came from the same sessions. A rise's net displacement depends on when it is read (six
clean jumps measured 2.26 to 2.52 against a calibrated 2.47 with an allowance of 0.05, and the frontier filed it), so
an action whose calibrated displacement falls at a longer hold is never compared with it. A value stepped once a
frame is read up to a step past the bound it stops at (a lifetime declared 0 to 5 read 5.0005), so a declared bound
is crossed only by more than 1% of it.

| | Clean | Held-out |
|---|---|---|
| FPSKit (1 session each) | 0 mismatches; looks, crouch held | aim_toggle: aiming and aim blend never change (b05); the frontier filed it live with an `after_action` replay check |
| DragonBlaze (3 clean, 1 held-out) | 0 mismatches; jump, attack, the dash ending held; stick and dash at rest while_held | one names-only mismatch, dash never makes the player invisible, which fails on the clean build too |

Blind score with the directed sessions: FPSKit 1 of 3 (b05), DragonBlaze 0 of 2; the names-only lead matched nothing.

## Live on the clean build (`runs/fpskit/live-clean-01`)

The same hunt on clean FPSKit, with expectations pre-registered from the clean build's own declarations (0
mismatches against its three guards-only tables), 300 s, $0.32. It filed no finding: on a build with no seeded bug,
every finding would be false. The frontier's hypotheses settled as normal behaviour each time (range exits as
locomotion, the aim transition as aiming). One fact S1 got was false: look_right's yaw went 356.1 to 43.4, a turn of
+47 degrees across 0/360, which the scorer read as -313, "the other way". An angle's readings are now unwrapped
around the first (`is_angle`: camera.rotation, or a reader whose alias names an angle: yaw, pitch, roll, heading,
angle, rotation, euler), and the prediction holds on that table and every other clean one. Any other reader's
readings are taken as they are, so a health that drops from 300 to 50 still drops.

## Not done

- A **supported** condition from the investigation does not yet become a directed session (S1 steered to bring the
  condition about, then the rule checked again). The live loop above directs tests from the frontier's own hypotheses.
- The frontier's live findings have no replay `repro_check`.
- Calibration's measured speeds are not compared with declared ones beyond the guard tables' own (opposite moves were
  within 1.5% on both builds).
