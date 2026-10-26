---
name: playtest-s1-ownership
description: Audit a unity-playtest-agent change, design, or trace for S1 ownership and context fidelity. Use when a diff touches decisions, adherence, pending help, monitors, provider adapters, turn records, context assembly, windowing or compaction, or any path that could select, substitute, suppress, escalate or hide information from a choice; this is a targeted audit, not a general code review.
---

# S1 ownership and context audit

Paths are relative to the target repository root, the checkout containing `src/playtest/`. Do not infer the root from where this skill is installed.

Implementation notes describe `879f9129`; verify component availability in the target checkout before applying them.

## Scope

Use on a diff, design or trace that can change what S1 sees, may choose, or happens to its answer: decisions, choices, pending help, monitors, providers, records, context assembly, windowing and compaction. Default is the actuator audit of `raw_choice -> effective_choice -> executed_action`, rejection, pending help and escalation routing; read `references/context-fidelity.md` when the payload or a shortened record is in scope. Not a general code review, statistics, or a ruling on game meaning. No runtime loop, context builder or trace writer exists, so those checks are design review unless a real trace exists.

## Binding rules

- S1 owns the actuator. Only the selected choice executes. Code may validate, bind, execute, reject and fail closed; it may not substitute, rank, suppress or reinterpret.
- An invalid answer rejects the turn, is recorded, and sends no input. It is never repaired with another action, a safe wait or a replacement answer. A transport retry is a separate call, not a repair of a received answer.
- S1 owns escalation. Monitors may compute facts with counts, thresholds and timers and log diagnostic margins; they may not use them to route, recommend, substitute or trigger help. A threshold that only computes or logs is not a violation; one that chooses help is.
- Code exposes facts and executable choices. A fact may carry measured counts and margins but no recommended action.
- Missing evidence is `unknown`/`null`, never zero, success or failure. Observations and hypotheses stay separate. The core is name-blind; game knowledge lives only in the manifest.
- Fail closed and release inputs on disconnect, timeout, reset, scene change or stop; never reconnect and replay.

## Procedure

1. Build a path table for every branch: invalid, stale, action/skill/wait, finish, escalate, delegate, pending wait/continue, no helper and adoption. Record condition, effective and executed choice, outcome, `policy_default`, file:line and authorising rule.
2. Check the actuator. `parse_decision()` must reject without consulting `Decision.distribution` (record-only); `ChoiceSet.built_from_seq` and `ActRequest.based_on_seq`/`max_age_s`/`depends_on` bind an action to its observation. A stale rejection is not a different choice. Reject argmax, fallback, confidence rules and code-selected "safe" actions.
3. Check pending help. `while_pending` applies only on real-time help turns; a missing or invalid secondary defaults to `wait` and records `policy_default`, while a valid primary survives. One helper, one adoption. Ordinary selections execute; only an explicit wait sends no input.
4. Check escalation routing. Search monitors, facts, adapters and loop branches for a score, threshold, stagnation check or timer that becomes a help request. A fact reporting a measured mismatch is allowed; a branch that escalates from it, or suppresses a valid selection to continue, is not.
5. Check context fidelity. Reconstruct the serialized payload, trace each fact and choice from observation through extraction, window and compaction, and distinguish the live directive from an expired compact-history entry. Do not infer absence from a shortened record or accept hardcoded reader names. Follow the reference.
6. Run:

   ```sh
   uv run pytest tests/test_decisions.py tests/test_adherence.py tests/test_name_blind.py
   ```

   These cover the parser and hand-built records, not a runtime path. Replay a real trace through `check_adherence()` and require zero violations; otherwise report live-trace verification as not applicable, never passed. Every new branch needs a negative test that fails on repair, substitution, pending suppression or created escalation.

## Outcome and stop

Write a note with the path table or payload map, file:line references, verdict (`pass`, `blocked`, `not auditable`) and residual risk. State whether runtime tracing and a production builder were applicable. If unsupported or unknown, name the missing evidence and smallest check.

Report `blocked` if the change only works when code chooses an action, including a safe wait; if a fact becomes escalation; if a valid pending selection is suppressed; or if the test cannot catch a repair. Report `not auditable` when there is no diff, design or trace.
