# FPSKit: S1 on Liquid's d1-ultra, a first live trial (2026-10-29)

Issue #138. S1 has run on Baseten's `mammoth-1` since Day 3. Its latency varies from run to run, and on the
#133 validation run its p90 call took 1.4 s. At the 0.8 s age limit, 34 of 78 actions were rejected as stale
(#136). The owner asked for a trial of Liquid's `d1-ultra`, a decision model no bench here had run. This note
records the smoke test and one live session. It is descriptive: one unpaired run per model, an hour apart, and
**no decision-quality comparison**.

## Setup

- **Arm.** `liquid-d1-ultra` (#138): model `d1-ultra` on Liquid's decisions endpoint, in the same request shape
  as every S1 arm. Liquid reports `usage.input_tokens` and `usage.cost` on every call.
- **Smoke test.** Five S1 contexts recorded in the #133 run were replayed through d1-ultra.
  - All five answered HTTP 200 with an offered choice; the resolved model was `d1-ultra`.
  - Calls took 250–359 ms at 3.7k–6.1k input tokens.
  - Liquid's reported cost was exactly $0.042 per million input tokens, $0.0011 for the five calls.
- **Trial.** `runs/issue138/run-01`, local.
  - The same FPSKit base build, Day 4 inputs, objective (help sentence included), Standard frontier tier, pacing
    (85,000 tokens a minute), 0.8 s age limit and recording as the #133 run. Only `-S1Arm` changed.
  - Approval: the owner approved $1.55.

## Results

| | mammoth-1 (#133 run) | d1-ultra (this trial) |
|---|---|---|
| Ending | S1 chose `finish`, turn 105 | S1 chose `finish`, turn 90 |
| S1 calls | 111 | 96 |
| S1 input tokens, median / max | 5,050 / 6,185 | 5,000 / 6,531 |
| S1 call, median | 594 ms | **297 ms** |
| S1 call, p90 | 1,375 ms | **391 ms** |
| S1 call, p95 | 1,657 ms | **578 ms** |
| S1 call, slowest | 11,500 ms | 4,938 ms |
| Turns rejected as stale | 31 of 105 (30%) | **0 of 90** |
| Action attempts rejected | 34 of 78 | **0 of 64** |
| Action age at the bridge, median / p95 | 0.65 / 1.69 s | **0.32 / 0.43 s** |
| Actions executed | 44 | 63 |
| Invalid answers / provider failures | 0 / 0 | 0 / 0 |
| Adherence | 1.0 | 1.0 |
| Escalations / frontier calls | 6 / 5 | 6 / 6 |
| Directives (outcomes) | 5 (5 met) | 6 (5 met, 1 not_met) |
| Directives tested by S1's input | 2 of 5 | 1 of 6 |
| S1 cost, measured | unknown (price unpublished) | **$0.021** |
| Frontier cost, measured | $0.052 | $0.057 |

- **Integrity.** `playtest-trace verify` found 0 problems. Every held input was verified as released, the game
  worktree did not change, and the recording has 3,620 frames, none missed, 367 s of video.
- **Freshness.** Every d1-ultra action reached the bridge under the old 0.5 s limit, at a p95 of 0.43 s.
- **Where S1's turns went.** S1 moved forward 38 times and waited 20 times. It sprinted 8 times, looked around
  12 times, escalated 6 times, moved back 5 times and finished once.

## What it shows, and what it does not

- **Latency and freshness.** In this run, d1-ultra was about twice as fast at the median and three to four times
  faster at the tail. No action went stale. Baseten's latency varied within the hour before, so one run each
  cannot say how stable either provider is.
- **Cost.** About $0.0002 a call, confirmed by Liquid's own per-call cost: a 10-minute run is a few cents.
- **Not shown: decision quality.** The Day 3 S1 bench ranked `liquid-d1.1` well below `mammoth-1`: 19 more
  mistakes in 40 labelled frames, and 14 of 40 top choices flipped on repeat (`docs/design/s1-model.md`).
  d1-ultra has not been on that bench. A live run has no labels, so "it played sensibly" is not evidence. The
  smallest check before making it the default is the same 40-frame bench at a few cents.
- **Directives.** Most were still not really tested:
  - 4 of the 6 were met with a window of 0 turns, because the frontier asked for values that already held;
  - 1 was `not_met` with `error_class` `no_attempt`, because S1 waited out its two turns.

  That concerns the frontier's setpoints and S1's choices, not the S1 provider.
- **Data.** The S1 context, game-state text with no images, went to Liquid, as the Day 3 bench's did. Liquid's
  data-use terms have not been checked.
