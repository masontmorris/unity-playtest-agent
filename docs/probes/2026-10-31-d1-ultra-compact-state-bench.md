# d1-ultra: compact S1 state, live paired bench (31 October 2026)

**Question.** Does sending S1's state compactly (`--s1-compact-state default` or `all`) change d1-ultra's
choices relative to the night configuration (object state, `dup-state-ref/1`), and what does each arm bill?

**Verdict, by the rule declared before any call: both compact arms change S1's choices.** The modal choice
agreed with the night arm on 28/40 contexts (`default`) and 29/40 (`all`). The "changes" threshold was
below 32/40. Billing fell as predicted: −18.2% and −29.0%, each within 1.3% of the offline prediction.
These frames have no labels of acceptable choices, so this measures change, not quality.

## Design (written to `runs/s1-opt-live/plan.json` before the first call)

- **Unit.** One frozen context from the d1-ultra night sessions 01–02: every fourth of the 157 primary
  calls, 40 in all, spanning session-01 turns 0–96 and session-02 turns 3–59. Each context is the exact
  payload plus the exact JPEG it was sent with, recovered from the session recording by SHA-256.
- **Question.** The primary question only, on a stepped clock, so no help follow-up is asked. The request
  is otherwise production: `SystemOneS1`, `liquid-d1-ultra`, game view on.
- **Arms.** `night` (object state, `dup-state-ref/1`), compact `default`, compact `all`. 5 repeats each,
  600 calls, in a seeded random order (seed 20261031).
- **Primary estimand.** Per arm, the share of contexts whose modal choice equals the night arm's.
- **Rule.** An arm keeps S1's choices at ≥36/40 with per-call agreement within 5 points of the night arm's
  own; it changes them below 32/40; otherwise inconclusive.

## Gates before the bench

- **Quick try, 3 night calls.** All returned 200. Each request hash equalled the night run's recorded hash,
  each billed token count equalled the night's (6,120, 24,745 and 21,365), and each choice matched the night's.
- **Gate, 3 calls on session-01/80, which the night billed at 24,745 tokens:**
  - compact `default`: 19,886 tokens (predicted 19,723).
  - compact `all`: 18,015 (predicted 17,852).
  - compact `all` + batched pending question: 19,919, so the second question costs 1,904.

  All three returned 200 and the night's choice.

## Results (600 calls, 600 OK)

| Arm | Modal agreement with night | Per-call agreement with the night modal | Leave-one-out self-agreement | Mean TV vs night (night split-half: 0.064) | Billed total | vs night | p50 / p90 latency |
|---|---:|---:|---:|---:|---:|---:|---:|
| night | 40/40 | 0.925 (in-sample) | 0.905 | — | 3,518,010 | — | 532 / 735 ms |
| compact `default` | **28/40** | 0.725 | 0.865 | 0.121 | 2,879,520 | **−18.2%** | 484 / 687 ms |
| compact `all` | **29/40** | 0.705 | 0.910 | 0.122 | 2,496,320 | **−29.0%** | 454 / 625 ms |

**What the flips look like:**
- **They are near-ties.** The night arm's median margin between its top two choices was 0.043 on the 11
  contexts `all` flipped, against 0.155 on the 29 it kept. In 10 of the 11, `all`'s choice was the night
  arm's second choice.
- **They are not one systematic effect.** The two compact arms agree with each other on only 28/40, the
  same rate at which each agrees with night. Small presentation changes reshuffle low-margin decisions.
- **Neither compact arm is noisier.** Its repeat self-agreement matches the night arm's.
- **Escalation moves, but not significantly.** Modal `escalate` contexts were 12 for night, 8 for
  `default` and 17 for `all`. Per context, the sign tests do not separate either compact arm from night
  (p = 0.18 and p = 0.61).
- **Finishing is rare in every arm.** `finish` was chosen in 1 night call, 4 `default` calls and 6 `all`
  calls, out of 200 each. That is too few to read, but a `finish` ends a run, so it is worth watching live.

## Spend

606 calls, $0.378 by Liquid's reported `usage.cost`: 6 gate calls $0.0046, and the bench $0.1478, $0.1209
and $0.1048 by arm. No call was left in flight and none exceeded its per-call bound. The approval in
`runs/s1-opt-live/approval.json` cites the user's instruction and records Liquid's balance as unverified.
The quick-try calls served as the balance check.

## What it does not show

- **Quality.** No acceptable-choice labels exist for these frames.
- **The cause.** The compact arms change both the format (no indentation, a header) and the structure
  (tables, `=`, `@`). This bench cannot say which part moves the near-ties.
- **Whether d1-ultra moves this much under any harmless change.** A key reorder, for example, was not run
  as a control.
- **One game, one night, 40 contexts.**

Artifacts are local under `runs/s1-opt-live/`: `plan.json`, `approval.json`, `results.jsonl`, `summary.json`,
`smoke.jsonl`, `gate.jsonl` and `calls/`. The script is `scripts/probes/probe_s1_compact_live.py`, and the
offline billing model is in `docs/design/s1-token-budget.md`.
