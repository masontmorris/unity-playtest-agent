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

## Finer turns: design only

The manifest's turn skills are five identical mouse impulses. A skill that ran k of the 5 would turn by
some smaller angle, but that angle cannot be inferred from the full skill's 38.4°, because mouse
smoothing and acceleration make it non-linear. Under "measured, not inferred", it could only be offered
labelled as uncalibrated. The honest fix is a preflight calibration step that measures a short turn and
publishes it like any other skill. Until then, finer turns stay out.

Contributors: gpt-6.1-terra (first implementation and replay), with the orchestrator adding the
rounding, compression, range summary, tests and this note.
