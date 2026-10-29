# Reader facts, suspicions and finding records (#125, #37)

A live session now sees the game's own values, and the loop turns what it measures into recorded facts.
Nothing here chooses, ranks or escalates: a suspicion is a fact (S1 ownership rules 3 and 4), and S1 decides
what to do with it. The core stays name-blind: readers, rules and metrics come from the manifest and the probe.

## Readers in the observation

The manifest's stage 4 readers are installed when a session starts (`playtest.runner.readers.FieldReaders`).
Before each turn's observation the runner reads them all with one Editor `inspect("fields")`, and the loop
installs the values in that observation's `public.<alias>` (`playtest.core.readers.install`). The reading is
taken before the observation, never between it and the act, so it adds nothing to the age an action is bound to.
It costs one inspection a turn (the FPSKit scene's 312 field rows are about 390 kB of JSON); a bridge-side producer would be
atomic with the observation and is the long-term home. This is the stopgap.

- A reader is matched by its field id, and the live row must still carry the manifest's descriptor (scene,
  object, component, declaring type, field type). Otherwise it is unknown with the reason.
- A reader that cannot be read is `null` in `public` and has a `reader_errors` entry saying why: not in the
  live scene, descriptor differs, value null, destroyed, unsupported, not finite, truncated, or a different
  kind than the manifest declares. It is never zero, false or an earlier value. A name the bridge already
  publishes (`camera`) is not overwritten.
- At install time one reading must resolve at least one reader; none resolving ends the run before any paid
  call (the scene is not the one the manifest was made for). A lost bridge (`BridgeError`) ends the run; it is not
  a failed read.
- `run.json` records `readers.declared`, `readers.installed` (resolved and unresolved with reasons), `reads`,
  `failed_samples` and `unread_by_reader`. `plan.json` lists the aliases and the invariants.

## What S1 reads

Facts, after the last turn's own facts and in this order (one line per item, nothing ranked):

1. `The readers were read at game time … (inspection seq …); this observation is at …` (or that they could
   not be read).
2. For each violated invariant that began earlier: `Invariant <name> still does not hold (since turn …,
   suspicion s…): …`.
3. For each reader, in manifest order: `public.<alias>: now <v>; previous <v>; earlier range <lo> to <hi>
   (<n> earlier readings).` The range is of the earlier readings and never includes `now`. A flag or string
   lists the values seen; a vector gives a range per component; `unknown` is a reading nobody measured.
4. Each suspicion not yet stated, as `Suspicion s<n> (<basis>, trace_seq <seq>): <statement>`.

Statements carry measured values only. A test scans every line the watch writes for advice words.

## Suspicions

`playtest.core.suspicion.ReaderWatch` raises a `Suspicion` (`playtest.contracts.investigation`) when:

| basis | when | evidence it cites |
|---|---|---|
| `invariant` | a manifest invariant (`range`, `monotonic`, `implies`) does not hold for the readings, scored by `playtest.core.evaluate.evaluate_invariant` over the reading and the one before it. Each of its checks is scored by the shared `playtest.roles.checks.evaluate`, the same function that scores a directive's setpoint. Unknown readings make a rule unknown, never violated. | the observation before and the one it was found in |
| `range_exit` | a reader's value leaves the range its earlier readings in the session showed (numbers: min..max; flags and strings: the values seen; vectors: per component). | the current observation, and the observations that set the earlier minimum and maximum |
| `effect_mismatch` | an action's measured effect differs from its calibrated one by more than the calibration's recorded spread allows. | the execution and effect entries |

**When one is raised.** An event is raised when a state begins, not on every turn it lasts. A range exit is raised
once and again only after a reading came back inside the (extended) range; a violated invariant is raised once
and restated as "still" while it lasts; an unknown reading neither raises nor ends either. A reader's own line
shows its value against its earlier range every turn, so a departure stays visible without a record each turn.

**Warm-up.** Leaving a range is stated only after `RANGE_MIN_READINGS = 10` earlier known readings: fewer is a
point or a pair, not a range the session has shown. The number is fixed here, ahead of any run, and gates a
fact only.

**The effect allowance** is the stage 7 formula (`playtest.preflight.verify.effect_tolerance`), fixed before any
run: the square root of the calibration's sample variance plus the calibration's measurement allowance
(`2 * IDLE_TOLERANCE + EFFECT_TOLERANCE`, 0.05 world units). A calibration with no recorded variance, or an
effect that was not measured, has no comparison: unknown, not a mismatch. The probe supplies the allowance in
`EffectReport.comparison`; the watch only compares.

## The records

`Suspicion` (a local record, also the body of a `suspicion` trace entry; schema `schemas/suspicion.schema.json`):

```
suspicion_id   "s1", "s2", … unique in the run, in the order raised
basis          invariant | effect_mismatch | range_exit
turn           the turn it was raised in (equals the trace entry's turn)
based_on_seq   bridge seq of that turn's observation
game_s         game clock of that observation, or null
subject        the invariant's name | the choice id | "public.<alias>"
statement      the fact S1 reads (measured values only)
detail         the same numbers as data; keys by basis (below)
evidence_seq   trace_seq of the entries it rests on (equals the entry's refs)
```

`detail` keys: `invariant` → `invariant`, `rule`, `readings`, `baselines`, `reason`. `range_exit` → `field`,
`value`, `previous`, `readings` (earlier count), `shape`, `min`/`max`/`min_seq`/`max_seq` (numbers) or `values`
(flags, strings) or `components`/`min`/`max` (vectors), `required_readings`, `read_game_s`, `inspection_seq`.
`effect_mismatch` → `choice_id`, `metric`, `unit`, `expected`, `observed`, `difference`, `spread`, `allowed`, `basis`.

**Trace kinds** (`playtest.contracts.trace.TraceKind`, documented in `playtest.core.trace_sink`):

- `suspicion` (author `core`, in a turn): body is the `Suspicion`; `refs` are its `evidence_seq`. From an
  observation it follows the `choice_set` and precedes the `context` (so the context states it with its
  `trace_seq`); from an effect it follows that `effect` entry and is stated in the next context.
- `finding` (author `core`, `frontier`, … ; inside a turn or after the run): body is a `Finding` in one state;
  `evidence_seq` must be among `refs`. A finding is never edited: a new state is a new entry with the same
  `finding_id`. `Finding` already requires a candidate or later to cite evidence; `FindingLog.record` makes each
  citation a ref, and the sink refuses one that is not an earlier entry, so a citation that does not exist cannot
  be written.

**For the next steps** (`playtest.core.findings`, `TurnLoop`):

- `loop.suspicions` → `[RaisedSuspicion(trace_seq, suspicion)]`; `loop.observation_entries` → `{bridge seq: trace_seq}`
  of every observation, to turn a seq a model cites into the `trace_seq` a finding cites. The same maps come
  out of any trace: `suspicions_in(entries)`, `observation_entries(entries)`.
- `FindingLog(sink, author=…).record(finding, turn=…)` writes a state; `latest_findings(entries)` and
  `findings_in(entries)` read the newest state, or the history, by `finding_id`.
- A suspicion is evidence to cite; it is not a finding and has no state. Whoever judges (the frontier proposing a
  candidate, a reproduction attempt moving it on) writes the `finding`.

## Limits

- The reading is Python-side, one inspection a turn, taken just before the observation, so a value can be a
  fraction of a second older than the observation it is installed in; the header line states both clocks.
- Only the readers the manifest selected are visible, and only what is in `public`; nothing is read during a hold.
- Reader freshness classes are all `null` in the manifest, so `depends_on` stays empty.
- `Finding.expected_source` has no value for "the calibration said so"; a candidate written from an effect
  suspicion has to use one of the four existing values. Not changed here: the frontier's provider schema is
  rendered from it.
- No `hypothesis` or `directive` trace kinds yet (the frontier hand-off adds them).
- C# parity for the new contract is pending, not verified: there is no C# consumer of `suspicion` or of the new
  trace kinds.
