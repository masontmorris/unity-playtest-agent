# d1-2B live trial: does decision-lab's 2k adapter stop the `wait` lock? (10 November 2026)

Written before any session. Results are added below the plan when the trial has run.

## Why

In [the wait-result trial](2026-11-10-modal-d1-wait-result-trial.md), writing a wait's `recent` result as `unmeasured` made locks rarer but did not stop them. Treatment session 4 chose `wait` 263 times in a row. decision-lab's synthetic experiments 2h and 2j used generic, engine-agnostic frames and no Unity data. They found that d1-2B answers `wait` whenever the facts leave the next step open, at 97% with 60 choices. Experiment 2k trained a rank-8 LoRA on rule-labelled synthetic frames. In those frames, `wait` is wrong when nothing is running or pending and right when something is. On held-out synthetic frames it took open-frame `wait` from 97% to 0% at 60 choices and kept `wait` at 100% where something was pending. It also picked "read" actions far more often than menus offer them. That raises the risk of a new lock on one action.

## Arms

| Arm | S1 weights | Endpoint | Everything else |
|---|---|---|---|
| `base` | d1-2B `652f920e` | `d1-2b-s1` (unchanged since the first trial) | identical |
| `lora` | d1-2B `652f920e` with 2k's adapter (`8725f7a253484f7e9bd9d10bf2905f10`) merged in | `d1-2b-s1-lora` | identical |

- **Setup:** both arms use the `modal-d1-2b` S1 arm on one Modal A10 each, with d1's fused attention and `s1_wait_result: unmeasured`. The `lora` endpoint names its adapter in each reply's `model`, so every call record shows which weights answered.
- **Merge check:** before this plan, the `lora` endpoint answered 178 of 180 synthetic 2k check frames as 2k's report did, including all 60 pending frames. The 2 misses were 12-choice open frames. The serving kernels differ slightly from the training kernels.
- **Game and settings:** FPSKit at `bridge-v0`, with the same frozen hunt inputs, settings and approval as the first trial. That is 600 s of play and at most 12 Meta Standard (`muse-spark-1.3`) frontier calls per session. Each arm's session memory starts empty.

## Order

Six sessions, one at a time: `lora` 1, `base` 1, `base` 2, `lora` 2, `lora` 3, `base` 3. Each holds the machine's GPU lease while it plays.

## Metrics

- **Primary:** each session's share of S1 decisions choosing `wait`.
- **Lock check, primary for the new risk:** each session's longest run of one identical choice of any id, and the share of its most-chosen id.
- **Secondary, reported but not used for a win:** the longest `wait` run, distinct choices, candidate findings and their replays, and the hunt's scores. Movement alone is never counted as progress.
- **Attrition:** as before. A session killed by tooling before S1's first decision is set aside and played again. Any other session without `run.json` is `unknown`.

## Reading

- **The adapter lowers waiting** if every `lora` session's `wait` share is below every `base` session's (1 in 20 by chance).
  - The same comparison is also made against all nine unmodified-d1 sessions with `unmeasured`: the six earlier treatment sessions plus the three `base` sessions. It counts if a one-sided exact Mann-Whitney test gives U ≤ 4 (p ≤ 0.05). This is secondary, because those six ran earlier in the day.
- **It does not** if the arms' ranges overlap and the `lora` median is not lower.
- **It swaps one lock for another** if any `lora` session has a run of at least 50 identical non-wait choices, or one non-wait id takes more than half its decisions. This is reported whatever the primary reading is.
- Anything else is `inconclusive`. No claim about finding bugs is made from six sessions.

## Cost

- **Meta Standard:** at most $0.57 a session and $3.40 for six. The first twelve sessions averaged $0.24.
- **Modal:** each endpoint runs while its sessions play and for 300 s after. That is about three hours of A10 in all, about $3 to $4.

## Data

Every session's frames, requests and traces are Unity-derived. They stay in this repository's ignored `runs/` and do not go to decision-lab. Unity's Terms of Service §17.2(dd) bar using Unity-derived data for machine-learning training, validation or development without authorization. The user decided on 10 November to run this test. To keep it a test of a finished adapter, its results are reported only, and not used to train, tune or choose between adapters.
