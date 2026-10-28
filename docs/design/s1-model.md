# S1 model decision

Issue #25. **Status: provisional.** Mason Morris chose `mammoth-1` on 27 October 2026 on the evidence
below, and #25 closes on that provisional choice. Three conditions are open; each says what would
change the choice and what the fallback is. #100 revisits them on Day 5.

## Decision

S1 is `mammoth-1`: `decision-models/baseten-mammoth-27b-latest` on Baseten's System One endpoint
(`inference.baseten.co/v1/systemone`), with text state and optional image input.

## Evidence

The build bible picks S1 on top-1 acceptable, acceptable mass, p50 and p95 latency at our real context
size, image support and cost per 100 decisions (section 8.3). The three candidates we could call ran
on the same corpus, labels and transport.

| | mammoth-1 | elephant-1 | liquid-d1.1 |
|---|---:|---:|---:|
| Top-1 acceptable, 40 paired frames | **40/40** | 30/40 | 21/40 |
| Against mammoth-1 on the same frames (wins / losses / ties) | | 0 / 10 / 30 | 0 / 19 / 21 |
| Acceptable mass, mean | 0.981 | 0.681 | 0.522 |
| Top-choice flips across identical repeats | 0/40 | 0/40 | 14/40 |
| Shuffled re-test, 45 frames | 45/45 | not run | not run |
| Live loop, 30 decisions | 30/30, 100% adherence | not run | not run |
| p50 / p95 latency at about 2,000 tokens | 250 / 266 ms | 172 / 203 ms | 203 / 250 ms |
| Median latency at about 11,000–13,000 tokens | 748 ms (Day 1); 875 ms at 13,249 tokens | 320 ms (Day 1) | 324 ms (Day 1) |
| Image input | yes; a 1280×720 frame adds 800–1,000 tokens and about 225 ms | yes | yes |
| Cost per 100 decisions | $0 in the trial week; unknown after | $0 in the trial week; unknown after | about $0.004 at 2,000 tokens; about $0.026 at 13,000 |

Sources: the first replay and its label review (`docs/probes/2026-10-27-fpskit-s1-bench.md`), the
shuffled re-test (`docs/probes/2026-10-27-fpskit-s1-bench-retest.md`), the live loop test
(`docs/probes/2026-10-27-fpskit-s1-live.md`), the load test (`docs/probes/2026-10-27-fpskit-s1-load.md`),
the Day 1 probe (`docs/probes/2026-10-26-day1.md`) and `docs/budget.md`. The 2,000-token latencies come
from the bench, which opened a new connection per call. The Day 1 figures at full size are 3 calls
each on a synthetic packet.

## Why mammoth-1

- It is the only model that chose acceptably on every frame, and neither alternative beat it on a
  single frame. Its answers did not change between identical requests, so the lead is not noise.
- It kept that record when the first corpus's regularities were removed (the shuffled re-test) and
  when the real loop executed its choices (the live test).
- Quality is the criterion with a clear winner. elephant-1 is faster and liquid-d1.1 is cheaper and
  priced, but both made many more mistakes on the simplest task we have.

## Open conditions

1. **Price after the trial.** Unknown. Baseten's pricing page, checked on 27 October, lists
   mammoth-1 without a price. The credits requested in #8 are unconfirmed: a Baseten representative
   said the final wave was being sent on 27 October and no notice has arrived, with no refusal.
   Modal's $100 did arrive. *This changes the choice if* the price, or a lack of credits, makes a
   10-minute run (400–1,000 calls) unaffordable. *Fallbacks:* the open weights (`baselabs/mammoth-1`)
   on Modal, which the Modal credits can fund (its cost, cold start and throughput are untested),
   then liquid-d1.1, which is priced but made 19 more mistakes in 40 and gives inconsistent answers.
2. **Latency at full context size.** Near the 12,000-token design budget Mammoth's median is about
   800 ms, and the loop rejects an action whose decision took 0.5 s or more. *Keeping Mammoth needs at
   least one of:* S1 payloads under about 7,000 tokens (less with a frame), freshness judged per action
   rather than by one age limit, or deciding during the hold. *Fallback:* elephant-1, about twice as
   fast at full size on Day 1's thin sample, with 10 more mistakes in 40.
3. **Rate limit.** Baseten rate-limited the load test after 23 calls, about 180,000 input tokens in
   46 s. The limit's kind and size are unknown. Real-time play needs 0.7–1.6 decisions a second for
   10 minutes, so confirm the limit with Baseten, or use a dedicated deployment, before the first
   10-minute run.

## What the evidence does not cover

- One game and one simple bookkeeping task. Nothing here measures escalation judgment, bug noticing,
  or whether a frame improves decisions; only the frame's token and latency cost was measured.
- Jev, the build bible's bench reference, was not run, and neither were Liquid's d1 and d1-ultra.
- The labels were reviewed at the level of the rule, after the reviewer had seen the results, with no
  second labeller.
- Only Mammoth was load-tested. The full-size comparison with the other two rests on Day 1's 3 calls
  each.

## Revisit when

#100 is the scheduled revisit, on Day 5 after the Day 4 ten-minute run. Earlier if:

- Baseten publishes a price, or its credits show up.
- The production payload size is known, once a facts builder and directive history exist.
- A real game frame can be captured, to measure its upload size and latency.
