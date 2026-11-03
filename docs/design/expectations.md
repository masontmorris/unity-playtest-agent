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

Code screens the answer. A prediction is set aside, kept but never scored, when it names a verb or an observable
nobody measures, when `reaches` comes without a value, or when it needs a condition the guard states do not give (a
target in range, ammunition). That last rule alone removes the v2 class of "interact with nothing in range".

## How code scores it (`score`, `score-play`)

A guard-table cell is one of:
- **held**: the effect was seen, the right way;
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

## In play (`--expectations`)

With `--measure-guards`, a session scores the predictions against its own guard table before play and saves
`expectations-unmet.json`. Each unmet line reaches S1 as a measured fact every turn and the frontier with the guard
facts. Nothing is decided in code: S1 weighs it as any other fact.

## What it found on the held-out builds (2026-11-03)

The clean builds' tables and traces were scored with the same predictions as a false-positive check. That check is
for measuring this method only; a run never uses a control.

| | Held-out | Clean |
|---|---|---|
| FPSKit, guards (3 fresh sessions each) | aim_toggle at rest: aiming and the declared aim FOV 50 both missing 3/3 | 0 mismatches |
| FPSKit, play (8 held-out, 7 clean sessions) | pitch within its declared ±80, jump count within its declared 2: held | held |
| DragonBlaze, guards (3 fresh sessions each, in-air presses at the measured apex) | 0 mismatches; the double jump and the rest hold | 0 mismatches |
| DragonBlaze, play (11 held-out, 12 clean sessions) | health within its declared 30: held. When health drops, invulnerability follows in 23 of 25 observable hits: mixed | 6 of 6 |

The first version of the prompt also predicted direction-specific effects ("dash works in every direction") and the
scorer had none of the cell outcomes above. Scored on the clean builds, it raised 6 FPSKit and 7 DragonBlaze
mismatches, every one an artifact or a design rule. Each fix above is generic and came from those clean-build false
positives, not from the answer key: the clean builds were this method's development set.

## Not done

- A **mixed** expectation is a candidate for a controlled test the frontier directs: what differed between the
  triggers that were followed and the ones that were not. That step is not built.
- Calibration's measured speeds are not compared with declared ones. Opposite moves were within 1.5% on both builds,
  so a symmetry relation over them found nothing.
- `--expectations` has unit tests but has not run in a live session.
