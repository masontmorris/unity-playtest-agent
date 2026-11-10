# d1-2B live trial: does writing a wait's `recent` result as `unmeasured` break the wait lock? (10 November 2026)

Written before any session. Results are added below the plan when the trial has run.

## Why

d1-2B as S1 falls into one repeated choice per session. In the 9 November local hunts it chose `wait` in 56–93% of turns in most sessions. The compact menu's `recent` writes a wait's row as `{"id": "wait", "result": "wait", "s": null}`, although nothing measured it. decision-lab's synthetic experiments 1j and 1k (synthetic frames only, no Unity data) found that one or more `recent` rows whose result is the word `wait` pull d1-2B's answer to `wait` against the stated state, and that the same rows with result `unmeasured`, the harness's own word for an unmeasured result, do not.

## Arms

| Arm | `s1_wait_result` | Everything else |
|---|---|---|
| `control` | `wait` (current behaviour) | identical |
| `treatment` | `unmeasured` | identical |

Both arms use the `modal-d1-2b` S1 arm: d1-2B at revision `652f920e`, served by `scripts/modal/d1_2b.py` on one Modal A10 with d1's fused attention. They play FPSKit at `bridge-v0` with the hunt settings of `runs/hunts-d1/fpskit-d1-2b.json`: 600 s of play, at most 12 frontier calls per session on Meta Standard (`muse-spark-1.3`), compact menu, game view.

Both arms start from the same frozen hunt inputs, copied from the 9 November `fpskit-d1-2b-02` hunt: its guard table, its three predictions, the session expectations and the objective. No guards session and no predict call runs. Each arm keeps its own session memory, which starts empty.

## Order

Six sessions, one at a time, in the order control 1, treatment 1, treatment 2, control 2, control 3, treatment 3. Each session holds the machine's GPU lease while it plays.

## Metrics

- **Primary:** the share of each session's S1 decisions whose chosen id is `wait`, from the session's trace. Also reported per session: the longest run of consecutive `wait` choices.
- **Secondary, reported but not used for a win:** candidate findings and their replay results, the hunt's play, rules and magnitude scores, and the number of distinct choices tried. Movement alone is never counted as progress.
- **Attrition:** a session without `run.json` is `unknown` for every metric and is shown as such.

## Reading

- **The change breaks the lock** if every treatment session's `wait` share is below every control session's. With three sessions per arm, complete separation happens by chance in 1 of 20 orderings.
- **It does not** if the two arms' ranges overlap and the treatment median is not lower.
- Anything else is `inconclusive`, and the per-session numbers are reported as they are.
- No claim about finding bugs is made from six sessions.

## Cost

- **Meta Standard:** at most 12 calls per session at the measured worst case of $0.047, so at most $0.57 a session and $3.40 for six. Expected about $0.28 a session from the measured median.
- **Modal:** the A10 endpoint runs while sessions play and for 300 s after the last call. That is about two hours at about $1.32 an hour, so about $2.60.
- **Approval:** `runs/hunts-modal-d1/approval-modal-d1-2b.json`. The user approved live trials on 10 November with an uncapped Meta budget, used judiciously.

## Data

Every session's frames, requests and traces are Unity-derived and stay in this repository's ignored `runs/`. Unity's Terms of Service §17.2(dd) bar using them for machine-learning training, validation or development without authorization. This trial tests a harness setting inside the forked test projects. Its data is not used to train, validate or develop a model, and does not go to decision-lab.

## Results

Run on 10 November 2026, 05:07 to 09:48, in the planned order, from branch `trial/modal-d1-wait-result-as-run` at `6946678`. That branch was built on `5104cbe`. This PR's branch carries the same changes rebased onto `main` at `b57ec24`, beside the `d1-2b-local` arm main gained meanwhile. The `local-d1-2b` server and the Modal endpoint the trial used are unchanged. A seventh attempt, the first try at treatment session 1, was killed by the runner's two-hour job limit during setup, before S1's first decision. It is attrition from the trial's own tooling, set aside as `treatment/session-01.killed-0758`, and the session was played again. Each session ran 600 s of play on the Modal A10 with d1's fused attention.

| Session | Decisions | `wait` | Longest run of `wait` | Distinct choices | Candidate findings (replayed k/n) |
|---|---|---|---|---|---|
| control 1 | 276 | 189 (68%) | 180 | 12 | `f-38` look_down never changes pitch (0/5); `f-57` jump often has no effect on jump_count (3/5) |
| control 2 | 321 | 64 (20%) | 2 | 15 | `f-56` jump often does not increase jump_count (0/5) |
| control 3 | 214 | 143 (67%) | 104 | 11 | none |
| treatment 1 | 155 | 56 (36%) | 10 | 15 | none |
| treatment 2 | 131 | 2 (2%) | 1 | 14 | none |
| treatment 3 | 146 | 18 (12%) | 2 | 11 | none |

**Predeclared reading: inconclusive.** Every treatment session is below the control median (67%), and the treatment median is 12%. But the ranges overlap: control 2 (20%) waited less than treatment 1 (36%). So neither "breaks the lock" (complete separation) nor "does not" (overlap and no lower median) applies.

What the sessions show, beyond the predeclared reading:

- **No treatment session locked.** Two of the three control sessions held `wait` for 104 and 180 decisions in a row; the longest treatment run was 10. Control 2 did not lock either, so a control session can also stay out of the lock.
- **The treatment arm found nothing.** The control arm raised three candidates, and one, `f-57`, replayed 3 of 5. Fewer waits did not turn into findings in these six sessions. Movement is not progress, and nothing here shows the change helps a hunt find bugs.
- Treatment sessions made fewer decisions (131 to 155 against 214 to 321), as expected when more of them start inputs that take game time instead of waiting.

## Cost

- **Meta Standard:** $1.46 in all, $0.73 per arm.
- **Modal:** the `d1-2b-s1` endpoint billed $1.99 through the 08:00 hour. The last two hours were not yet reported when this was written.

## Extension: three more sessions per arm (planned 10 November 2026, before any extension session)

The first six sessions were inconclusive by the reading above. This extension adds three sessions per arm and reads all twelve together. It was planned after the first six results were seen. Those results are already known, so the combined reading is a planned second look and not a fresh test. Its error rate is somewhat higher than its stated α, and the extension's own three sessions per arm are also read alone.

- **Arms, settings and inputs:** unchanged. The same configs (`sessions` raised to 4, 5, then 6), the same frozen hunt inputs, the same Modal endpoint (`d1-2b-s1`, unchanged since the first six), and FPSKit at `bridge-v0`. Each arm's session memory carries on from its first three sessions, as it would have with `sessions: 6` from the start.
- **Code:** this PR's branch, `feat/modal-d1-wait-result`. Its S1 path and endpoint are the as-run code.
- **Order:** treatment 4, control 4, control 5, treatment 5, treatment 6, control 6. This mirrors the first order with the arms swapped.
- **Primary, combined:** the one-sided exact Mann-Whitney test on the twelve sessions' `wait` shares, with treatment lower as the alternative. **Breaks the lock** if U ≤ 7 (p ≤ 0.047). **Does not** if the treatment median is not lower. Otherwise **inconclusive**.
- **Extension alone:** the first plan's rule applied to sessions 4 to 6. Complete separation means it breaks the lock; overlap with a treatment median that is not lower means it does not; anything else is inconclusive.
- **Secondary:** sessions with a run of at least 50 consecutive `wait` choices, per arm, out of six. Candidate findings and replays are reported, and no claim about finding bugs is made.
- **Attrition:** as before. A session killed by tooling before S1's first decision is set aside and played again. Any other session without `run.json` is `unknown`.
- **Cost:** the same per-session envelope. Meta Standard costs at most $0.57 a session and $3.40 for six, with about $1.50 expected from the first six. The Modal A10 costs about $1.32 an hour, about $2.60 over two hours. The approval file still applies; its estimates are valid until 11 November, 21:44 UTC.
- **Data:** as before. Everything stays in this repository's ignored `runs/` and nothing goes to decision-lab.

## Extension results

The extension ran on 10 November 2026, 11:21 to 13:04, in the planned order, from this branch at `8fd999c`. All six sessions recorded `run.json`, with no attrition. The 3js builds paused their GPU work for the window.

| Session | Decisions | `wait` | Longest run of `wait` | Distinct choices | Candidate findings |
|---|---|---|---|---|---|
| treatment 4 | 271 | 263 (97%) | 263 | 7 | none |
| control 4 | 228 | 106 (46%) | 101 | 11 | none |
| control 5 | 279 | 112 (40%) | 93 | 14 | none |
| treatment 5 | 276 | 81 (29%) | 24 | 11 | none |
| treatment 6 | 197 | 53 (27%) | 1 | 14 | none |
| control 6 | 195 | 153 (78%) | 135 | 10 | none |

**Combined reading, twelve sessions: inconclusive.**

| Arm | `wait` shares | Median |
|---|---|---|
| Control | 20, 40, 46, 67, 68, 78% | 57% |
| Treatment | 2, 12, 27, 29, 36, 97% | 28% |

The one-sided exact Mann-Whitney test gives U = 9 (p = 0.090), above the U ≤ 7 bar. Because the treatment median is lower, "does not" doesn't apply either.

**Extension alone: inconclusive.** Treatment 4 (97%) is above every control session, so the ranges overlap. The treatment median (29%) is below the control median (46%).

**Secondary, runs of at least 50 `wait` choices:** 5 of 6 control sessions against 1 of 6 treatment sessions. This was not the primary metric, so it is reported and not claimed.

What the extension shows:

- **A treatment session can lock.** Treatment 4 chose `wait` on 263 decisions in a row, from turn 8 to the end. Its `recent` rows read `{"id": "wait", "result": "unmeasured", "s": null}` and no help was pending, so the setting had applied. The distribution was nearly flat over about 60 choices, with `wait` around 0.07 to 0.08.
- **Writing `unmeasured` makes locks rarer, not impossible.** In decision-lab's synthetic experiment 2h, which used generic frames and no Unity data, the result word stopped mattering at 12 and 60 choices. At 60 choices d1-2B answered `wait` on 97% of generic frames before any row repeated. The live menu has about 60 choices.
- **No extension session raised a candidate finding.** Across all twelve sessions, only control 1's `f-57` replayed (3 of 5).

## Extension cost

- **Meta Standard:** $1.39 for the six extension sessions. Over all twelve sessions the arms recorded $1.44 (control) and $1.41 (treatment).
- **Modal:** the `d1-2b-s1` endpoint billed $2.19 from 16:00 to 18:00 UTC, which covers the extension and was reported through the 18:00 hour.
