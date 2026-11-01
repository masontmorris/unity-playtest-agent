# d1-ultra frozen-frame probe: can S1 point at a target in the game view? (31 October 2026)

**Reading: yes, to within one strip, but it does not say when the target is out of view.** With the image
and the question alone, it "points well". In the full in-play request it reads "partly", because its slight
leftward bias of −0.53 strips is just past the ±0.5 bar. Within-one accuracy was 29 or 30 of 30 frames.

## Why

Relative turns of a fixed size hunt whenever the remaining error is smaller than the step.
`docs/design/oscillation.md` covers the trials and the deadband reading.

Pointing is the generic alternative: S1 says *where* in the image the target is, and a measured skill turns
that far. The skill uses the camera's field of view, which the bridge reads in any game, and the calibrated
turn rates. This probe tests the one assumption that design rests on: that S1 can localize the target in
the image.

## Setup

Script: `scripts/probes/probe_s1_pointing.py`. Results are in `runs/probes/s1-pointing/`: plan, approval,
ledger and results, local only. Spend: 68 calls, 364,973 billed input tokens, $0.0153 as reported by Liquid.
All calls returned 200.

**Frames:** 34 distinct recorded `next_action` turns, each with its exact JPEG, from five staircase trials.
- They come from the spawn and from the ground between the spawn and the foot.
- Repeated views were dropped.
- Each recorded request passed the hash gate (34/34).
- 30 frames had the foot in view, spread over all nine strips. 4 did not.

**Ground truth:** the long flight's bottom step is at x 8.5, z −10.6. That position comes from where the
player's feet measurably rose onto the flight across the trials:
- the flight rises along +z;
- feet x spanned 7.6 to 9.5 on it;
- y first leaves the ground near z −10.7.

The foot's strip in each frame follows from the recorded camera position, the player's yaw and the vertical
field of view (65°), on the 640×480 frame. Frames whose foot lies within 3° of the view's edge were left
out. The position is uncertain by about ±0.5 units, which is about ±4° from the spawn. That is why
within-one-strip is the primary measure.

**Question:** "Divide [the image] into nine equal vertical strips, numbered 1 at the left edge to 9 at the
right edge. Which strip contains the bottom step of the long staircase … ? Choose not_visible if its bottom
step is not in the image." It was asked once per frame in each of two conditions:
- `in_play`: the exact recorded request, with `foot_strip` as a second question beside `next_action`;
- `minimal`: the image and the question only.

## Results

| Condition | Exact | Within one strip | Bias (strips) | Mean abs error | Predeclared reading |
|---|---:|---:|---:|---:|---|
| `minimal` | 19/30 (63%) | 30/30 (100%) | −0.37 | 0.37 | points well |
| `in_play` | 15/30 (50%) | 29/30 (97%) | −0.53 | 0.53 | partly (bias just past 0.5) |

By true strip, the answers were:

| True strip | `minimal` | `in_play` |
|---|---|---|
| 1 | 1 | 1 |
| 2 | 1, 1 | 1, 1 |
| 3 | 3, 3, 3 | 3, 3, 3 |
| 4 | 3, 3, 3, 4, 4 | 3, 3, 3, 3, 4 |
| 5 | 5 ×5 | 4, 5, 5, 5, 5 |
| 6 | 5 ×5 | 4, 5, 5, 5, 5 |
| 7 | 7 ×5 | 7 ×5 |
| 8 | 8, 8, 8 | 7, 7, 8 |
| 9 | 8 | 8 |

The bias is about one strip to the left in strips 2, 4, 6 and 9. The ground truth may account for part
of it: the one climb that went straight up the flight held x 8.17, not 8.5, and that alone moves the
foot's bearing from the spawn about 2° to the left.

**Out of view, it always picks a strip.** On all 8 out-of-view calls, S1 named a strip, giving `not_visible` a
probability of 0.04–0.22. The scene has five staircase objects, so it may be pointing at another one. Either
way, S1 does not reliably report that its target is absent.

## What it means

- Pointing is a viable, game-agnostic aiming primitive for d1-ultra. One strip is about 9–11° here, and
  the mean error is about half a strip, roughly 4–5°. At the spawn's distance, that lands within the
  2-unit flight.
- The design follows: offer "face strip k" choices, each a turn by the strip centre's angle. The turn is
  computed from the observed field of view and built from measured turn inputs, so no angle is inferred.
  S1 still chooses; one pointing turn lands the target near the centre. Pointing has no fixed step, so the
  deadband problem that made fixed-step turns hunt does not arise.
- Searching needs its own choice, such as a large measured turn, because S1 will not say "not visible".
