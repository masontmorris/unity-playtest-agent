# d1-ultra frozen-frame probe: does a wider alignment tolerance in the objective stop the hunting? (31 October 2026)

**Predeclared reading: no wording effect.** Read by choice, the effect is smaller than the live trials
suggested, but it points the same way, and it can compound over a cycle.

## Setup

Script: `scripts/probes/probe_s1_tolerance_wording.py`. Results are in `runs/probes/s1-tolerance-wording/`
(local, with the plan, approval and ledger). Spend: 270 calls, 2,434,560 billed input tokens, about $0.102
at $0.042 per million. All 270 calls returned 200.

**Units:** recorded `next_action` turns from the 31 October FPSKit staircase trials, each replayed with its
exact JPEG. Each unit's recorded arm passed a hash gate: it rebuilt the recorded request exactly (29/29).
- **16 spawn turns** where S1 was hunting:
  - 8 `far` (yaw 37–47°), where continuing the hunt is `mouse_turn_left`;
  - 8 `near` (yaw within 10° of 0°), where `mouse_turn_right` turns toward the staircase.
- **13 `foot` turns** at the foot of the long flight, still at the approach heading (yaw 37–47°). There,
  `mouse_turn_left` lines up with the flight's axis.

**Arms:** only the objective's two alignment phrases change.

| Arm | Wording |
|---|---|
| `centre` | as recorded: "in the centre of the view", "rise straight ahead of you" |
| `third`, `half`, `two_thirds`, `view` | both phrases become "ahead of you, anywhere in the middle third / half / two thirds of the view / the view" |
| `half_approach` | only the approach is widened (middle half); the line-up keeps "rise straight ahead of you" |

Foot units were asked under `centre`, `half` and `half_approach` only. Each unit-arm had 2 repeats.

## Predeclared reading: no wording effect

The rule, fixed in `plan.json` before any call: an arm settles if its mean far p(`mouse_turn_left`) is at
most half the centre arm's, and its far p(forward) is at least 0.15 higher.

| Arm | far p(turn back) | far p(forward) | near p(turn toward) | near p(forward) | foot p(turn to align) |
|---|---:|---:|---:|---:|---:|
| `centre` | 0.296 | 0.199 | 0.376 | 0.120 | 0.187 |
| `third` | 0.275 | 0.218 | 0.365 | 0.146 | — |
| `half` | 0.247 | 0.235 | 0.366 | 0.153 | 0.187 |
| `two_thirds` | 0.267 | 0.237 | 0.351 | 0.162 | — |
| `view` | 0.284 | 0.231 | 0.363 | 0.151 | — |
| `half_approach` | 0.253 | 0.222 | 0.365 | 0.146 | 0.184 |

No arm comes close: the largest drop in p(turn back) is 0.05, and the largest rise in p(forward) is 0.04.
The foot line-up gain of `half_approach` over `half` is −0.003.

## Post hoc: the choice is the argmax of a noisy distribution

This reading was not predeclared.

In every one of the 270 calls, the option d1-ultra chose was the one with the highest returned
probability. Those probabilities vary between identical calls: in the finish-wording probe, the median
largest spread across repeats was 0.095. So what decides the choice is the *margin* between the two leading
options, and that margin is about as wide as the noise.

| Arm | far: chose turn back | far: mean p(turn back) − p(forward) | near: chose turn toward | foot: chose turn to align |
|---|---:|---:|---:|---:|
| `centre` | 14/16 | +0.097 | 15/16 | 3/26 |
| `third` | 14/16 | +0.056 | 12/16 | — |
| `half` | 12/16 | +0.012 | 13/16 | 3/26 |
| `two_thirds` | 12/16 | +0.029 | 13/16 | — |
| `view` | 14/16 | +0.054 | 14/16 | — |
| `half_approach` | 11/16 | +0.030 | 15/16 | 6/26 |

The wider wordings shrink the far margin from about 0.10 to 0.01–0.03, and the turn back is chosen a
little less often. Each difference is 2–3 calls out of 16, well inside noise, and nothing here is
significant.

A small per-decision change can still matter over a cycle. If each far decision breaks the hunt with
probability q, a hunt lasts about 2/q turns. A q near 0.12 gives about 16 turns, and a q near 0.25–0.3
gives about 7. The two live runs with the centre wording had longest cycles of 29 and 13; the two with the
half wording had 5 and 7. That fits a modest per-decision shift compounding. It is not evidence of a large
one.

At the foot, wording barely matters. d1-ultra prefers walking on (p 0.30) or jumping (0.14) to turning
onto the flight's axis (0.19) under every arm. `half_approach` chose the turn on 6 of 26 calls, against 3
of 26, which is again within noise.

## What it means

- The objective's tolerance wording is a weak lever on any single decision. The predeclared verdict
  stands: no wording effect.
- The live improvement is consistent with a small push near a tie, repeated each turn. So the half
  wording is a reasonable default for these trials. It costs nothing, and the over-widening at the near
  heading is small: turn toward 15/16 → 12–14/16.
- Hunting's root cause is untouched: 38.4° turns against a target near 27°. Finer, calibrated turns would
  remove the tie rather than nudge it.

**Lesson for the next probe:** predeclare on the *chosen action's share* or on the *margin*, not on mean
probabilities. Then size n for the change that matters, e.g. 12% → 25% breaks, which needs well over 100
calls per arm.
