# S1 model decision

Issue #25 chose `mammoth-1` provisionally on 27 October 2026. **Status: changed on 29 October (#100).** S1's
default is now Liquid's `d1-ultra`, Mason Morris's choice on the evidence below. `mammoth-1` stays one flag
away (`--s1-arm`, `-S1Arm`). The 27 October decision and its evidence follow, unchanged, as the record it
superseded.

## Revisit, 29 October (#100): `liquid-d1-ultra`

S1 is `liquid-d1-ultra`: model `d1-ultra` on Liquid's decisions endpoint, in the same System One request
shape as every arm (#138).

| | mammoth-1 | liquid-d1-ultra |
|---|---:|---:|
| **First bench, 40 frames** | | |
| Top-1 acceptable | 40/40 | **40/40** |
| Top-choice flips across identical repeats | 0/40 | 0/40 |
| Acceptable mass, mean | 0.981 | 0.893 |
| p50 / p95 latency at about 2,000 tokens | 250 / 266 ms | 203 / 250 ms |
| **Shuffled re-test, 45 frames** | | |
| Top-1 acceptable | 45/45 | **45/45**: checks 40/40, terminal 5/5 |
| Acceptable mass, mean / minimum | 0.986 / 0.961 | 0.888 / 0.739 |
| Repeated requests | identical 5/5 | same choice 5/5; probabilities within 0.04 |
| Median / p95 / slowest latency | 266 / 782 / 1,188 ms | 203 / 250 / 266 ms |
| **Live 10-minute FPSKit run, about 5,000 tokens, frontier on** | | |
| S1 call p50 / p90 | 594 / 1,375 ms | **297 / 391 ms** |
| Turns rejected as stale | 31 of 105 | **0 of 90** |
| Action attempts rejected | 34 of 78 | **0 of 64** |
| Adherence; invalid answers | 1.0; 0 | 1.0; 0 |
| **Cost** | | |
| Cost per 100 decisions | unknown; the price is unpublished | about $0.008 at 2,000 tokens; about $0.022 at 5,000 |

Sources:
- `docs/probes/2026-10-29-fpskit-s1-bench-d1-ultra.md`: both benches. d1-ultra ran over the frozen Day 3
  corpora, whose payloads the current context builder reproduced byte for byte, so it answered the same
  requests mammoth-1 answered.
- `docs/probes/2026-10-29-fpskit-s1-d1-ultra.md`: the live run.
- `docs/probes/2026-10-29-fpskit-directive-windows.md`: the mammoth-1 live run it is set against.

**The three open conditions from 27 October:**
1. **Price.** d1-ultra costs $0.042 per million input tokens and nothing for output, which Liquid's own
   per-call `usage.cost` confirms. A 10-minute run costs cents. mammoth-1's post-trial price is still
   unpublished.
2. **Latency at the real context size.** On 29 October mammoth-1's p90 call took 0.9–1.4 s at 5,000–6,000
   tokens, so even the 0.8 s age limit (#132) rejected a third of its actions (#136). d1-ultra's p90 was
   391 ms, and no action went stale.
3. **Rate limit.** Baseten's limit is 100,000 tokens a minute. Liquid's is unpublished and unverified, but
   its 241 calls on 29 October met no rate limiting. The runner keeps its client-side pacing at 85,000
   tokens a minute.

**Caveats:**
- d1-ultra is less certain. Its acceptable mass averages 0.89 against 0.98, its lowest acceptable pick
  had 0.74, and identical requests return slightly different probabilities.
- Each live run is one session, unpaired, an hour apart.
- The benches test a checklist task in one game. They do not measure escalation judgment or bug noticing.
- Liquid's data-use terms are unverified. The S1 context, game-state text with no image, goes to Liquid.
- Liquid's tokenizer counted 20% more tokens than Baseten's on Day 1. d1-ultra's context is listed at
  32,768 tokens, double Baseten's 16,384.

## 27 October decision: mammoth-1 (provisional, superseded)

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

## Optional live game view (#157)

`--s1-game-view` (or `run-session.ps1 -S1GameView`) sends one bridge offscreen JPEG with each S1
request when a fresh one is available. It uses the existing `Temp/playtest-bridge-frames` window;
`-RecordFrames` is independent and is not required. The image and the game context are sent to the
selected provider. Frames remain local apart from that request; the context trace contains the view
statement, status and SHA-256, never frame bytes. The request hash covers the actual image request.
The plan and run summary record the mode; `s1_game_view.sent` and `omitted` count attempted provider
calls, including help follow-ups, and `stale` is the subset of omitted calls with an old frame. A
transport error counts as an attempted call because delivery may be unknown. If the runner fails before
it can assemble the summary, those counts are `null`, not assumed to be zero.

The bridge reports file write-completion time, not the actual capture time. The runner selects the
latest measured frame whose completion time is no later than the observation, from this session only.
Its statement gives the completion timestamp, dimensions and age relative to the observation, and
says the capture time is unknown. A frame over **1.0 s** old is omitted as too old; a missing,
unreadable, untrusted or unalignable frame is unknown and omitted. One second permits ten nominal
10 Hz frame intervals while bounding the discrepancy; it is a policy bound, not measured visual
freshness. No later frame or frame from a previous session fills a gap. The image does not alter the
choice set, selection or escalation rules.

Image support is restricted to combinations exercised in the [Day 1 probe](../probes/2026-10-26-day1.md):
`mammoth-1` and `elephant-1` use Baseten's `state.messages` image part; `liquid-d1.1` uses Liquid's
top-level `images` array. The default `liquid-d1-ultra` takes the same `images` array: in the
[30 October probe](../probes/2026-10-30-d1-ultra-images.md) a 640×480 JPEG added 450 input tokens, about
what the Day 1 image cost predicts for a third of the pixels. That shows the image was taken in, not that the
model made use of it. An arm with no recorded transport is refused at startup in this mode. Acceptance and
latency of actual bridge JPEGs in a live session still need checking. No call is made to discover capability.

The [load probe](../probes/2026-10-27-fpskit-s1-load.md) found that a 1280x720 frame added roughly
800–1,000 input tokens and 225 ms median on `mammoth-1`, with eight calls per size and a synthetic
PNG; real JPEG upload costs and latency are unmeasured. Day 1 estimated about 1,400 additional Liquid
tokens. Pacing uses reported usage when available; otherwise it estimates the text plus 1,000 Baseten
or 1,400 Liquid image tokens, rather than counting base64 characters. Its first-turn forecast reserves
those tokens, as does the forecast after an omitted image. More latency can cause more action freshness
rejections and greater cost; the approved budget must cover the selected mode and arm.

### S1 ownership and context audit

**Verdict: pass for the changed path under fake-bridge replay; real-session verification is not
applicable here.** The production context builder is used in the fake session, but no Unity or paid
provider run was made. The payload provenance is:

- `runner/game_view.py:64` compares the bridge JPEG's completion time with the observation wall time,
  then produces a statement, digest and separate in-memory bytes, or a missing/stale statement.
- `core/turn_loop.py:317` adds the statement, status and digest to the ordinary `build_context` payload;
  its `as_of.seq`, choices, pending help, live guidance and history follow their existing paths. The
  canonical payload hash is written in the context entry before the separate bytes go to S1.
- `runner/s1.py:157` checks the digest and presence, then `runner/s1.py:186` serializes that same payload
  with the provider's image transport. The trace retains the context and request hashes, not the image.
- `core/turn_loop.py:342` still parses S1's selected id against the observation-bound choice set;
  `core/turn_loop.py:374` and `:391` execute the chosen primary or pending selection. The view's stale
  branch only omits evidence. It does not change choices, help routing, pending guidance or defaults.

The focused fake-bridge tests in `tests/test_runner_session.py:1012` and `:1069` replay the trace and
check adherence with sent, stale and missing views, including an unoffered answer that sends no input.
They reconstruct the provider payload from the trace and check its digest. They do not prove real
capture alignment or provider acceptance. The first live session must check bridge capture/self-test,
JPEG acceptance, observed age and dimensions, token usage, call latency, freshness rejections and trace
replay; the JPEG completion time is only an approximate proxy for capture time.
