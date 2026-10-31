# Continuous input (opt in)

Status: implemented for review; Python fake-peer validation only. No Unity run, provider call,
commit or push was performed. C# compilation, shared-fixture parity and live continuity remain unverified.

## Distance and decision cadence: step versus held movement

Distance per decision = speed * decision interval. Using the supplied trial's 2.03 units per
0.4 s gives 5.075 units/s; at a measured 1.7 s cadence the calibrated product is 8.6275 units.
That estimate explains why an input held throughout deliberation can cross a narrow alignment
region before another correction. A bounded step remains about 2.03 calibrated units, followed by
an idle decision period. These are diagnostic quantities, not evidence of task success.

A held walk can reduce idle gaps while crossing open ground. Steps can retain precision when
lining up with a narrow target, approaching an edge or making a small correction. Only S1 sees
the scene and objective and chooses which trade is appropriate; there is no distance threshold,
geometry heuristic, score or timer that selects a form or asks for help. S1 needs the bounded
step's calibration and duration, held rate, recent interval/product, lease limit, current receipt
and the scene evidence to judge clearance. Missing timing is explicitly unknown.

Cadence depends on the token budget and payload size as well as provider latency, pacing waits,
observation/view work and blocking action/skill execution time. More options also consume tokens.
A faster inner decision loop would shorten held travel between corrections and make the precision
trade less severe, while lease expiry and supersession gaps would still apply. It would not
establish improved task performance without a separately approved evaluation. This implementation
makes no improvement claim and runs no paid comparison.

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

Every eligible original choice remains an unchanged bounded step (0.4 s in manifest 2),
blocking until its terminal receipt and measured against its original calibration. Continuous mode
adds one action-kind variant with id `<original>.held`, the same manifest action and params.
Choice ids are opaque nonempty strings and must be unique (contracts/loop.py); suffix collisions
fail construction rather than renaming an original. Only keyboard-only, non-skill declarations with
positive measured displacement over a requested hold > 0.2 s qualify. Eligibility is runner-side.
Manifest 2 therefore grows from 27 to 31 options. Liquid d1 already codes options beyond 26 as
`00`, `01`, and so on; no renderer change is required.

At each turn start, perf_counter measures the preceding start-to-start interval, including external
pacing, readings and decision/execution time. The safety lease is twice the slowest of the last
three intervals, capped at 10 s. After a slow turn leaves that window the lease can shrink again.
Before the first interval a held selection uses its original calibrated lease, asynchronously;
its distance per decision is unknown. Restart resets timing history. No latency is invented.

Runner-side held labels state calibrated net displacement / requested hold (units/s), the latest
measured start-to-start interval, their product as expected distance per decision, and the lease.
This product is a calibration-based estimate, not measured future travel or a guarantee: obstacles,
acceleration and lease expiry can change it. The latest interval describes the recent cadence;
the three-interval maximum serves only the safety lease. Labels reach both the ordinary choice
set and the while_pending follow-up. The core gets opaque ids, leases and a runner label callback;
it does not infer movement semantics or select a form on S1's behalf.

Context facts state release semantics and, for an outstanding selection, action and choice id,
latest bridge receipt status, input_start_wall_s, age since acknowledged input start, receipt age and requested lease. The original
choice label identifies its physical controls. Receipt age is measured wall time since that
receipt, not measured continuous application duration. Current physical held state is explicitly
unknown until another bridge receipt: no local timer proves that a device remains held.
Terminal applied_duration_s and per-input receipts remain the evidence for actual duration.
Missing timestamps remain null. A terminal receipt collected before context removes the outstanding
selection. Input may expire while a context/decision is in flight; that context is time-stamped evidence.

## Offline manifest 2 example

Computed from local `runs/s1-trial/inputs/manifest-2.json`, with an illustrative measured interval
of 1.70 s and lease 3.40 s (no new live measurement):

* Step `move_forward`: `walk forward to explore: request a 0.4-second hold of w; calibrated net displacement 2.032 world units (sample variance 0.001862, n=2); calibrated view turn yaw 0.000 degrees, pitch 0.000 degrees (spread 0.000 degrees, n=2).`
* Held `move_forward.held`: `move_forward: keeps input held until next decision or lease ends (lease 3.40 s); calibrated rate 5.08 world units/s; decision interval 1.70 s; expected distance/decision 5.08 world units/s * 1.70 s = 8.64 world units`

The eligible declarations are `move_forward`, `move_back`, `strafe_left`, and `move_right`.
Displayed numbers are rounded; the product uses unrounded calibration and interval values.

## Effects and trace evidence

Only held variants skip calibrated effect comparison: their execution window differs from the
short calibration. Bounded originals retain the existing probe and calibration. Before a bounded
execution, the loop first releases and verifies any outstanding hold, then takes a fresh probe
baseline so held travel during deliberation is not attributed to the step. The act remains bound
to its original observation; a stale request rejects without repair. Other bounded declarations
also retain their ordinary effect measurements. Held effects remain unknown; no zero, match or
mismatch is synthesized. Richer interval displacement attribution remains deferred.

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
| Selected held variant (including same action) | New leased asynchronous request only for the selected held id; previous released/reapplied | S1 actuator ownership; TurnLoop._send |
| Selected original step | Release/verify previous hold, refresh probe baseline, run original bounded lease and measure effect | S1 actuator ownership; _send |
| Noneligible action/skill | Release previous; execute selected bounded declaration | Selected choice only; _send |
| wait | Release previous, no act, then existing wait duration | Explicit no-input wait; run_turn / _release_active |
| finish | Release previous, no act; session stops normally | Explicit finish; run_turn / stop |
| escalate/delegate + continue | Both step and held variants are offered; only the named form executes, with its own release/probe semantics | S1 pending selection; run_turn / _send |
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
overshoot. Long pacing waits can expire the lease. A three-interval maximum and a two-interval allowance
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


## Step/held revision validation

The requested targeted acceptance command passed 225 tests; `uv run pytest -q` passed 2,458 tests
(exit 0). Logs: `runs/continuous-input-validation/acceptance-final.log` and `full-final.log`.
The full suite emitted a fake-peer request-handler exception notice but reported no failed tests.
New fake-peer tests cover first-turn selected holds, verified release before a primary or pending
step, fresh probe baselines, original leases, exact choice/action binding, runner flag gating,
label arithmetic and unknown timing, and id collisions. The original default-off serialized
payload/trace SHA-256 golden still passes. Ownership audit: pass for implementation and fake
paths; live evidence for this revision remains unverified. No paid/provider call, Unity launch,
commit, push or branch creation was performed.

Live acceptance should check that S1 uses steps for alignment and holds for open travel, with no
claim of improved performance until measured. Check both forms in the pending-help follow-up,
31-option rendering, current interval/rate/product labels, bounded-step effects after a verified
release, every existing fail-closed release route, and expiry under a delayed decision. The extra
post-release probe reading can age the bound observation; confirm stale-rejection counts without
adding a retry or replacement action.
