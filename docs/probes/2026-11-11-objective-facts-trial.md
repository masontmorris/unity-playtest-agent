# Live trial: does stating measured objective progress to S1 help it complete the test plan? (11 November 2026)

Written before any session. Results are added below the plan when the trial has run.

## Why

[#268](https://github.com/masontmorris/unity-playtest-agent/pull/268) added a measured objective tracker. With `objective_facts` on, S1 sees one fact listing every objective item with its measured progress and status, such as `press look_left: 16 of 4 presses, met`. With it off, S1's payload is byte-identical to before. Either way, each session's run record scores the same items after the session.

In the [2k adapter trial](2026-11-10-modal-d1-lora-trial.md), the adapter followed the objective's test plan but over-pressed some steps and skipped others. Scored after the fact by the new tracker, its three sessions met 5, 3 and 3 of the 7 plan items:

| Step | Presses (sessions 1, 2, 3) |
|---|---|
| `look_right` | 50, 65, 75 |
| `look_left` | 65, 46, 47 |
| `look_up` | 9, 3, 1 |
| `look_down` | 9, unknown, unknown |
| `aim_toggle` | 36, 40, 32 |
| `jump` | never counted (unknown) |
| pickup | never met |

These three are context, not an arm, because they ran before this plan and on earlier code.

## Arms

| Arm | `objective_facts` | Everything else |
|---|---|---|
| `off` | false | identical |
| `on` | true | identical |

- **S1:** both arms use the `modal-d1-2b` S1 arm pointed at `d1-2b-s1-lora`, which is d1-2B `652f920e` with decision-lab's 2k adapter `8725f7a2` merged in. The endpoint is unchanged since the 2k adapter trial. `s1_wait_result` is `unmeasured`.
- **Game and settings:** FPSKit at `bridge-v0`, with the same frozen hunt inputs, objective, settings and approval as the 2k adapter trial. That is 600 s of play and at most 12 Meta Standard frontier calls.
- **Session memory:** each arm's memory starts empty.
- **Code:** branch `trial/objective-facts`, which merges `main` at `7700bda` (#268) into `feat/modal-d1-lora-arm` (#269 on #265).

## Order

Six sessions, one at a time: `on` 1, `off` 1, `off` 2, `on` 2, `on` 3, `off` 3. Each holds the machine's GPU lease while it plays.

## Metrics

- **Primary:** each session's plan items met, out of 7, from the `objectives` summary in its `run.json`. Both arms compute it the same way.
- **Secondary, reported but not used for a win:**
  - over-pressing: the largest presses-to-asked ratio on any one step;
  - steps left `unknown`;
  - `wait` share;
  - the longest run of one identical choice;
  - distinct choices;
  - candidate findings and their replays.

  Movement alone is never counted as progress.
- **Attrition:** a session killed by tooling before S1's first decision is set aside and played again. Any other session without `run.json` is `unknown`.

## Reading

- **The facts help complete the plan** if every `on` session meets more plan items than every `off` session (1 in 20 by chance). A tie between arms breaks this.
- **They do not** if the arms' ranges overlap and the `on` median is not higher.
- Anything else is `inconclusive`. No claim about finding bugs is made from six sessions.

## Cost

- **Meta Standard:** at most $0.57 a session. The adapter made no frontier calls in the 2k trial, so this is expected to be near $0.
- **Modal:** the `d1-2b-s1-lora` endpoint runs while sessions play and for 300 s after, about $1 an hour, so about $2.50.

## Data

Every session's frames, requests and traces are Unity-derived. They stay in this repository's ignored `runs/` and do not go to decision-lab. Results are reported only, and are not used to train, tune or choose adapters.

## Results

Run on 11 November 2026, 00:06 to 01:50, in the planned order, from branch `trial/objective-facts` at `f28e97f`. All six sessions recorded `run.json`, with no attrition. Every S1 call was answered by `d1-2b@652f920e+lora-8725f7a2-66b8f257`. The 3js builds paused their GPU work for the window.

| Session | Plan items met (of 7) | Unknown | Worst over-press | Decisions | `wait` | Longest run of one choice | Distinct choices |
|---|---|---|---|---|---|---|---|
| `on` 1 | 3 | 4 | 17.0× | 58 | 0% | 2 | 6 |
| `off` 1 | 3 | 4 | 22.5× | 71 | 0% | 3 | 9 |
| `off` 2 | 4 | 3 | 19.2× | 74 | 0% | 3 | 11 |
| `on` 2 | 5 | 2 | 15.0× | 65 | 0% | 3 | 11 |
| `on` 3 | 6 | 1 | 14.0× | 54 | 0% | 2 | 7 |
| `off` 3 | 5 | 2 | 12.0× | 97 | 0% | 2 | 10 |

**Predeclared reading: inconclusive.** `on` met 3, 5 and 6, with a median of 5. `off` met 3, 4 and 5, with a median of 4. The ranges overlap, because `on` 1 tied `off` 1 at 3, so the facts did not "help" by complete separation. The `on` median is higher, so "do not" does not apply either.

What the sessions show beyond the reading:

- **Both arms improved session by session**: `on` from 3 to 5 to 6, and `off` from 3 to 4 to 5. Each arm keeps its own session memory across its three sessions, so this looks like memory, in both arms, more than the fact. It also means a session's order within its arm is confounded with the arm. A next trial should reset memory each session, or include memory in the design.
- **The steps that stay open are the same in both arms.** `jump` was met once, in `on` 3, and the pickup step once, in `off` 3. Neither was met in any other session. The look and aim steps were over-pressed in every session, from 12 to 22.5 times the 4 asked. The fact made over-pressing only somewhat less: the worst step averaged 15.3× with it on and 17.9× with it off.
- **The fact has a known defect for this use** ([#270](https://github.com/masontmorris/unity-playtest-agent/issues/270)). A step with no press sent reads `unknown`, not `0 of 4`, so S1 could not see which steps it had never tried. In `on` 1, `look_up` and `jump` were never sent and both read `unknown`.
- **The adapter may not read objective facts at all.** Its synthetic training frames contained none. Whether a model can use the fact, and whether the fact is worded usably, are not separable here.
- No frontier calls ($0.00 Meta). No candidate findings in any session.

## Cost

- **Meta Standard:** $0.00 in both arms.
- **Modal:** the `d1-2b-s1-lora` endpoint billed $1.12 through the 00:00 hour, with the last hour not yet reported when this was written.

## Rerun with the counting fix and fresh memory (planned 11 November 2026, before any rerun session)

The first run was inconclusive, and it had two problems this rerun removes.

- **Session memory:** each arm carried its memory across its three sessions, and both arms improved session by session. In the rerun, every session runs in its own hunt folder with empty memory. Order within an arm no longer adds what earlier sessions learned.
- **The fact's counting:** before [#271](https://github.com/masontmorris/unity-playtest-agent/pull/271), a step never pressed and a step pressed but not scorable both read `unknown`. In the rerun, the first reads `0 of N presses, open`, and the second reads `K sent, none measurable, unknown`.

Everything else is as above: the 2k adapter on `d1-2b-s1-lora`, `s1_wait_result: unmeasured`, the same frozen inputs, objective, settings and approval, and the only difference between arms is `objective_facts`.

- **Code:** `trial/objective-facts` with #271 merged in, at the commit that adds this section.
- **Order:** `on` 1, `off` 1, `off` 2, `on` 2, `on` 3, `off` 3, each in its own folder (`runs/hunts-objective-facts-r2/<arm>-<n>/`).
- **Primary measure, readings and secondary measures:** the same as the first run. The facts **help** if every `on` session meets more plan items than every `off` session. They **do not** if the ranges overlap and the `on` median is not higher. Anything else is `inconclusive`.
- **Pooled reading, secondary:** a one-sided exact Mann-Whitney test, `on` higher, over the rerun's six sessions only. The first run's sessions are not pooled with them, because their memory and counting differ.

## Rerun results

Run on 11 November 2026, 08:11 to 09:53, in the planned order, from `trial/objective-facts` at `c48693b`. Each session ran in its own folder with empty memory. All six recorded `run.json`, with no attrition. A first launch at 08:10 failed before any session started: a path bug in the trial's runner script, fixed at once. The lease was held for under a minute and no game ran.

| Session (fresh memory) | Plan items met (of 7) | Worst over-press | `jump`: scored / sent | Pickup: scored / sent |
|---|---|---|---|---|
| `on`-1 | 5 | 17.8× | – / 5 | – / 5 |
| `off`-1 | 4 | 16.5× | – / 8 | – / 19 |
| `off`-2 | 3 | 16.0× | – / 12 | – / 34 |
| `on`-2 | 3 | 11.8× | – / 6 | – / 12 |
| `on`-3 | 4 | 12.0× | – / 10 | – / 21 |
| `off`-3 | 4 | 16.0× | – / 8 | – / 23 |

**Predeclared reading: the facts do not help.** `on` met 5, 3 and 4 (median 4), and `off` met 4, 3 and 4 (median 4). The ranges overlap and the `on` median is not higher. The pooled secondary test agrees: U = 5.5 of 9 for `on` higher, far from significant.

What the rerun shows beyond the reading:

- **With memory reset, the session-by-session climb of the first run is gone.** Scores sit at 3 to 5 in both arms. That climb was memory.
- **The facts lower over-pressing somewhat.** The worst step averaged 13.9× the 4 asked with facts on, against 16.2× with them off. This is secondary and from three sessions per arm.
- **Two steps were never met in any session.**
  - `jump`: S1 never chose `jump` or `test:jump`. Its presses came only inside `running_jump`, and all of those fell between observations (`unobserved_gap`), so none could be scored. In the first run, the one session that chose `test:jump` met the step.
  - Pickup: no `interact` was scored inside the pickup region.

  Both read `K sent, none measurable, unknown` with the counting fix, which is now true, where they used to be ambiguous. The fact told S1 these steps had not been scored, and S1 did not act on it.
- **The adapter does not act on objective facts.** Its synthetic training frames had none, so this is the expected result for this S1. The next test of the facts needs an S1 trained, or prompted, to use them.
- No frontier calls ($0.00 Meta). No candidate findings.

## Rerun cost

- **Meta Standard:** $0.00.
- **Modal:** the `d1-2b-s1-lora` endpoint, about 1¾ hours of use at about $1 an hour. The billing report had not caught up when this was written.
