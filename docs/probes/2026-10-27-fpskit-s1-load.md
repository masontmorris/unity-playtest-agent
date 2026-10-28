# FPSKit S1: Mammoth latency at full context size

On 27 October 2026 (early 28 October UTC) `mammoth-1` answered 41 production payloads at five sizes,
sent on one persistent connection with the live loop's 1.2 s pause between calls. Its median latency
rose from 241 ms at about 2,200 input tokens to 875 ms at about 13,200, and to 1,106 ms with a game
frame on top. Every call at the two largest sizes took longer than the loop's 0.5 s freshness window.
Baseten rate-limited the run once, after 23 calls; the user chose to resume. The design was
predeclared in `docs/design/s1-bench.md`. This measures speed, not answer quality.

## Result

| Payload | Input tokens (median) | p50 | Slowest of 8 | Fastest | Calls at 0.5 s or more |
|---|---:|---:|---:|---:|---:|
| `base`: what the live loop sent | 2,208 | 241 ms | 659 ms | 179 ms | 2 of 8 |
| `base+frame`: plus one 1280×720 frame | 2,978 | 466 ms | 1,301 ms | 357 ms | 4 of 8 |
| `mid`: extended facts | 6,970 | 479 ms | 924 ms | 472 ms | 2 of 8 |
| `full`: extended facts | 13,249 | 875 ms | 1,108 ms | 862 ms | 8 of 8 |
| `full+frame` | 14,268 | 1,106 ms | 1,928 ms | 1,042 ms | 8 of 8 |

All 41 answers were valid choices from `decision-models/baseten-mammoth-27b-latest`. With 8 calls a
size, the p95 is the slowest call.

- **Latency grows with size.** Across the text-only calls, each extra 1,000 tokens added 50–60 ms
  (52 ms by least squares; 50 and 63 ms between the medians). That puts the 12,000-token design budget
  at roughly 800 ms. The median was just under 0.5 s at about 7,000 tokens (479 ms).
- **A frame costs about 800–1,000 tokens and about 225 ms at the median.** With a frame, half the
  smallest payloads already took 0.5 s or more.
- **The slow tail reaches every size.** Even at today's size, 2 of 8 calls took 0.5 s or more (658 and
  659 ms), which matches the live test's stale turns.
- **The first call of a session was slow.** The first run's warm-up took 1,618 ms. The continuation's
  warm-up, also on a new connection, took 244 ms, so the connection itself was not most of it.
- **Compared with Day 1:** the Day 1 probe measured 748 ms at 10,895 tokens on a synthetic packet. The
  least-squares line through this run's text-only calls predicts about 770 ms there, so the two agree.

## What it means for the loop

Near the 12,000-token design budget, Mammoth's median is about 800 ms and every measured call took
longer than the loop's 0.5 s window, so nearly every action would be rejected as stale. Keeping Mammoth
means at least one of: keeping S1's payload under
about 7,000 tokens (less with a frame), a freshness rule per action rather than one age limit, or
deciding during the 1.2 s hold instead of after it. These are options, not a choice; the decision
record (`docs/design/s1-model.md`) carries them as conditions.

## The rate limit

Baseten answered HTTP 429 to the 24th call, after 23 answered calls and 180,710 input tokens in about
46 s (18.8 s of calls and 27.6 s of pauses). The runner stopped with no retry, as predeclared. The
earlier replay sent 180 smaller Baseten calls back to back without a 429, and the continuation four
minutes later sent 19 calls without one, so the limit's kind and size are unknown; the error body was
not recorded. At 0.7–1.6 decisions a second, a 10-minute run needs a sustained rate this test cannot
vouch for.

## Deviations

- **The sizes overshot their targets.** Sizes were estimated at 0.3675 tokens a character, measured on
  the re-test. The added fact lines are dense with numbers and tokenized more heavily, so `mid`
  measured about 7,000 tokens (target 6,000), `full` about 13,200 (target 11,000) and `full+frame`
  about 14,300 (target 12,000), above the 12,000-token design budget but under Baseten's 16,384 limit.
  The table reports the measured sizes.
- **One interruption.** After the 429 the user was asked whether to resume after a pause, stop, or
  resume more slowly, and replied "just resume now". `load.py` is pinned by the plan, so a separate
  `playtest.bench.load_resume` made a continuation: the 23 answered rows copied byte for byte, a
  warm-up call on the new connection, and indexes 23–40 of the same schedule. The first directory is
  unchanged and a reconciliation record lists what was copied. **43 requests were sent and 42
  answered**, against 41 predeclared: the extra two are the retried call and the second warm-up.

## Limits

- 8 calls a size, one machine, one network, one hour of one night. Baseten's load varies.
- The added facts are a stand-in for what a long run accumulates: real fact lines from the recordings
  and the live trace, mixed across passes. Their content is not realistic, so answers were checked
  for validity only.
- The frame is the Day 1 synthetic test pattern (PNG, 96,218 bytes). A real game frame may be larger
  to upload; that is untested.
- This is the trial's serverless endpoint. A paid tier or dedicated deployment may differ in latency
  and limits.

## Cost and accounting

43 requests, 42 answered and 1 rate-limited, `mammoth-1` only. Baseten returns no cost field, so the
billed cost is unknown; the free week was advertised to about 28 October. The reserved bound was
$0.005 a request, $0.215 for 43, which assumes at most $0.40 per million input tokens and is not a
Baseten price or a bill. The balance was not verified. The API key appears in none of the retained
files.

## Inputs and provenance

- Corpus `runs/bench/issue24-fpskit-04/frames.json` (`b84524b5…`); added facts from that corpus,
  `runs/bench/issue24-fpskit-03/frames.json` and `runs/bench/issue24-live-01/trace.json` (217
  distinct lines); frame SHA-256 `de18fdfa9e23dc214b50362e504f58393605096d3787247be7e9d6c1596f5537`.
- Approvals: first `7d7416252471ba68083fae899590f94b019201f7f7ff84610e607683995d21bd`, continuation
  `f2e5f7b7983019cfc09a87a3c924c700a9955a3d52fddfec4cee128dae82e739`.
- Plan `c6550b5ad0d8f3002ea5e3361631710a9dd88da9ceb97caeab23e586d390b155`; reconciliation
  `f1ebc88a8d43fd4797232ff382e1c1b15d84c6dc8824019ca3ac1ea13362e208`; report
  `282b27d0deaaa5e2d7f0077d4d207791d42d0a661164c2122db5a4709afe40a5`.
- Source: `load.py` `9819bdf895973e2cff11bb4c25ef90d743db7d76f8bcebc663dd3c818c7a19e7`,
  `load_resume.py` `eddf33f90575abc4f93e1243b1857b2ac62e6c103d48daf91a547c6f68cc57d1`, with `replay.py`
  `95f34cba…` and `live.py` `f45c3a68…` unchanged.

The plan, ledgers and report stay local under `runs/bench/issue25-load-01/` and
`runs/bench/issue25-load-02/`.
