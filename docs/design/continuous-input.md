# Continuous input (opt in)

Status: implemented for review; Python fake-peer validation only. No Unity run, provider call,
commit or push was performed. C# compilation, shared-fixture parity and live continuity remain unverified.

## Current model and alternatives

An executable choice sends one observation-bound ActRequest and waits for its terminal receipt.
The calibrated hold is typically 0.4 s. Reported live decision latency is 0.5–0.9 s and pacing can
add up to about 1.2 s, giving roughly 1.5–2 s turns in the supplied trial account (these ranges
are context, not measurements made by this implementation). This produces visible stops.

Options considered:

* Re-selecting the same action to renew a lease without releasing: best physical continuity,
  but the existing bridge explicitly releases/verifies/reapplies every superseding request.
  A true renewal needs more protocol, concurrency and receipt changes; deferred.
* Decide while an explicitly selected hold runs: chosen. Await the running acknowledgement,
  release the client request lock, then observe and decide while the safety lease runs.
  A subsequent S1 selection starts exactly that action, including when it names the same action.
  Supersession still briefly releases/reapplies. This removes the decision-sized idle gap,
  not every device-update gap. Only catalog keyboard actions qualify in this first version;
  skills, pointer impulses and other devices retain bounded execution.
* Longer blocking holds: fewer stops per second, but still idle throughout each decision,
  with slower corrections. Not chosen.

There is no autonomous renewal task, action ranking, control-name inference or timer-created help.
An earlier S1 selection explicitly authorizes input until superseded, released or lease expiry,
including the next decision call. Code never restarts expired input without another selection.

## Choice semantics, context and timing

Default is off: --continuous-input / -S1ContinuousInput enables the mode; UPA_RUN_CONTINUOUS_INPUT
is its strict boolean environment equivalent. Default-off labels, requests, contexts and traces
are unchanged. Deterministic fake measurements test exact serialized payload/trace equality, including a SHA-256
golden independently computed with the original HEAD TurnLoop and identical fixed inputs/clocks.

The first turn has no timing sample and retains the original bounded execution. At each later
turn start, perf_counter measures the preceding start-to-start interval, including external pacing,
readings and decision/execution time. The safety lease is twice the largest interval observed in
this generation, capped at 10 s. The factor two is an explicit safety/latency allowance, not a
claim about an observed percentile. No initial model latency is invented. The cap bounds risk;
a slower call or longer pacing stall permits expiry and a stop. A restart resets timing history.

Each eligible offered label retains the declaration and historical calibration text, then explicitly
states the exact new lease, that input runs during the next decision, that supersession releases
and reapplies, and that the short-hold calibration is not comparable. The very lease shown in this
choice set is bound to the selected request. Noneligible declarations keep their normal semantics.
The eligible set is host catalog data, never derived by the name-blind core.

Context facts state release semantics and, for an outstanding selection, action and choice id,
latest bridge receipt status, input_start_wall_s, age since acknowledged input start, receipt age and requested lease. The original
choice label identifies its physical controls. Receipt age is measured wall time since that
receipt, not measured continuous application duration. Current physical held state is explicitly
unknown until another bridge receipt: no local timer proves that a device remains held.
Terminal applied_duration_s and per-input receipts remain the evidence for actual duration.
Missing timestamps remain null. A terminal receipt collected before context removes the outstanding
selection. Input may expire while a context/decision is in flight; that context is time-stamped evidence.

## Effects and trace evidence

In continuous mode the per-action effect probe's calibrated comparison is disabled for the whole
session, including its initial bounded turn. The context explicitly calls those effects unknown.
Before-turn snapshots may still run but are never scored against the 0.4 s calibration. No zero,
match or mismatch is synthesized and no linear speed assumption rescales calibration.
Observation positions/readers remain measured facts. Future interval displacement can use two
stamped inspections with all overlapping action ids and a measured window, without attributing it
to a single action or comparing it with short-hold calibration; that richer probe is deferred.

A started asynchronous execution is returned to turn memory with its running receipt, not a fake
terminal receipt. Its complete execution trace entry is deferred until terminal collection, retaining
the original turn and decision reference; it can appear after that turn's record. Stop collects it
before closing. Failed starts preserve the sent request with empty records. An abrupt disconnect
may prevent terminal collection; physical release then rests on bridge disconnect handling, not
an invented receipt. Live trace replay is not applicable yet; fake turn records pass adherence.

## Release-only protocol addition

Existing stop closes the TCP session, including in the C# server. Therefore a wait cannot use
ordinary stop and still permit another decision on that connection. StopRequest.reason gains the
closed value `release`. It cancels/releases/verifies the current lease and sends the existing
StopResult while retaining the session, observation history, subscription and clock. It starts
no input. A failed verification closes the session. Every other stop reason keeps its existing
shutdown behavior. The client only keeps the connection open for this new reason.

Python is the contract authority; only stop_request schema changes. Valid release and invalid
renew fixtures cover the extension; invalid renew fails the reason literal. C# enum/validator and
server consume it. BridgeStopTests adds a release/observe/explicit-new-act check, which the
orchestrator must compile and execute. No replay or reconnect path is added.

## S1 ownership path audit

| Branch | Input and execution | Authorizing rule / implementation |
|---|---|---|
| Same eligible action | New request only after that selection; previous released/reapplied | S1 actuator ownership; TurnLoop._send |
| Different eligible action | Supersede with exactly selected declaration | S1 actuator ownership; _send |
| Noneligible action/skill | Release previous; execute selected bounded declaration | Selected choice only; _send |
| wait | Release previous, no act, then existing wait duration | Explicit no-input wait; run_turn / _release_active |
| finish | Release previous, no act; session stops normally | Explicit finish; run_turn / stop |
| escalate/delegate + continue | Only named executable while_pending choice starts input | S1 pending selection; run_turn / _send |
| escalate/delegate + wait, continue targeting wait, or default | Release previous, no act; helper semantics unchanged | Existing recorded pending-wait default; run_turn |
| Invalid answer/provider failure | Release previous, record rejection, send no act | Invalid answers never repaired; run_turn |
| Stale/rejected request | Record rejection; release old hold; no substitute/retry | Freshness binding and fail closed; _send |
| Expiry | Bridge releases; no automatic renewal | Safety lease only; BridgeActionLease |
| Timeout/disconnect | Session error cleanup; bridge disconnect releases; no replay | Fail closed; run_session / BridgeClient / bridge |
| Wall limit | Session stop first releases and collects, then closes | Harness boundary, never a choice; run_session / stop |
| Reset/restart/scene boundary | Release before restart/adoption or boundary context; reset timing after adoption | Fail closed epoch boundary; _release_for_restart / _adopt_restart / run_turn |

Audit verdict: implementation/fake paths pass S1 ownership checks. No real runtime trace exists
for replay. Core remains name-blind. No threshold selects help, an action or a secondary choice.

## Risks and live check

A held selection moves during observation, image acquisition and decision latency; corrections can
overshoot. Long pacing waits can expire the lease. High-water timing and a two-interval allowance
trade larger safety exposure for fewer gaps; no improvement claim is supported without live evidence.
S1 can select wait or another action at each turn. Generic keyboard inputs can have semantics for
which a long hold is inappropriate; S1 sees the explicit duration and retains that decision.
Other devices deliberately remain bounded. Supersession has a verified neutral gap, so this is
somewhat continuous, not seamless renewal. Existing schema consumers must learn the new reason
before enabling the flag. Terminal trace entries arrive later than their turn and early crash traces
may lack a terminal receipt. Release-only C# behavior and device timing need real validation.

After compile-checking the updated bridge and running its EditMode release test, the orchestrator
can use its existing run-session.ps1 arguments plus `-S1ContinuousInput -PacingBurst 0.25` (or another
already approved pacing setting). CLI equivalent: `--continuous-input --pacing-burst 0.25`.
Keep the usual call/wall budgets and one Editor; no paid trial was authorized or started here.
Check context lease/receipt facts, selection-to-request ids, supersession terminal receipts,
expiry after a deliberately delayed answer, wait/help-wait release, and finish release verification.
Compare recordings only after collecting those measured facts; staircase improvement is unproven.

## Review pointers

* `src/playtest/core/turn_loop.py:324`: observation boundary, offered lease and ownership branches.
* `src/playtest/core/turn_loop.py:399`: receipt and measured-age context facts.
* `src/playtest/core/turn_loop.py:781`: only selected requests start input; no retry or replacement.
* `src/playtest/core/turn_loop.py:850`: delayed terminal trace collection and release-only verification.
* `src/playtest/core/turn_loop.py:1118`: session shutdown releases before closing.
* `src/playtest/core/bridge_client.py:556`: nonblocking start and terminal collection.
* `src/playtest/core/bridge_client.py:765`: release-only response keeps the connection open.
* `bridge/Runtime/Transport/BridgeServer.cs:734`: release versus terminal session-stop behavior.

Validation logs stay local under `runs/continuous-input-validation/`. The full Python suite and
specified acceptance set are run separately. C# test execution and live-trace replay remain pending.
