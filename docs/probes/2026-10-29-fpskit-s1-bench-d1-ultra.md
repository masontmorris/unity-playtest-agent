# FPSKit S1 bench: Liquid d1-ultra (2026-10-29)

Issue #100. Liquid's `d1-ultra` was replayed over both frozen Day 3 S1 corpora: the first 40-frame replay and
the 45-frame shuffled re-test. The labels were Day 3's, unchanged. It chose acceptably on every frame of both.
After this and the live trial (`2026-10-29-fpskit-s1-d1-ultra.md`), the owner made it S1's default
(`docs/design/s1-model.md`).

## Same requests as Day 3

The replay rebuilds each frame's S1 payload with the production context builder. It refuses to call if the
result differs from the recorded payload hash. All 85 frames reproduced byte for byte, so d1-ultra answered the
same request bytes mammoth-1 and d1.1 answered on Day 3; only the model name differed. Day 3's saved answers
are therefore the comparison, without re-calling those models. The two sets of calls are an hour and two days
apart, against hosted models whose revisions are not pinned.

## First bench, 40 frames

Same protocol and seed (24) as Day 3: 50 noise repeats on 10 frames, then the 40-frame sweep. Scored against
the reviewed labels.

| Model | Acceptable top choice | Adjacent repeat flips | Acceptable mass, mean | Median / p95 latency |
|---|---:|---:|---:|---:|
| mammoth-1 (Day 3) | 40/40 | 0/40 | 0.981 | 250 / 266 ms |
| **d1-ultra** | **40/40** | **0/40** | 0.893 | 203 / 250 ms |
| elephant-1 (Day 3) | 30/40 | 0/40 | 0.681 | 172 / 203 ms |
| d1.1 (Day 3) | 21/40 | 14/40 | 0.522 | 203 / 250 ms |

This corpus can be passed by a counting shortcut (the Day 3 re-test note), so 40/40 here is necessary, not
sufficient.

## Shuffled re-test, 45 frames

Same protocol and seed (2410) as Day 3's mammoth-1 re-test: one request per frame, plus a second request on
five frames drawn by seed. The re-test runner now takes `--arm`, with mammoth-1 still the default. It defeats
the shortcuts: "the id after the last completed one" scores 22/40 there.

| | mammoth-1 (Day 3) | d1-ultra |
|---|---:|---:|
| Acceptable top choice | 45/45 | **45/45**: checks 40/40, terminal 5/5 |
| Misses / unknowns | 0 / 0 | 0 / 0 |
| Acceptable mass, mean / minimum | 0.986 / 0.961 | 0.888 / 0.739 |
| Same request sent twice | identical distribution 5/5 | same choice 5/5; probabilities differ by at most 0.039 |
| Median / p95 / slowest latency | 266 / 782 / 1,188 ms | 203 / 250 / 266 ms |

d1-ultra chose `finish` on all five terminal frames and never repeated a completed control.

## Cost and calls

- **Calls:** 140, all HTTP 200 with an offered choice, none retried: 90 in the first bench and 50 in the
  re-test.
- **Cost:** Liquid reported $0.00679 and $0.00406, **$0.0109** in all.
- **Approval:** the owner directed the run at "a few cents" with the Liquid balance unverified. Envelopes:
  100 calls and $0.05, then 50 calls and $0.025.

## Limits

- One task, a control checklist, in one recording of one game, text only. Nothing here measures escalation
  judgment, bug noticing, or play in a live loop. The live trial adds freshness and adherence, not quality.
- d1-ultra is right but less sure: lower acceptable mass, and slightly varying probabilities on identical
  requests. Its top choice never varied.
- The labels are Day 3's assistant draft, reviewed at the level of the rule after the reviewer had seen
  results, with no second labeller.

## Records

All local under `runs/bench/`.

| File | SHA-256 |
|---|---|
| Corpus | `3bdd5ff6…` |
| Draft labels | `9a43e01c…` |
| Reviewed labels | `d838144e…` |
| Shuffled corpus | `b84524b5…` |
| Shuffled draft labels | `b067d24e…` |
| First-bench plan | `f47c5f40…` |
| First-bench report | `0cd5ef9a…` |
| Re-test plan | `8db68da1…` |
| Re-test report | `a11afe34…` |
