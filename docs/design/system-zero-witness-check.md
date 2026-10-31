# System Zero witness: offline check (scope)

Branch `feat/system-zero-s1`, 31 October 2026. **Status: run and stopped, $0.56.** Part 2 failed, a perception
diagnostic found no motion perception, and Part 1 was not run ([record](../probes/2026-10-31-system-zero-witness.md)).
One correction to the scope below: a video request's choice questions take at most 10 options, not 26. It follows the
[format probe](../probes/2026-10-31-system-zero-format.md). System Zero read the state of a frame with 240/240
accuracy, but failed as S1's chooser in every format tested. The witness uses the half it's good at.

## What the witness is

A clip reviewer that never acts. Every ≤ 10 s of play, one HTTP decide sends a silent H.264 clip as the only
attachment, with a short state: the clip's span and speed, and the inputs executed in it, from the trace or the
scripted plan. It asks three fixed, name-blind questions:

| Question | Type | Options |
|---|---|---|
| `problem_visible` | boolean | Does the clip show the game behaving wrongly, in a way a player would report? Judge only the frames, against the listed inputs. |
| `kind` | choice | none; movement blocked; object frozen or not animating; input had no visible effect; view changed without a matching input; object vanished or appeared; passed through something solid; visual glitch; other |
| `when` | choice | 0–2 s, 2–4 s, 4–6 s, 6–8 s, 8–10 s, none |

The answers become `witness` trace entries: the clip's SHA-256, its seq range, every answer and its
distribution, and any refusal (a refusal is `unknown`, never "no problem"). No code reads them to open help,
rank or promote a finding. In this offline check nothing reaches S1 at all.

The `kind` options are a generic list of player-visible symptoms, fixed before any clip is seen. They are a
secondary measure, not part of the pass bar.

## The clips and their labels

Labels must come from measurement, not from someone watching videos. Two sources:

### Part 1: paired scripted clips (the main test; gold labels)

For each readable development bug, run the dev key's trigger as a fixed hold script (`bench/scripted.py`,
no model) on the bug's branch and on the unseeded base commit `25e43fb`, with the session recorder running.
The label is the build. Each run is confirmed by the key's measured signature; a run whose signature doesn't
match its build is excluded and reported, never relabelled.

| Bug | Trigger (dev key) | Visible symptom | Confirming signature |
|---|---|---|---|
| b01 | Shift alone, then Shift+W, +S, +D | view widens with no movement; no sprint going forward | `m_Dash`, camera FOV |
| b02 | walk across a weapon pad, wait ~5 s, walk back | the item returns frozen (no spin or bob) and can't be collected | `m_RespawnTTL` |
| b03 | fire held while jumping | recoil stops mid-air | `m_Pitch` |
| b04 | the #194 push into the 2-unit flight's 0.25 riser | stopped at the first step; base climbs | entity y |

That's 4 bugs × 2 builds × 3 runs = 24 clips, each cut to the trigger window. Each clip also goes at half
speed, because System Zero samples only 2 frames a second: b03's 0.6–0.9 s jump gets 1–2 frames at normal
speed and 3–4 at half. The state says the clip is slowed.

**Feasibility.** b01, b02 and b04 use declared keyboard actions and chords. b03 needs the mouse button, which
no session catalog declares yet. If the bridge can't press it, b03 drops out and Part 1 is 18 clips.

### Part 2: existing session footage (realistic false alarms)

`runs/b04-hunt/session-*/recording/session.mp4` (640×480, 5–10 min each) is b04-build play in which b04 never
showed: no session pushed a 0.25 riser with a walkable tread. Twenty 10 s windows, drawn by a seeded rule
from trace-measured stretches, make the false-alarm set. They include hard negatives a naive detector would
flag: session 9 climbing the main flight, session 9 walking off its top (an ordinary fall), and session 10
pressing a 1.1-unit wall for 98 turns (correct blocking).

Exploratory items are reported but kept out of the pass bar:
- Session 7 falling out of the world (camera y -3235), which may be a real base-game defect.
- About 6 Day 6 windows (b02 build) where the trace puts the player at the pads after an item returned. Whether
  the item is on screen isn't measured, so these can't be fair positives.

## Predeclared measures (to freeze before Part 1's first call)

- **Primary:** on Part 1, `problem_visible` true on bug clips (sensitivity) and on base clips (false alarms),
  per bug, normal and half speed.
- **Secondary:** Part 2 false alarms; `kind` and `when` agreement on bug clips; latency; refusals.
- **Bar for a live trial:** at normal or half speed, ≥ 9/12 bug clips flagged (or ≥ 7/9 without b03), with
  ≤ 2 of the paired base clips and ≤ 3/20 Part 2 windows flagged. Then the next step is one live d1-ultra
  session with the witness beside it, S1 unchanged. Otherwise the witness isn't worth a live trial on this
  evidence.
- Inconclusive is allowed. One game, four seeded bugs, all from the development set.

## Cost

| Part | Calls | Estimate |
|---|---:|---:|
| 0: one acceptance call on a real 10 s window (video is in preview) | 1 | $0.02 |
| 1: 24 clips × normal and half speed | 48 | $0.90 |
| 2: 20 negatives + ~8 exploratory | 28 | $0.52 |
| **Total** | **~77** | **~$1.45** (ask for $1.75) |

Each call is about $0.0187: a $0.018 clip, three questions at $0.00013, ~800 text tokens and the call fee.
The scripted Unity runs make no model calls. The balance is $4.73.

## Work

1. `runner/witness.py` (request, decode, trace entry) and a clip cutter (ffmpeg: H.264, no audio, ≤ 10 s,
   ≤ 2.5 MiB, optional half speed), with offline tests. Then Part 0.
2. Part 2's window selection from the traces, then Part 2.
3. Part 1's Unity clip production: five builds, serialized under the one-Editor rule, with each FPSKit branch
   switch done and undone the way the b04 hunt did. It is the longest step and suits a mid-tier worker.
4. Score both parts and write the record.

## Rules this keeps

- **S1 ownership:** the witness never selects, ranks or escalates; its entries are evidence only.
- **Blindness:** only the readable development bugs and the base build are used. The eval key and its bugs
  (b05, b07, b08) are untouched, and nothing here is tuned on them.
- **Measured, not inferred:** labels come from builds and readers; a refusal or unknown stays unknown.
- **Budget before spend:** the user approves the total before Part 0.
