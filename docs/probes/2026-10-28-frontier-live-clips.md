# Frontier live runs: clip capture, the rolling-window race and its fix

Issue #33. Five live runs on FPSKit commit `25e43fb`, Unity `6000.5.4f1`, each under an approved
envelope recorded beside the local run. The frontier model was `muse-spark-1.3` on Meta's Standard
tier at reasoning effort `minimal`; S1 was `mammoth-1` on Baseten; the entity and calibration inputs
were the #22/#23 records (SHA-256 `6e8a80ca…` and `5ce34167…`), unchanged across the runs.

| Run (UTC) | Plan | Turns | S1 calls | Frontier calls | Clips | Frontier failures | Muse cost |
|---|---|---:|---:|---:|---:|---:|---:|
| `issue33-live-01` 05:04 | 1 ep × 16 | 14 | 14 | 2 | 1 | 1 | $0.0205 |
| `issue33-live-02` 05:19 | 3 ep × 16 | 38 | 38 | 8 | 6 | 2 | $0.0609 |
| `issue33-live-03` 05:39 | 1 ep × 16 | 2 | 3 | 0 | 0 | — | $0 |
| `issue33-live-04` 05:46 | 1 ep × 16 | 11 | 11 | 1 | 1 | 0 | $0.0097 |
| `issue33-live-05` 05:49 | 4 ep × 16 | 44 | 44 | 6 | 6 | 0 | $0.0368 |

Every completed run checked all eight controls in every episode and finished. Adherence was 1.0 with
no violations in all nine completed episodes; 9 S1 turns were rejected as stale and recorded, not
suppressed. `issue33-live-03` stopped at turn 2 of 16 on a Baseten transport error (no HTTP status,
19 s hang) with no retry by design: 3 S1 reservations, no frontier call, no clip. Its retry is
`issue33-live-04`.

## The capture race

The bridge writes JPEG frames at 10 Hz into a bounded window of 120 frames and deletes its oldest
frame on every write. `capture_clip` listed the folder and then measured every listed JPEG, so a
name could be deleted (or delete-pending on Windows) before its `stat` ran. Any such frame aborted
the whole capture with `a capture frame changed while the window was read`. The three failures in
`live-01`/`live-02` left no clip folder at all, because the abort came before the output folder was
created: 3 of 10 calls, 1 of 2 and 2 of 8.

The fix in `src/playtest/frontier/media.py`:

- a listed frame that vanishes before it is measured is skipped and counted;
- a frame removed between the scan and its copy is skipped the same way, and copied files are
  renumbered so the concat list has no gap;
- a window with fewer than two measurable frames, or no distinct modification times, is scanned at
  most `WINDOW_ATTEMPTS` (three) times and then fails with the same concrete reason;
- a file that still exists but changed while it was read is an integrity failure and still fails
  closed;
- the bridge's `self-test.jpg` artifact in that folder is never treated as a game frame;
- `manifest.json` records `frames_skipped`: listed frames that could not be measured.

## Evidence after the fix

Seven frontier calls in `live-04` and `live-05`, seven clips, no failures. One call exercised the
race directly: `issue33-live-05` `e2-t1` lost a listed frame (`frames_skipped: 1`) and still
produced a valid 15-frame, 1.44 s clip. The regression test reproduces the same condition against
the old code, where it returns no clip and no folder. An offline stress with a 120-frame window at
40 Hz and a deliberately slow scan lost frames in 8 of 12 scans (2–3 each) and still produced 12 of
12 clips.

Clips are H.264 `yuv420p`, 640×360 with letterboxing, 1.44–3.96 s, no audio, durations from frame
modification times in a 4 s window clamped by the episode start. The two 1.44 s clips are first-turn
captures where the episode start cut the window short. `frame_game_s` is `null` and alignment with
the trace is approximate: modification times are the writer's completion times.

## Frontier answers and S1

All 17 raw answers across the completed runs were HTTP 200, parsed, and schema-valid against the
exact rendered schema (the requested schema hash matches every call). Fourteen carried a directive
with no checkable postcondition, so their outcome is `unknown`; two returned `directive: null` and
kept the current one. Six calls carried add/prune choices (one of them an added choice); none were
applied, because the runtime choice builder is deferred (#35).

S1 escalated 17 times across 107 completed turns, including three turns in a row in `live-02`
episode 2 after an answer had already named the next control. That is S1's decision; no code path
capped, suppressed or converted it.

## Cost

| | Reserved | Measured |
|---|---:|---:|
| Muse (17 calls) | $0.85 | $0.127942 |
| S1 (110 calls) | $0.110 | unknown |

The Muse figure is computed from token usage returned by Meta; the S1 figure is the reservation
estimate because Baseten returns no cost field. Per-run envelopes and their ceilings are in each
run's approval file; the largest was $3.27.

## Limits

- The post-fix sample is seven calls. It shows the skip path running live and the acceptance rule
  met; it does not establish a failure rate. Against the pre-fix 3-of-10 rate, P(0 failures in 7)
  is about 0.08.
- The race was observed once live. The offline stress and the regression tests carry the rest.
- Capture alignment is approximate; frame order and durations are modification times, not game
  time. A frame that the rolling window removes cannot be recovered.
- Directives were generic, so their usefulness and S1's judgment about when to escalate remain
  unmeasured. Directive scoring is #34 and the choice builder and investigation store are #35.
- S1's billed price is unknown.

## Local records

Full traces, frames and clips stay local under `runs/frontier/`; only this note is committed. The
traces were read for this note and hashed:

| Run | Trace SHA-256 | Python source |
|---|---|---|
| `issue33-live-01` | `a43eac60bd8a7dfcfc507c426424b5a0f6259038d1a7496ba5593edb046d3db4` | `251931a4…` (pre-fix) |
| `issue33-live-02` | `b6217328ab783c329e50e471dff391890e8922b358d1475f59ab91abc15d9777` | `251931a4…` (pre-fix) |
| `issue33-live-03` | `d8b28d3811c8b3859d8be28702dd3f7a18a63784aa5dae176fe109e73ce29363` | `83bf3fbe…` (post-fix) |
| `issue33-live-04` | `14eb0e5ebb3e49f68a5ea1e52b4e3e83cf6d09f95de6e432db0c27864ff4b4e8` | `83bf3fbe…` (post-fix) |
| `issue33-live-05` | `52222d8df991cb200738ab513242ea933bced2f96e442f85b31012c53b1bd839` | `83bf3fbe…` (post-fix) |

All runs started from repo commit `54c9c4b`. The game's package and `TimeManager.asset` files were
restored byte for byte after every run, the game working tree was unchanged, and no Unity process or
lockfile was left behind.
