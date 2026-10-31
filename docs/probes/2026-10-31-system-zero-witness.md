# System Zero witness, offline check (31 October 2026)

Scope and predeclared bar: [system-zero-witness-check.md](../design/system-zero-witness-check.md). The user
approved $1.75. **Spent $0.56** (balance $4.73 → $4.17). 30 billed calls, all HTTP 200. Two earlier attempts
were refused and not billed; see "Transport" below. Model `system-zero-2.0.0`, video input (preview).

**Verdict: System Zero isn't usable as a video witness.** Part 2 failed its bar. A perception diagnostic then
found it reports no camera motion at all, so Part 1 (the scripted bug clips) wasn't run.

## Part 2: 24 windows from recorded sessions

Twenty negatives from b04-build sessions where b04 never showed: three hard ones (a stair climb, a walk off
the stairs' top, 98 turns pressing a wall) and 17 random windows by a seeded rule. Four exploratory windows:
the session 7 fall out of the world, and three Day 6 frozen-pickup moments. Each window is 9.5 s, sent with
the inputs the trace recorded in it.

| | Result |
|---|---|
| Negatives flagged (`problem_visible` true) | **5/20** (bar: ≤ 3/20) |
| P(problem), all 24 windows | 0.41–0.56, median 0.46 on negatives |
| `kind` | "none" on all 22 valid answers; 2 had no valid answer |
| `when` | "none" on all 24 |
| Exploratory (fall, frozen pickups) | not flagged, P 0.43–0.44 |
| Latency | 0.7–2.5 s |
| Usage per call | `media_items` 1, 1,960 media tokens (20 frames), ~2,750 input tokens |

The five "flags" sit at P 0.51–0.56, each with `kind` and `when` "none" in the same reply, so they are noise
around 0.5. The frames were taken in, but nothing separated one window from another.

## Perception diagnostic (added before Part 1, bar declared first)

Six of the same clips, with five narrow boolean questions each: did the view travel, rise, drop, turn more
than 30°, or leave the level? The truth came from the trace's camera readings. Readings in a borderline band
were unscored, which left 27 judgments. Bar: ≥ 90% means perception works and the broad question was the
problem; ≤ 70% means the video channel isn't usable. 19 of the 27 truths are "no", so always answering "no"
scores 70%.

**Result: 19/27, exactly the always-"no" baseline. It caught 0 of the 8 "yes" cases.**
- "Did the camera travel?" got P 0.07–0.45 on four clips that all travel: a 10-unit climb, a 7-unit walk-off,
  a 4-unit walk, and a 170-unit fall.
- Looking at the frames myself confirmed the climb clip visibly travels toward and up the staircase, so the
  timing alignment holds.
- The fall clip is visually weak evidence. Below the floor the view is sky over a flat plain with a level
  horizon.

## Transport (recorded for anyone sending video to System Zero)

- A 10.04 s container was refused, `413 media_too_large`. SDK 0.4.2's `VIDEO_PROFILE` caps video at 10 s.
- A High-profile, variable-frame-rate cut with B-frames was refused, `422 media_decode_failed`.
- Constrained baseline, no B-frames, constant 10 fps, no edit list was accepted. That's now
  `runner/witness.cut_clip`, which measures each clip's duration with ffprobe.
- A video request's choice questions take at most 10 options (`VIDEO_PROFILE.choice_options`), not the 26 an
  image request allows.

## What this does not settle

- One model version with video in preview, one game's render, 9.5 s clips at System Zero's fixed 2 frames a
  second.
- A future version, or another video model, might perceive motion. The paired scripted-clip design (Part 1)
  is model-agnostic and still the right test for one.
- Part 1's tooling worker was stopped before it finished; its partial files are in its own worktree.

## Records

Local under `runs/bench/system-zero/`: `witness/` (windows, clips, call rows, the two refused rows),
`witness-perception/` (plan with truths, call rows), the approval, and balance reads. Scripts:
`scripts/probes/system_zero_witness.py` and `system_zero_witness_perception.py`. Module: `src/playtest/runner/witness.py`.
