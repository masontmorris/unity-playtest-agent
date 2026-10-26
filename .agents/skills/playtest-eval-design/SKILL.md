---
name: playtest-eval-design
description: Design or review a unity-playtest-agent comparison before any paid model sweep or improvement claim. Use for S1 prompts, contexts, choice sets or models, preflight efforts, frontier or directive formats, and any "better" claim; covers budget approval, estimand, frozen inputs, nondeterminism, pairing, scoring, attrition and uncertainty.
---

# Evaluation design

Paths are relative to the target repository root, the checkout containing `docs/budget.md` and `scripts/probes/`. Do not infer the root from where this skill is installed.

Implementation notes describe `879f9129`; verify component availability in the target checkout before applying them.

## Scope

This is experiment design, applied before calls and claims. Current probes check capability, latency and cost on small samples, not quality. No S1 bench, production `context()` or `playtest-bench` command exists. Do not present a probe as paired or claim a planned tool.

## Binding rules

- No paid call starts — pilot, noise, repeat, retry or arm — before approval of a stated call count and cost envelope and a balance check.
- Predeclare the estimand, outcome and scoring rule. No predeclared scoring means `unknown`; do not use a post-hoc proxy.
- Arms differ only in the declared variable. A prompt comparison varies the prompt; a non-prompt comparison does not. Never randomize whether S1 may escalate; only the format or context of an S1-requested escalation may be randomized.
- A missing game outcome is `unknown`, not failure or loss. Only a predeclared operational metric counts an API failure. Objective counters measure progress; movement is diagnostic.

## Procedure

1. **Budget first.** Estimate arms × units × repeats plus pilot, noise and retries; use current prices (`docs/budget.md`, provider). Get approval and check the balance before any paid call. Keep a ledger per call, make the sweep resumable, and stop at the envelope. Historical costs are not approval for a new project.
2. **Predeclare.** State the decision, unit (request body, frame, turn, run or project), population, arms, primary metric, direction, stopping rule, and that `inconclusive` is allowed. Declare an observable scoring rule or rubric. If judgment is needed, say who judges, what they see, and how disagreement and uncertainty are recorded. Human judgment is allowed; a deterministic scorer is not required.
3. **Freeze.** Record prompt hashes, model and resolved model, effort, schema, choice-set version, game commit, clock, reset, seeds and code. In a prompt comparison both prompts differ by design: record both and hold the rest constant. If prompts differ in a non-prompt comparison, stop. Labels stay in the repo with labeller, date and rationale; a human sample check is a heuristic, not a law.
4. **Assess noise.** Repeat the same body; report top-choice flips, the probability range and paired per-frame wins. Adequacy depends on variance, correlation and effect size, not a fixed frame count. Five repeats and a 10% human check are starting heuristics (bible §2, §10.4), not universal law; budget repeats. If noise exceeds the expected effect, add pairs or declare inconclusive.
5. **Pilot and match.** Use randomized order, the same seeds, resets and deadlines; hold prompts, schema and model except the variable. No unpaired 2-vs-3 A/B. Match clock and reset for game outcomes; report real-time and stepped separately. For preflight effort, fix stage 4 and vary stage 6.
6. **Attrition and scoring.** Record attempted, succeeded and billed per arm; never impute game success or failure. An API failure is a loss only under a predeclared operational metric; otherwise it is `unknown` or excluded with missingness shown. Count objective progress separately from movement. Score directives `met`, `not_met`, `expired`, `invalidated` or `unknown`; no setpoint is unscorable. Report k/n, latency, cost, attrition and uncertainty.

## Outcome and stop

The artifact is the design table, ledger, committed labels, and compact write-up. Check the ledger matches count and cost, arms are paired, noise is reported, objectives are separate from movement, inconclusive results are labelled, and no planned tool is claimed. If unsupported or unknown, name the missing evidence and the smallest budgeted test.

Stop without approval or a verified balance, or if the envelope would be exceeded; if outcome or scoring was not predeclared; if arms are unpaired; if a non-prompt comparison changes the prompt; or if models differ without being the variable. Stop if noise is unmeasured for a near-tie, or if the only claimed gain is movement. On billing or auth failure, stop that column rather than retrying or switching silently.
