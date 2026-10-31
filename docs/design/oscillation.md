# S1's left/right turn oscillation, and the opt-in pattern fact

## What happens

In the 31 October FPSKit staircase trials, S1 (Liquid `d1-ultra`, a one-pass decision model with no
memory between calls) spent much of its time turning back and forth in place:
- `climb-stairs-02`: 18 of 43 turns.
- `climb-continuous-01`: 60 of 104 turns, including the first 25 in a row.

Each turn skill also takes about 0.72 s to execute, so the oscillation costs time as well as turns.

The trace shows an exact period-2 cycle. `mouse_turn_right` and `mouse_turn_left` each rotate the view by
exactly 38.4°, so the heading went 0.0°, 38.4°, 0.0°, 38.4° for 25 turns. The goal was the long
staircase, whose foot is at bearing 40.9° from the spawn: at 38.4° it was only 2.5° off-centre, and S1
turned back anyway.

## Why: a limit cycle

This is a classic control failure with three ingredients:
1. **Quantized actuators.** The manifest offers mouse turns of ±38.4° and a right stick look calibrated at
   36.2°, which turned 37–47° in play. The left look has no calibrated effect. Nothing finer is offered.
2. **An extended target.** At 38.4° the long flight runs from about 2.5° right to about 25° left of
   centre, so its visual centre is near 27°. At 0° it lies 14° to 41° to the right, centred near 27°.
   No heading one choice away centres it: 27° lies between 0° and the 36–38° that one turn or look gives.
3. **A memoryless greedy chooser.** At 0° the staircase looks right, so S1 turns right. At 38.4° most of
   it looks left, so S1 turns left. Each decision is locally sensible, and none of them knows the last
   one.

## Remedies, and how they fit the project rules

| Remedy | Fits? | Status |
|---|---|---|
| **Memory, as a measured fact:** state the cycle so S1 can see it | yes: "code exposes facts", with no advice | built: `--pattern-facts` |
| **Finer actuators:** a turn of a few degrees | yes, if honestly measured | design only (below) |
| **Commitment or hysteresis in code:** block the reverse turn, or delay it | **no**: code would be choosing, and S1 owns choices | rejected |
| **Objective wording:** name the aim point, e.g. "face the bottom step" | yes, per objective | per run |

## The pattern fact (`--pattern-facts`, `-PatternFacts`)

`runner/pattern.py::PatternFacts` wraps the facts source the way `CoverageFacts` does.
- **Trigger:** when the executed, non-rejected choices end in four or more turns alternating between
  exactly two choices, it appends one line.
- **Content:** which two choices alternated and over how many turns; then, for up to three of the
  manifest's declared scalar readers that changed in that window (the most changed first), how they moved
  and their net change.
- **Formatting:**
  - readings are rounded to 0.001, so the float noise of a reversed turn (3.4e-06) reads as 0.0;
  - an exact two-value cycle is stated once;
  - a long, irregular sequence is stated as its range.
- **No advice:** a test checks for words like "should", "try" and "avoid".
- **Off by default:** the plan records `pattern_facts` only when it is on.

What S1 would have seen in `climb-continuous-01`, replayed offline with manifest 2's readers (turns are
zero-based):
- Turn 4: "Your last 4 executed choices alternated between mouse_turn_right and mouse_turn_left. Over
  those turns public.yaw read 0.0, 38.4, 0.0, 38.4 and now 0.0 (net change 0.0). Over those turns
  public.jump_count read 1.0, 0.0, 0.0, 0.0 and now 0.0 (net change -1.0)."
- Turn 8: "… public.yaw alternated 0.0 and 38.4 (8 readings) and now 0.0 (net change 0.0). …"
- Turn 16: "… public.yaw alternated 0.0 and 38.4 (16 readings) and now 0.0 (net change 0.0). Over those
  turns public.jump_count ranged 0.0 to 1.0 over 16 readings and now 0.0 (net change -1.0)."

## Live check (`climb-held-pattern-01`, 31 October)

The fact appeared on 22 turns. Where a cycle of four or more stood, S1 continued it 16 times out of 20
(80%). In `climb-continuous-01`, without the fact, it continued 32 of 36 (89%). The longest cycle fell
from 29 turns to 13.

Twice, S1 kept the cycle going for 9 and 6 turns after the fact first showed. Once, it broke the cycle on
that first turn. The runs took different paths and this one had three restarts, so this is an observation,
not a test. It reads as no clear effect: stating the cycle does not by itself stop d1-ultra turning.

## Live check of the deadband (`climb-tolerance-half-01`, 31 October)

The objective's two alignment phrases were widened to "anywhere in the middle half of the view", and
nothing else changed. S1 alternated five times, then walked at 38.4°. It turned once to the flight's axis
and climbed to the top of the long staircase (feet 8.5) within 15 s. The longest cycle was 5 turns,
against 13 and 29 before. This is one run, so a frozen-frame probe of several widths is being built to
measure the effect turn by turn.

A second run with the same objective (`climb-tolerance-send-01`) cut the hunting short again, with a
longest cycle of 7 turns. But it missed the climb. At the foot of the flight, still at 38.4°, S1 walked on
instead of turning to the flight's axis.

That points at two tolerances, not one:
- **Approach (pointing at the foot).** It can be wide, since each later decision corrects the error.
- **Traverse (walking up a 2-unit-wide flight).** It cannot. A heading 38° off the axis leaves the side
  within a few units.

The traverse's axis is a reachable heading (0°), so its tolerance can stay tighter than half a step and
still settle. The next wording to test keeps the approach wide and the line-up tight.

**Frozen-frame check** (`docs/probes/2026-10-31-d1-ultra-tolerance-wording.md`, 270 calls, $0.10): the
predeclared reading is no wording effect. Widening the tolerance lowered the far-heading p(turn back) by at
most 0.05.

But d1-ultra's choice is always the argmax of a distribution that varies by about 0.1 between identical
calls. So the margin between turning back and walking on matters more than either probability:
- the margin shrinks from +0.10 to about +0.01–0.03;
- the turn back is chosen 11–12 times in 16, against 14 in 16.

That is a small per-decision push, inside noise. Repeated every turn, it roughly halves a hunt's expected
length (about 2/q turns), which fits the live cycles of 5 and 7 against 13 and 29. Wording barely changes
the line-up at the foot.

## The deadband reading

A controller whose tolerance is tighter than its smallest step hunts forever. The standard cure is a
deadband at least half a step wide. Here the objective sets S1's tolerance: "Turn until the bottom of the
staircase is in the centre of the view." With 38.4° turns no heading near the spawn centres it, so every
view says "turn" again.

Two remedies follow, and both fit the rules:
- **Widen the tolerance in the objective's wording.** This is per run and needs no code. The half-step is
  19.2°, and the view is about 81° wide, so the deadband needs to be at least the middle half of the view
  (±20°). For example: "turn until the bottom of the staircase is ahead of you, anywhere in the middle half
  of the view". At 38.4° the foot is 2.5° off centre and the flight's visual centre is 11.4° off, so that
  heading satisfies it.
- **Shrink the step** with a calibrated finer turn (below).

## Finer turns: measured both ways and built (`--turn-sizes`)

An earlier version of this note said a shorter mouse turn's angle could not be known without new
calibration. That was wrong: manifest 2's calibration already measured both look controls.
- **One mouse impulse** (64 px, x=+1) turns 7.68° with zero spread at every hold. The 38.4° skill is five
  of them with 0.1 s waits between, which is why it takes about 0.72 s.
- **The gamepad right stick** (x=+1) turns about 365° a second: 18.4° at 0.05 s, 36.2° at 0.1 s, 74.1° at
  0.2 s and 144.2° at 0.4 s, each with a spread of 4° or less.

What is missing is the other direction. Calibration measured only the declared sign (+x), so no left turn
has a measured angle. The five-impulse skills' labels also tell S1 nothing about their size.

Both are now built and validated in Unity (17feee5).
- **Calibration of both signs** (`calibrate-preflight.ps1 -BothSigns`, run on FPSKit b01, 31 October). The
  left measurements match the right within 0.2°: mouse impulse −7.68°; stick −18.2°, −36.2°, −72.2° and
  −144.3°.
- **Offline update.** `python -m playtest.preflight.update_calibration` updated manifest 2 to
  `manifest-3.json`, with no model calls. As a result the stick's left look is now labelled with its measured
  −36.2°.
- **`--turn-sizes`** offers S1 ten measured turns: 7.7°, 18°, 36°, 72° and 144°, each left and right. Each is
  one input, about 0.05–0.4 s, against 0.72 s for the five-impulse skill. A longer hold whose measured turn
  is smaller than a shorter one's has wrapped past a full circle, and is not offered: the 1.2 s left hold
  read 72.9°.

With a 7.7° step the half-step is 3.8°, so no reasonable tolerance can hunt.

**Live check, and a correction.** That last sentence assumed S1 would use the smallest step. In
`climb-turn-sizes-01` (turn sizes, original "centre" wording) it never chose the 7.7° turn. It hunted with
the 18° stick turn instead, between headings about 18° apart around the staircase. 91% of its choices were
turns, against 62–70% with two turn options.

So the deadband condition applies to the step the chooser actually uses, not the smallest one offered:
tolerance ≥ half of that step. The finer menu also adds an option-count pull toward turning. The
deadband-consistent pairing is the turn sizes with a "middle half" tolerance (±20° ≥ 9°).

Contributors: gpt-6.1-terra (first implementation and replay), with the orchestrator adding the
rounding, compression, range summary, tests and this note.
