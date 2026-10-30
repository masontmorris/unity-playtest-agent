# Frontier escalation in live runner sessions

`playtest-run` calls the frontier only when S1 chooses `escalate`. Enable the path with `--frontier`
and set `--max-frontier-calls` to a positive approved cap. The same approval file must include the
frontier model's arm and enough overall call and cost capacity for both providers. `META_API_KEY` is
required only when this flag is enabled. Without the flag the runner has no frontier helper.

The runner's `FrontierHelp` is a `HelpPort`. It reads the exact `HelpRequest.context` S1 saw, including
reader values and the measured fact lines, and uses the request's turn and observation sequence as
provenance. It captures the existing short clip and sends one schema-constrained frontier request.
The call is reserved in the shared paid-call ledger before the provider request and settled with its
reported cost after decode. An invalid answer is recorded as such; no hypothesis or directive is made
up. S1's selected `while_pending` choice remains owned by the core loop.

Accepted hypotheses and directives are written as `investigation` trace entries. Directive scoring
runs in a runner facts source against the manifest's reader aliases using the shared `roles.checks`
evaluator. The directive goal, setpoint, deadline and current status are shown to S1 as measured facts.
The tracker records `met`, `not_met`, `expired`, `invalidated` or `unknown` with a `trace_seq` reference
to the observation evidence. Missing readings stay unknown; a deadline with no scoreable reading is
recorded as expired. A session ending before an active directive can be scored records unknown.

## When a directive's test never ran (#133)

**The deadline.** The frontier gives a relative deadline. The tracker counts it from the observation that
first states the directive to S1, which is when S1 received it, as `to_directive` documents. It used to count
from the escalation's own observation. The answer reaches S1 only after the frontier call and the next turn's
pacing, so on the #132 validation run, 11 of 14 deadlines had already passed when S1 first saw its directive.
The relative length is kept, and only its start moves. When either game time is unknown, the deadline stays
as given.

**The window.** When a directive closes, its record carries a `window` (`DirectiveWindow`): what S1's inputs
did from that first statement up to the outcome.
- `turns`;
- inputs sent and completed;
- inputs rejected, by the bridge's reason;
- failed, cancelled, timed-out and unrecorded inputs;
- waits and escalations.

These are counts from the bridge's own terminal records, never a verdict.

**Untested outcomes.** A `not_met` whose window completed no input gets an `error_class`:
- `no_attempt` when no input was sent;
- `action_rejected` when every input that reached the bridge was rejected.

Any outcome whose window completed no input says so in its reason. A `met` reached without S1's input is
labelled the same way, because the setpoint already held.

**Where it shows up.**
- The fact line S1 reads states the window and the error class.
- The next frontier request's directive history carries both, because they are fields of the `Directive`
  contract.
- The trace's `outcome` entry records them.

Offline coverage uses a fake bridge, fake provider response, fake clip capture and scripted S1. It does
not make a live model call or launch a Unity Editor.

## S1 ownership and evidence audit

| Branch | Runtime behavior | Evidence |
|---|---|---|
| Invalid S1 answer | Rejects the turn without input; no helper starts. | `TurnLoop.run_turn`, `core/turn_loop.py:322`; parser and loop coverage in `tests/test_decisions.py` and `tests/test_turn_loop.py`. |
| Action or skill | Executes only the selected choice through the bound observation; a stale rejection sends no substitute. | `core/turn_loop.py:328`, `_send` at `:446`; adherence tests. |
| Finish | Ends normally, then stops and releases inputs. | finish choice branch after `:328`; `tests/test_runner_session.py`. |
| Escalate or delegate | Only the selected help choice calls `_help_request`; runner calls the frontier only for the attached `escalate` helper. | `core/turn_loop.py:336-337`; `runner/frontier_help.py:42`. |
| Pending help | Executes exactly S1's valid `while_pending` continuation; otherwise waits under the existing default. | `core/turn_loop.py:343`; `tests/test_turn_loop.py`. |
| Helper adoption | A completed helper result is adopted once at the next turn start and included in S1 guidance. | `core/turn_loop.py:255`, `_adopt_finished_help` at `:526`; end-to-end test in `tests/test_frontier_help.py`. |
| Directive scoring | Reads the current observation and shared comparator result to write a fact and trace outcome; it cannot call the help port or choose input. | `runner/directives.py:45-71`; all terminal outcomes covered in `tests/test_frontier_help.py`. |

The request uses the exact context S1 received and the explicit request turn and observation sequence. Investigation rows cite the corresponding observation's trace entry. The fake end-to-end trace passes the trace-entry validator, adherence checker and trace replay checks. No live trace was collected, as requested.
