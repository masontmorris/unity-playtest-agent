# What we take from Aura Autopilot, and what we keep

*1 November 2026. Source studied: https://github.com/ramenvr/autopilot (Apache-2.0, commit 48966d1). Ideas only; every
line here is implemented in our own code, name-blind, against our contracts. The source is cited per item.*

## Where the two systems stand

Both put a System One decision model (theirs: TypeSafe `jev-1.13.0`; ours: Liquid d1-ultra or mammoth-1, same request
shape) over **options**: macro actions with code executors, chosen at decision points, each with facts of its own. Their
`kit/harness.py` docstring reads like our `docs/design/options-layer.md`: "Executors do the fast part in code every
tick; Jev chooses what to do, at its tempo." We reached the same shape from the same failures.

The difference is who writes the harness. Theirs: a coding agent (Claude Code, Codex, OpenCode) writes `harness.py`
per game and prompt from a **codex** (the game's code and scenes as text), with privileged reads (`game.get` any
property, `game.call` any method, `game.set` writes) and a kit (`walk_to`, `jump_to`, `look_at`, `melee`). Ours:
preflight **measures** the game (calibration, entity, probe) and builds a manifest; the core is name-blind; S1 owns
every choice and escalation; adherence and replay prove it. We keep that. It is the hackathon's claim ("a second game
with no code changes") and it is what makes our evidence auditable. Nothing below writes game-specific code.

## Take, in priority order

### 1. The hop takes off like `jump_to` (kit/control.py `_jump_once`)
Theirs: take-off distance = `speed × t_apex + speed × (read + 0.5 × dt)`: the read's age and half a tick, because
what it reads is a frame old and the press lands within a step. It presses only once running speed has **plateaued**
(no faster than 0.3 s ago), retries up to 3 times backing off for a fresh run-up when it "ran past the take-off point
at too low a speed", and refuses before trying when the rise is above `reach_up_max` ("too high: X above the feet, a
jump reaches Y"). Ours: lead = speed × measured t_apex only; no latency term, no plateau check, no pre-check.
**Do:** add the latency term from the measured observation age and period; press the rise only at plateaued speed;
up to 3 attempts inside one `hop`; and a pre-check from the probe's blocker heights against measured `rise_peak +
short_step`, stated in the option's label as a measured fact ("wall ≥0.9 high, a jump reaches 2.7") and as an
outcome `rejected: too high` when it is known to be out of reach.

### 2. `recent` outcomes and a `continue` choice (runtime.py)
Theirs: the state carries `recent` (last 6 options: name, seconds, worked) and `now_doing`; the instruction says
"don't keep repeating one that keeps failing in the same situation". Mid-option decision points at `tempo` Hz; a
different pick replaces the running option only when preferred by `switch_margin` (hysteresis against dithering);
a single legal option runs without asking. Ours: decisions only at option end; the compact menu dropped history.
**Do:** `recent` (6 entries) in the compact menu; while an option runs, ask at a tempo with an explicit
`continue` choice so S1, not a margin, decides whether to switch (law 1); when exactly one choice is legal, run it
and record `decision.source: "only_choice"` with no call.

### 3. A felt map that persists (kit/nav.py, memory.py)
Theirs: `sonar` sweeps the capsule each way (`clear` distance, `by` wall/drop/steep/reach); `NavMap` is a lazily
felt grid per level (hit / solid / guessed / open, per storey) with A*, kept on the game so later walks know it; memory
across runs is `notes.md` (verified facts), `lib.py`, and the codex diff. Ours: the probe's down-rays and blockers,
settled points, outcome memory per cell, all per run.
**Do:** persist `OutcomeMemory` and a felt map per game+scene across runs (world coordinates make it valid; the
manifest cache already keys by game), load at session start so labels begin informed; add a felt map to memory from
probe samples (floor spans and walls per cell, 1-D for a side-view plane, grid for 3-D) and state it in option labels
("floor known 12 ahead, then wall 3.1 high") and in `traverse`'s termination ("floor ends in 1.2").

### 4. Generic detectors → findings (engines/unity.md "Detectors")
Theirs: errors and exceptions in the log, NaN transforms, falling out of the level, long falls, hitches, on-screen
counts above their maximum. Ours: preflight invariants and runtime errors.
**Do:** name-blind detectors over what we already measure: non-finite pose or velocity; a fall past the lowest
settled floor by more than the measured rise peak; observation interval spikes (hitch); exceptions in the Editor log
during play; a reader that exceeds its declared maximum. Each becomes a suspicion with evidence seqs, so the existing
reproduction path can promote it.

### 5. Stall → continue, not restart (kit/harness.py `stall_seconds`, `resume`)
Theirs: when nothing advances the plan for `stall_seconds`, the episode ends as stalled with "what was tried and where
the player stood", and the next harness **continues** from there (steps done stay done). Ours: S1 alone decides to
escalate; the frontier gets the clip and the record.
**Do (no threshold, S1 still decides):** the escalation packet carries memory's `cell_line` ("this place: hop+x 6
tried, blocked 6") and the position; the directive comes back as a **filtered menu with a setpoint** (bible 5.6),
which is their `Step.options`. A restart keeps outcome memory.

### 6. A blind describer (vision.py)
Theirs: frames go to a vision model with the agent's questions only, never the request, "so what the agent hopes to see
can't colour what it is told is there"; no agent reads frames itself. Ours: the frontier sees the clip and the
hypothesis together.
**Do, for findings:** evidence clips are described by a model that gets neutral questions ("what changed between
these frames?") and never the hypothesis; the frontier reads the description beside the clip. One flag; benchable
against the night run's false positives.

### 7. Unresponsive input as a measured state (runtime.py RESPONSE_*)
Theirs: the view not turning when the mouse was moved ≥10° for 2 s means dead, menu or cutscene.
**Do:** an option whose held move produces no displacement and no velocity for a window while the clock runs ends
`state_changed: input has no effect` (distinct from `blocked`, where the body pushes and stalls), and the fact
reaches S1 so it can choose `submit`/menu actions or restart.

### 8. Bench warm vs cold (bench/run_autopilot.py)
Theirs: the same prompt suite with memory cold or warm, headless copies, results per prompt.
**Do:** run our bench suite twice, memory cold and warm, once item 3 exists; report the difference. This is the
measurable form of "it learns the game".

## Not taken, and why
- **An LLM-written harness per game.** It is their core and the thing we are testing an alternative to.
- **`game.set` / `game.call` as play.** Writes and method calls are not a player's actions; we keep them for
  placement and reproduction only, reported as such (they report them too).
- **Legacy-input routing by recompiling the game.** Our prototype did it (s1-loop's LegacyInputRewriter); the
  hackathon build reads the Input System and treats legacy as a measured gap.
- **`switch_margin` as code hysteresis.** A measured margin deciding whether S1's new pick takes effect is code
  choosing. The `continue` option (item 2) gives S1 the same stability without that.
