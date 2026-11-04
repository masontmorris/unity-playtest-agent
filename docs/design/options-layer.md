# Options: closed-loop choices with measured outcomes

*1 November 2026. Shared spec for the parallel build on `feat/options-layer`.*

## Why

On FPSKit and DragonBlaze the agent fails the same way. S1 presses a wall face for hundreds of turns, misses a
jump into the same pit ten times, and never backs up. Faster S1 (d1-ultra, 54 decisions/min), held walks, and
facts about where the body went (live-09) did not change that. The cause is structural: **the unit of choice is
a button press**, chosen at about 1 Hz by a model that scores labels and keeps no memory. Motor control needs a
closed loop at frame rate; strategy needs memory of what was tried. One decision model at one rate does neither.

A player does not decide "press D for 0.4 s". They decide "get over that ledge", and their hands close the loop.
So the unit of choice becomes an **option**: a generic routine that runs a closed loop over measured primitives,
ends on a measured condition, and returns a measured outcome. S1 chooses options. Code runs the loop it chose.
What each option did at each place is remembered and written into that option's label, which is what a
label-scoring S1 actually reads.

Everything here is game-agnostic. Templates are defined over vectors, measured effects and generic termination
evidence. No game, object, control or genre names.

## Layers and rates

| Layer | Rate | Who | Does |
|---|---|---|---|
| Option executor | observation rate, about 20 Hz | code (runner) | runs the chosen option's closed loop until a termination condition |
| S1 | per option | decision model | chooses which option, with which parameter, given labels carrying measured outcome history |
| Strategy | on request | frontier | sub-goals with checkable setpoints, reading the same outcome memory |

## 1. Controlled-entity pose in every observation (bridge, owner W3)

`observation.public["controlled"]` is published on every observation tick:

```json
{"object_id": "<scene object id>", "path": "<hierarchy path>",
 "position": [x, y, z], "velocity": [vx, vy, vz] | null,
 "source": "rigidbody2d" | "rigidbody" | "character_controller" | "transform"}
```

- The entity is the one preflight measured (the stage-2 entity record). The runner passes its hierarchy path
  to the bridge through the existing session configuration (an environment variable such as
  `UPA_CONTROLLED_ENTITY`, set the way `UPA_PROBE_ANCHOR` is).
- Velocity comes from the physics body when there is one (2D or 3D); otherwise it is `null`, never a finite
  difference dressed up as a measurement.
- If the entity cannot be resolved, `controlled` is absent and the observation carries a reader error with
  the reason. Unknown is never zero.
- The geometry probe's anchor (`UnityProbeField`) also accepts this configured entity and `Rigidbody2D`, so 2D
  games get the probe. A probe ray cast in 2D uses `Physics2D`.

## 2. Placement (bridge, owner W3)

A tester's affordance, not a player's: put the controlled entity at a position, zero its velocity, let the
physics settle, and report what happened.

- Bridge verb `place`: request `{position: [x, y, z], settle_s: float}`. Result
  `{placed: bool, position_after_settle: [x, y, z] | null, moved_during_settle: float | null, reason: str | null}`.
- It acts only on the configured controlled entity, releases all held input first (verified, as `stop` does),
  and is recorded in the trace like any other execution.
- Python: `BridgeClient.place(position, settle_s)`. New protocol messages in `contracts/bridge.py`, with schemas
  re-exported.
- A placement whose entity keeps moving after settle (falling, pushed out of geometry) is reported as
  `placed: true` with the measured `moved_during_settle`; the caller decides what that means.

## 3. Primitives (runner, owner W1)

Built once per run from the calibration record. No new preflight model call.

- For each offered action whose calibration has valid trials with before and after positions: its mean
  displacement **vector per second of hold**, and its spread. This is measured, not inferred.
- **Up** is Unity's world `+y`, the engine convention for default gravity. Document it; don't rename it.
- **Move primitives:** actions whose vector is mostly horizontal (x and z). **Rise primitive:** an action whose
  vector is mostly `+y`. A primitive that fits neither is offered as before, as a plain action.
- Where calibration reports a basis (entity forward and right) and the vectors rotate with it, directions are
  basis-relative; otherwise they are world-relative. v1 must be correct for world-relative (side view, top-down).
  It must not silently mislabel basis-relative movement; it states that such a direction is not supported yet.

## 4. Option templates and termination (runner, owner W1)

Each option runs a closed loop at observation rate using `public.controlled.position`. Its inputs go through
the bridge's existing `act` with leases (`lease_s`, continuous supersession). A move held through a jump needs a
chord action with a lease, or two concurrent leases. If the bridge cannot hold one input steady while another
is pressed, report it; don't fake it with alternating presses.

| Template | Parameters | Does | Succeeds when |
|---|---|---|---|
| `traverse(d)` | a measured direction | holds the move primitive best aligned with `d` | progress along `d` reaches the requested distance, if one was given |
| `retreat(d, r)` | direction, distance | `traverse(-d)` for `r` | moved `r` along `-d` |
| `hop(d)` | a measured direction | retreats a run-up distance, then traverses `d` and at launch distance presses the rise primitive **while still holding the move primitive** through the flight until grounded or a flight-time bound | the landing position is beyond the start's forward reach along `d` by a margin |
| `wait(s)` | seconds | holds nothing | always, after `s` |

Parameters come from primitives: run-up distance from measured speed times the measured rise time; launch
distance and flight-time bound from the rise primitive's measured duration. Each constant names its source.

During a hop's run-up, a down-ray hitting a lower surface ends the current floor when the drop exceeds
the landing tolerance. The jump-height fall bound does not define floor continuity: a reachable lower
floor can still cost the hop its take-off height.

A hop backs up on the floor it starts on. With a probe, the back-up stops at the last sample whose ray one
step further back still reaches floor within the `fell` bound, and the note says
`run-up N.NN available of M.MM (floor edge)`. Without a probe it stops when the body has dropped below where
it started by the landing epsilon, and the note says `run-up truncated: floor ended after N.NN`. Either way the
hop runs with the run-up it has: a run-up shorter than the launch lead is reported, not replaced, and the lead
itself does not change.

**Termination conditions** are generic and measured. The first to occur wins and is recorded as `terminated_by`:
- `succeeded`
- `blocked`: progress along the commanded direction stays below a fraction of the expected progress at
  measured speed over a short window
- `fell`: vertical drop beyond the primitive-derived bound
- `state_changed`: scene change, game clock stopped, entity missing, or a death or health reader crossing to
  dead or zero where the manifest declares one
- `timed_out`: a bound derived from distance over measured speed, with a hard cap
- `rejected`: the bridge rejected an input
- `unmeasured`: no pose

**Outcome** (returned and traced):

```json
{"option_id": "...", "template": "...", "params": {...},
 "start_position": [...] | null, "end_position": [...] | null, "displacement": [...] | null,
 "terminated_by": "...", "duration_s": ..., "scene": "..." | null,
 "measured": {...}, "receipts": [<bridge execution records>]}
```

`params` stays exactly as requested by S1. Additional measurements belong in `measured`, an object
that may be empty; older traces may omit it. The trace entry keeps `receipts` beside `outcome`, not
inside it. The trace validator checks this shape and option identity; these checks are not encoded
in the exported JSON Schema.

## 5. Outcome memory and labels (runner, owner W2)

- **Place:** a cell from `floor(position / cell)` per axis, where `cell` is the measured displacement of one
  short move step (stated in the memory's header). Positions in one scene are world coordinates, so they stay
  comparable across play-mode restarts. The scene id is part of the key.
- **Memory:** per (scene, cell, option_id): counts by `terminated_by`, best progress along the option's
  direction, and the last outcome's end position. It also keeps the **settled points** visited: positions where
  the entity stood still and alive for a short window, since these are safe placement targets.
- **Label suffix**, rendered deterministically from memory, quantities only. For example:
  `here: 3 tried — blocked 3, best +0.1` or `here: untried`. No advice words.
- **Facts:** an optional one-line summary of the current cell's history for the frontier and S1 context.

## 6. Choices, turn loop and trace (owner W1)

- New `ChoiceKind.option`, with `option_id`, template and params in the choice. Offered with `--options`
  (`-Options`, `UPA_RUN_OPTIONS`). With options on, movement primitives are replaced by options built from
  them. Other verbs (attack, interact) stay as plain actions.
- Each option choice's label is its intent and measured expectation, plus the memory suffix.
- The turn loop executes an option choice through an `OptionExecutor` port the runner provides. The port runs
  the closed loop and returns the outcome. S1's decision is the option; nothing substitutes it.
- Trace: a new `TraceKind.option` entry per executed option, with body
  `{option_id, template, params, outcome, receipts}`, referencing the decision. Replay and adherence accept an
  option turn: its executed choice is the option S1 chose, and its receipts are the bridge's own.
- `place_at(target)` choices are offered with `--placement`. Targets are memory's settled points and preflight's
  reach-region anchors. The option calls `BridgeClient.place`, and its outcome includes `moved_during_settle`.

## Acceptance

- Unit tests over fakes: a fake side-view world with a one-tile step that `traverse` cannot pass but `hop`
  does; a pit that `traverse` falls into; a step against a wall where `hop` from zero run-up fails but `hop`
  with its run-up succeeds. Termination by each condition. Labels show history. The trace replays clean.
- Live: DragonBlaze Level1 with `--options --placement`, d1-ultra, unpaced. Report whether the body passes
  camera x −11.7 and reaches further than any earlier run, with the decisions-overlay video.

## 7. Interfaces between the parallel pieces (pinned; build against these exactly)

```python
# src/playtest/runner/options/model.py  (owner W1a; W1b and W2 import it)
TERMINATIONS = ("succeeded", "blocked", "fell", "state_changed", "timed_out", "rejected", "unmeasured")

@dataclass(frozen=True)
class OptionSpec:            # one offerable option, built from primitives
    option_id: str           # e.g. "traverse+x", "hop+x", "retreat-x:2.0", "place:<k>"
    template: str            # "traverse" | "retreat" | "hop" | "wait" | "place"
    params: dict             # JSON-ready; direction as a unit vector [x, y, z], distances in world units
    intent: str              # short label text, quantities only, e.g. "hop toward +x (run-up 2.1, rise 2.45)"

@dataclass(frozen=True)
class OptionOutcome:
    option_id: str; template: str; params: dict
    start_position: list[float] | None; end_position: list[float] | None; displacement: list[float] | None
    terminated_by: str       # one of TERMINATIONS
    duration_s: float | None
    receipts: list[dict]     # bridge execution records (model_dump(mode="json")) and place results
    scene: str | None

# src/playtest/runner/options/primitives.py  (W1a)
def primitives_from_calibration(calibration: dict, offered_actions: list[str]) -> "Primitives": ...
def option_specs(primitives: "Primitives") -> list[OptionSpec]: ...

# src/playtest/runner/options/executor.py  (W1a)
class OptionExecutor:
    def __init__(self, client, primitives, *, clock=time.monotonic): ...
    async def run(self, spec: OptionSpec) -> OptionOutcome: ...   # never raises for a measured failure

# src/playtest/runner/options/memory.py  (owner W2)
class OutcomeMemory:
    def __init__(self, cell: float): ...
    def observe(self, observation) -> None: ...            # settled-point detection from public.controlled
    def record(self, outcome: OptionOutcome) -> None: ...
    def suffix(self, scene: str | None, position: list[float] | None, option_id: str) -> str: ...
    def settled_points(self, scene: str | None) -> list[list[float]]: ...
    def cell_line(self, scene: str | None, position: list[float] | None) -> str | None: ...  # optional fact
```

W1b wires these into choices, the turn loop and the trace. W3's `BridgeClient.place(position, settle_s)` returns a
typed `PlaceResult`. W1a's `place` template calls it, and tests use a fake client.

## 8. Clearing a level from first principles (2026-11-02)

Live-38 completed DragonBlaze Level1 through gameplay: Unity loaded `Level2` after S1's `sweep` option pressed
the game's interact control at the level's end. The agent holds no knowledge of the level. Each piece below is
opt-in, and a run that sets none of them keeps its bytes.

| Flag (env) | What it adds |
|---|---|
| `--measure-air-rise` (`UPA_RUN_MEASURE_AIR_RISE`) | Before play, the rise is pressed from rest, then again at the apex it measured (`runner/options/air_rise.py`). `hop2±x` is offered only when every second press rose more than a quarter higher. Both hop labels then show measured flight numbers (`air T s, peak P`). DragonBlaze: 2.71 → 5.43. |
| `--decide-during-options` (`UPA_RUN_DECIDE_DURING_OPTIONS`) | Options, actions and skills run in the background, and S1 is asked again at once. Re-picking the running one keeps it going; any other non-help choice stops it first (`interrupted`, released, measured). A hop, a sweep or a press test is committed: the loop waits for it to finish before it observes and asks again. |
| `--keep-help-across-restart` (`UPA_RUN_KEEP_HELP_ACROSS_RESTART`) | An open help request survives S1's own restart into the same scene. Its answer is kept, and its directive is re-anchored to the new session. |
| `--frontier-advice` (`UPA_RUN_FRONTIER_ADVICE`) | The frontier is asked for gameplay advice (`roles.frontier.ADVICE_SYSTEM`), including an untried choice when everything tried at a place failed. |
| `--hold-met-directive` (`UPA_RUN_HOLD_MET_DIRECTIVE`) | A directive whose setpoint was met stays shown, marked `(met)`, until its own deadline. |

Always on:

- A plain action or skill that drops past the options' fall bound (`executor.fell_bound`) is `fell`, and so is a hop that lands that far below its take-off. A fall never sets a cell's `best`.
- One `stale_age` rejection of an option's own input is sent once more on a fresh reading. A plain action's or skill's stale rejection stands.
- The `sweep` template is offered by `choices.sweep_spec` at a place where at least three choices were tried, with the plain controls never tried there, least calibrated motion first. It presses each once and ends `state_changed` on a scene change, else `blocked`. Every control it pressed is filed as tried at that place, and each press is released and collected, so its measured end is in the receipts.
- With `--press-tests` (`UPA_RUN_PRESS_TESTS`), the `press_test` template is offered once per input a pre-registered action rule presses (`expectations.press_test_specs`): the input pressed 4 times with the rules' window between presses. It is committed, and it reads the readers itself around each press and through each wait (`measured["readings"]`), since S1's turns stop while it runs.
- Labels carry measured facts only: the escalate label's run help record, restart "drops the open advice", finish "scene unchanged since the run began", untried labels "N others tried here, none got on". Choice rows no longer repeat what their label already says.
- When the bridge closes the session (it does so on a scene change), one fresh connection reads the running scene. A scene different from the run's start ends the run `complete` with `ending: scene_changed`, and nothing is replayed.

### 3D bodies (FPSKit, 2026-11-02)

The same layer runs on a first-person body whose moves turn with it. Nothing in it names a game:

- A direction measured in the entity's own basis (calibration `world_right`/`world_forward`) is offered as `traverse:<label>`, `hop:<label>` and `hop2:<label>`, resolved against the facing at the option's start. FPSKit: a forward hop peaks at 1.94, a double one at 3.97.
- Background work that ended before a turn is collected at that turn's start and filed (memory, `recent`) before S1 is asked, so the question that follows a fall states it. The coverage fact counts background moves and options.
- Plain moves started in the background are dispatched inside the turn, so the input reaches the bridge as fresh as the decision.
- On a 3D probe only, the menu states:
  - where a tilted floor rises. It marks the move whose measured turn faces it ("floor rises this way"), or else the nearest turn, with what is left.
  - "no floor found D ahead", or a floor ahead lower than the highest measured jump peak. This goes on every straight-ahead choice and replaces the rising mark on that path.
- A restart that times out with the run's wall time used up ends the run `wall_limit`. The loop bounds a restart by the remaining time.

FPSKit live-08 to live-11 each climbed the long staircase to its top (y 7.1 to 7.3). S1 then stepped off the end each time: the strip moves step a fixed 2.0 units, and finish ranked second (0.16 and 0.12) behind a move. Choosing to finish there stays S1's decision.

A finish label carrying "height now H, highest reached M" (live-10 to live-11) was removed. A label matcher reads "now at the highest point reached" as completion whatever the objective. It said so on flat ground at every run's start, and in the FPSKit bug-hunt sessions S1 finished early with that label on the menu. A highest point also counts mid-jump readings, so it is not a standing height.
