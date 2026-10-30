# d1-ultra image transport probe (30 October 2026)

Issue #157. This is a static, two-call transport and token-usage probe, not a
live game or visual-understanding test. It used `liquid-d1-ultra` (resolved
model `d1-ultra`) on Liquid's decisions endpoint with the runner's choice
request and top-level `images` array. Both calls used the same small decision
context; only the image was added to the second request. The runner's image
allowlist was modified only in memory for this probe. The checkout was at
`6000ed47ec8bc6f35ce5f7d92dec13e2cd18fff2`.

The image was a 640×480 JPEG captured for preflight, SHA-256
`fa7eba8083da269e3d7b3f91e40e425f8c8e357c491aba38e82160427a08c9b7`.
The frame and call records remain local. The Liquid balance was unverified,
per approval-plan.json; the user approved this probe with a four-call, $0.002
maximum. No balance check was made.

| Call | Request | HTTP status | Error | Input tokens | Reported cost | Parsed choice |
|---|---|---:|---|---:|---:|---|
| 1 | Text only | 200 | None | 257 | $0.000010794 | Yes |
| 2 | Same text + JPEG | 200 | None | 707 | $0.000029694 | Yes |

The reported total was **$0.000040488**; no retry was needed. Output tokens
were reported as zero on both calls. The JPEG call added **450 input tokens**.
This differs from the roughly 1,000–1,400-token increase required by this
probe's acceptance criterion and the roughly 1,400-token increase measured
for a 1280×720 synthetic PNG with `d1.1` in the
[Day 1 probe](2026-10-26-day1.md). The image size, format and model differ;
this pair does not isolate why the increment is smaller.

**Verdict: inconclusive.** Both requests succeeded and the image request used
more tokens, so the observed result meets neither the specified acceptance
threshold nor the no-increase ignored criterion; it was not rejected. The
result does not establish that the model used the image or demonstrate visual
understanding. The runner's game-view allowlist remains unchanged. A
separately approved check with a comparable image and a visual control would
help resolve the uncertainty.

## Orchestrator's reading (added after review)

The acceptance range in the probe's brief (about 1,000–1,400 added tokens) came from a 1280×720 image. This
probe sent a 640×480 JPEG, a third of the pixels. Scaling the Day 1 increment of about 1,400 tokens by pixel
count predicts about 470, and the probe measured 450. An image the provider ignored would add nothing. So the
`images` transport is taken as accepted for `d1-ultra`, on the same kind of evidence that verified `d1.1` on
Day 1. `liquid-d1-ultra` was added to the runner's image allowlist on that basis.

This shows the image was taken in, not that the model used it. Whether S1 acts on what it sees is for the live
game-view sessions to show.
