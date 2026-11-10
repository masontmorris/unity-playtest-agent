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
