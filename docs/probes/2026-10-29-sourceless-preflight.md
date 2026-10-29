# Preflight without game source

Issue #121, for the thin-collector route (#120). The question: can preflight still select well when a customer doesn't upload source? Three arms differed only in the code evidence the packet carried:
- **S:** source excerpts plus IL write facts (today's packet).
- **I:** IL write facts only.
- **N:** neither.

**Result: inconclusive** under the predeclared rule, and under each judge alone. Readers and actions show no sign of needing source. Whether invariants get worse without source is unresolved; an exploratory count leans toward source. Full records stay local under `runs/eval/sourceless-121/`.

## Setup

- **Inputs:** the frozen #28 FPSKit preflight (`issue28-fpskit-01`: survey, discovery, the `record.json` calibration and `frame.jpg`) on the unseeded fork `25e43fb`.
  - Packets: S 888,355 characters (`4542fc70…`), I 790,960 (`b50e5afb…`), N 726,442 (`06be5e37…`).
  - Every arm used one system prompt that doesn't promise source (`21b3fbac…`) and one schema (`c157220b…`).
- **Code:** preflight at `a3bad76`, main just before #115. Current main rejects the #28 inputs, and a fresh capture is blocked by #116.
  - The runner is `scripts/probes/sourceless_preflight.py`. It swaps in the neutral prompt on the outgoing request after production's own request checks, so no production module changed.
- **Model:** `muse-spark-1.3` (Standard) at `minimal`. The stage runner's validation retries are the only retries.
- **Schedule:** 1 pilot slot and 6 main slots. Each slot ran all three arms in a seeded order (seed 121).
- **Deviations, all recorded before the run on #121 or in the run ledger:**
  - 6 slots instead of 10, for budget.
  - Code frozen at `a3bad76`.
  - After the first pilot chain: ASCII-only console output (the Windows console's code page ended the runner), and a per-arm `prompt_cache_key`. The key is routing only; the model's input is unchanged.

## Accounting

- **Calls:** 63 (9 pilot, 54 main). All 21 chains completed, with 0 retries, 0 failures and no stops. `muse-spark-1.3` resolved on every call.
- **Cost:** $3.496 estimated from usage at list price, against an $8 cap.
- **Token sizes and caching:** prompts were 190–235k tokens a call, not the 26k the budget assumed.
  - Cold first calls cost $0.24–0.30; cached calls $0.036–0.042.
  - Three calls missed the cache and paid full price, two of them before the cache key was added.

## Secondary metrics (code-scored, 6 pairs)

| | S | I | N |
|---|---|---|---|
| Chosen readers that calibration saw change | 48% | 55% | 51% |
| Calibrated effective controls used | 100% | 100% | 100% |
| Invariants per chain | 2.0 | 2.33 | 2.17 |
| Invariant citations naming a recorded writer of the field | 2 of 4 | 9 of 9 | 0 of 5 |
| Reader overlap across repeats within the arm (Jaccard) | 0.56 | 0.68 | 0.65 |
| Reader overlap with S | — | 0.67 | 0.55 |
| Gaps: unobservable / uncontrollable / unjudgeable | 4.7 / 4.7 / 3.5 | 4.0 / 4.7 / 3.0 | 3.8 / 4.0 / 3.2 |
| Estimated cost per chain | $0.124 | $0.112 | $0.139 |

**Paired differences (90% bootstrap intervals):**
- Live-reader share: I − S +0.07 [+0.03, +0.10]; N − S +0.03 [−0.10, +0.16].
- Invariants per chain: I − S +0.33 [−0.33, +1.00].

**Notes on the citation count.** Citations whose field has no IL facts are left out (8, 5 and 8). The I arm's 9 of 9 is partly circular, because that arm sees the facts the check uses.

**Reader choice.** Withholding source moved the reader choice no further than S moves from one repeat to the next.

## Primary metric: stage-6 entries judged correct, per chain

**No arm produced progress fields.** Every arm said no reader tracks score, pickups or level completion, so the primary counts invariants.

**Judges.** The owner tried the blind sheet and found code-level judging impractical: most cards came out "can't tell". Before any verdicts existed, two blind model judges replaced the owner:
- Claude Sonnet 5.5 at `effort: max`.
- GPT-6 Luna at `reasoningEffort: xhigh`.

Each saw only the 32 deduplicated, shuffled cards and the unseeded game source. An entry counts as correct only if both judges say so.

**Agreement.** The judges agreed exactly on 59% of cards (Cohen's kappa 0.34), and on correct-versus-not on 66%. Most splits were Sonnet's `correct` against Luna's `plausible`: Luna treats bounds a designer can change in the Inspector as not guaranteed.

| Verdicts | Mean correct, S / I / N | I − S | N − S | Margin (−25% of S) | Decision |
|---|---|---|---|---|---|
| Both judges (primary) | 0.83 / 0.67 / 0.67 | −0.17 [−0.67, +0.33] | −0.17 [−0.67, +0.33] | −0.21 | inconclusive |
| Sonnet 5.5 only | 1.17 / 1.17 / 1.17 | 0.00 [−1.00, +1.00] | 0.00 [−0.67, +0.67] | −0.29 | inconclusive |
| GPT-6 Luna only | 1.50 / 1.00 / 1.00 | −0.50 [−1.00, 0.00] | −0.50 [−1.17, +0.17] | −0.38 | inconclusive |

**Exploratory, not predeclared:**
- Share of entries both judges called correct: S 42% (5 of 12), I 29% (4 of 14), N 31% (4 of 13).
- Both judges called wrong: S 1, I 5, N 2.
- Only 1 of the 32 unique entries came from more than one arm.

Labels from both judges are in [`2026-10-29-sourceless-preflight-labels.csv`](2026-10-29-sourceless-preflight-labels.csv).

## Claim limits

**What the result covers:**
- FPSKit only, one frozen capture.
- Inputs captured in the Editor, not from a player.
- Pre-#115 code, Standard at `minimal`, 6 pairs.

It says nothing about S1 play or other games.

**Why the primary can't decide:** at this size it can't detect a 25% drop. A chain yields about 2 invariants, and about 1 of those is judged correct.

**Blinding and self-agreement:** entry content can hint at the arm, though labels were hidden. Self-agreement wasn't measured; inter-judge agreement stands in for it.

## What would settle it

The paired differences here have a spread of about 0.74 correct invariants. If there's no real difference, about 35 pairs would decide at this margin. If the true drop is near the margin, it would take far more.

With caching, a three-arm slot costs about $0.35 on Standard, and an S-versus-I slot about $0.24.

A more sensitive primary would help more than raw size. One option is to check each invariant against recorded play. It would have to be declared before a new run.
