# S1-selectable Play Mode restart

Issue #91 (loop recovery), related to #36. This note covers the Python side of an explicit, S1-selected
stop/start of Play Mode. **It has not been validated against a live Unity Editor yet.** The Python
contracts, runner adapter, loop wiring and tests are here; the Editor-owned host and the live wrapper are
a separate change (`scripts/unity/`, `bridge/`) owned by the Unity worker.

## Why a primary choice, not a detector

A dead or stuck play session cannot be fixed by another game input, and a threshold, timer, stagnation
detector or anomaly must not turn into help or a restart. `restart_play_mode` is therefore a **primary
choice** offered to S1, and S1 alone selects it. Code exposes the fact that the operation is available
and the valid choice; it never ranks it, selects it, or escalates to it. It is not a `while_pending`
target, and it is not a helper request. The core stays name-blind: the operation is a closed `ChoiceKind`
and carries no game name.

The choice is offered only when the configured Editor-owned host advertises `restart_play_mode` for this
run. A bench run, a run with no control directory, or a host that does not advertise it keeps exactly the
old choice list and order (`wait, escalate, delegate, finish`, plus manifest actions and skills).

## What one selection does

1. The turn loop records the request trace entry and invalidates any pending help.
2. It releases every held input through the bridge's existing `stop("reset")` and requires
   `release_verified is True`. An unverified or failed release is a terminal operation failure; the host
   never sees a ticket and no old input is replayed.
3. It generates one ticket, dispatches it once, and waits outside decision turns using bounded local
   checks. **No S1 call, context turn or synthetic wait choice is made while it waits.**
4. A ready result must be backed by a new play generation, the host's own ready state, a fresh bridge
   lock/token and a valid first observation on a new `BridgeClient`. An IPC `ready` string alone is not
   completion.
5. Only then does the loop adopt the new client and first observation. The next turn observes that
   session again and asks S1 normally.

Any other outcome (failed, cancelled, timeout, malformed, mismatched, unverified release) ends the run
closed: `ending="error"`, status `inconclusive`, with the concrete reason. The operation is never retried,
never resumed, and no next answer is replayed. An accidental disconnect outside this explicit path stays
fatal exactly as before; it never reconnects, restarts or replays.

## Mailbox

Files live outside the game project in a per-run control directory whose path the wrapper passes as
`UPA_SESSION_CONTROL_DIRECTORY`. Publication is an atomic same-directory temp-file write followed by a
rename. These are host control records, not gameplay input; they carry no credentials or API keys.

`config.json`:

```
version: 1
run_id: string
project_path: absolute string
catalog_path: absolute string
catalog_sha256: lowercase 64-hex string
deadline_utc: timezone-aware ISO8601 string
```

`host-state.json`:

```
version: 1
run_id: string
phase: starting | ready | stopping | stopped | failed
generation: string|null
scene_path: string|null
scene_handle: integer|null
ready_utc: timezone-aware string|null
error: string|null
capabilities: list (contains restart_play_mode when supported)
```

`request.json` (written by the runner only after a verified release):

```
version: 1
run_id: string
ticket_id: string
operation: restart_play_mode
from_generation: string
deadline_utc: timezone-aware string
```

`result.json` (written by the host, one terminal record per ticket):

```
version: 1
run_id: string
ticket_id: string
phase: accepted | stopped | starting | ready | failed | cancelled
from_generation: string
to_generation: string|null
entered_edit_mode_utc: timezone-aware string|null
entered_play_mode_utc: timezone-aware string|null
ready_utc: timezone-aware string|null
scene_path: string|null
scene_handle: integer|null
failure_reason: string|null
```

`cancel.json` (written by the runner when it stops waiting):

```
version: 1
run_id: string
ticket_id: string
operation: cancel_restart
```

Python contracts are in `src/playtest/contracts/session_control.py` with strict models
(`SessionControlConfig`, `HostState`, `RestartRequest`, `RestartResult`, `RestartCancel`). A generation is
one canonical lowercase, hyphenated UUID created per play instance (`Guid.ToString("D")` /
`str(uuid.uuid4())`). Both sides reject extra keys, bad types, and a stale or mismatched run, ticket or
generation.

A ready host state needs a measured generation, scene path, scene handle and ready time. A ready result
needs all three event timestamps, a nonempty scene path, the loaded scene handle and a `to_generation`
distinct from `from_generation`; its edit-mode, play-mode and ready instants must be in that order.
Failed and cancelled results need a nonempty reason and must not name a new generation. A value nobody
measured is `null`, never zero or a default.

## Bounds and identity

- Exactly **one ticket per run**. A second selected restart is refused; there is no queue or retry.
- The operation deadline is the smaller of the run's remaining wall time and **60 seconds**.
- Polling defaults to a **250 ms** bounded local file check. Polls are not turns and are never traced.
- A result for another run, ticket or `from_generation` is fatal. A ready result must also agree with
  `host-state.json` (same run, phase `ready`, same generation, same scene, and a `ready_utc` not older
  than the result's).
- The new bridge must be a genuinely new endpoint: a different token or start time than the released
  endpoint, and not older than the result's entered-play time. The first observation must report a
  running, unpaused session with a scene matching the result (or at least a named scene when the result
  has no path).
- A timeout (or any fatal evidence problem) publishes `cancel.json`. A late transition can never publish
  success for a cancelled ticket, and it is never read again.

## What resets and what persists

The run's wall deadline, call ledger, dollar budget and token pacing **persist** across a restart. Only
the scene-bound state is reset:

- new client, execution controller, observation store and decision memory;
- pending help and adopted guidance cleared; prior evidence remains historical, not current guidance;
- reader watch baselines and effect probe baselines reset;
- the view source and help port rebound to the new generation;
- a bounded `reset_summary` is shown once in the next context (from/to generation, scene, waits, checks,
  what was invalidated).

## Generations and frames

The host writes frames under `Temp/playtest-bridge-frames/<generation>/`. A run without a host generation
keeps the legacy flat directory exactly as before. Frame and clip manifests record the generation, and a
clip never crosses a generation boundary. The view source reads only its generation's directory and a
restart rebinds it to the new one; an old frame is never accepted as the new generation by timestamp.

## Trace

One restart turn contributes exactly two entries:

- `restart` / `request` (core; refs the decision): ticket, from generation, and no claimed outcome yet;
- `restart` / `completed` or `restart` / `failed` (core; refs the request): status, to generation, reason,
  release verification, check count and measured wall time.

There is no entry per poll. The `TurnRecord` carries `outcome="restarted"` and an `operation` with the
raw and effective choice ids, the ticket, both generations, the explicit `OperationStatus` and the
reason. A turn that restarts sends no gameplay input, so it has no `execution` or `effect` entry.

## Local verification

`playtest-restart-smoke` drives a scripted session through the configured Editor-owned host and bridge.
It makes no provider call and needs no model credentials. Unit tests use fake protocol peers and host
mailboxes; those tests alone are not Unity validation. Do not report live support until the Editor-owned
host has run this path for real.
