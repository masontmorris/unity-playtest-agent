# System Zero on the FPSKit S1 bench (31 October 2026)

Branch `feat/system-zero-s1`, design in [system-zero-s1.md](../design/system-zero-s1.md). Gates G1 (image
acceptance) and G2 (frozen-corpus replay) ran on RightNow's `system-zero-2.0.0`, under the user's approval of
$0.50 for both. 142 calls, all HTTP 200, none retried. **Measured cost $0.09**: the credit balance read $5.00
before and $4.91 after (`GET /v1/credits`, whole cents).

## G2: same request bytes as d1-ultra, only the model differs

Both Day 3 corpora offer 10 choices (8 actions, `wait`, `finish`), which fits System Zero's 10-option text
limit. So every request was the flat System One shape, and each frame's state rebuilt byte for byte to its
recorded hash. The comparison with d1-ultra's 29 October bench is therefore a model comparison on identical
requests; the two runs are two days apart. Protocols and seeds as before: 50 noise repeats on 10 frames
plus a 40-frame sweep (seed 24), and the shuffled re-test (seed 2410). Scored against the same labels.

| | d1-ultra (29 Oct) | **system-zero-2.0.0** |
|---|---:|---:|
| First bench, acceptable top choice | 40/40 | **7/40** |
| First bench, acceptable mass, mean | 0.893 | 0.339 |
| Noise repeats, adjacent flips | 0/40 | 0/40 |
| Shuffled re-test, acceptable top choice | 45/45 | **19/45**: checks 14/40, terminal 5/5 |
| Re-test misses | none | premature `finish` 22, re-check 4 |
| Re-test acceptable mass, mean / minimum | 0.888 / 0.739 | 0.457 / 0.020 |
| Same request sent twice | same choice 5/5, within 0.039 | identical distribution 5/5 |
| Median / p95 latency | 203 / 250 ms | 296 / 437 ms (first); 297 / 343 ms (re-test) |

System Zero chose `finish` on 33 of the first bench's 40 frames, usually with half or more of the checklist
undone. Its distributions are valid and the decode matches its own `decision` field, so this is the model's
choice, not a parsing fault. Read against the predeclared rule (docs/design/s1-bench.md): on this task and
request format, System Zero isn't acceptable as S1's chooser.

**What this does not settle.** System Zero's docs say to keep application data in `state` and the task in
each question's `instructions`. Our shared request puts the objective in `state` under generic instructions.
A format comparison (the task in the question's instructions, everything else fixed) is a separate
experiment and hasn't been run. Like the earlier benches, this is one checklist task in one game.

## G1: the layouts and the game-view image

Two calls through the production adapter, the second with the 640×480 JPEG the d1-ultra image probe used
(SHA-256 `fa7eba80…`). The image allowlist entry existed only in that process.

| Call | Questions | HTTP | Latency | Input tokens | Media | Decoded |
|---|---|---:|---:|---:|---|---|
| 22 choices, text only | two-level: root 4, groups 7, 7, 4, 4 | 200 | 563 ms | 2,305 | none | yes |
| Same choices + JPEG | one flat question, 22 options | 200 | 1,578 ms | 2,103 | 1 item, 300 tokens | yes |

Both layouts were accepted, and the image was taken in (`media_items: 1`), so `system-zero` joined the
runner's image allowlist with 300 tokens as its pacing estimate. This doesn't show the model used the image.
One image call isn't a latency sample.

## Records

Local under `runs/bench/system-zero/`: approvals, ledgers, call rows, reports and both balance reads.
Cached input was 28,512 of 266,790 tokens (11%).
