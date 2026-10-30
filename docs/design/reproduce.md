# Reproducing a finding k of n times

Issue #38. `reproduce` takes a candidate finding from a finished session and replays the session's recorded
inputs after a reset, n times, then reports how many attempts showed the finding's `repro_check`. For Day 6 it
is run by an operator: a code-driven procedure, not an S1 turn, and it calls no model. Making the same engine
a choice S1 can take mid-session is #127. A check is either a reader value or, since #154, the measured effect
of one action (see "Two kinds of check").

## Where findings come from

When S1 escalates, the frontier may return a `candidate_finding` with a `repro_check`: one manifest field, one
comparator and one value, the same shape as a directive's setpoint. Until #38 the runner dropped it. Now
`FrontierHelp` writes it to the trace as a `finding` entry in state `candidate`, carrying its severity, the
severity reason and the `repro_check`. Its field may also be an action's effect, `effect.<action>`; a directive's
setpoint may not name one.

The answer cites bridge observation seqs, and a trace entry must cite earlier trace entries, so each seq is
mapped to the entry that recorded its observation. If any cited seq has no entry, nothing is written and the
reason is returned. S1's guidance names the finding id and that reason. A citation is never dropped or
guessed to make the finding fit.

## Running it

```powershell
scripts/unity/reproduce.ps1 -ProjectPath <game> -TracePath runs/<run>/trace.jsonl `
    -ManifestPath <the manifest the session used> -OutputDirectory runs/<new folder> `
    [-FindingId f-17] [-ReproCheckPath check.json] [-Attempts 5] [-SettleSeconds 3]
```

The script plans from the trace, exports the manifest's catalog, and runs one PlayMode host under the
single-Editor mutex. It refuses to start while the game is open in an Editor, and it never writes to the
game project. The output folder must be new. It holds `plan.json`, `catalog.json`, the report
`reproduce.json`, the reload handshake files and `provenance.json` (the game's git state before and after).
Without `-FindingId` the latest candidate is used. `-ReproCheckPath` supplies a check when the finding has
none, or overrides it. An override never moves the finding (see below). Either way, the plan records whether
the check came from the finding or the operator.

## What replays

- **The inputs before the evidence.** Every execution that completed with its release verified, in trace
  order, before the finding's last cited evidence entry. An input sent after that entry cannot have caused
  what the evidence shows, and replaying it could undo it.
- **Rejected or failed inputs** sent nothing in the session. They are counted as skipped and never replayed.
- **Timing.** Each input keeps its action, params and lease, and starts at its recorded offset from the first
  input, as the bridge measured it. When the evidence is an observation, its offset is kept too.
- **The reset** is a numbered scene reload served by the host. It is the only reset calibrated on FPSKit
  (`docs/probes/2026-10-27-fpskit-reset.md`). Save/load and checkpoint resets need bindings the manifest
  does not have (`preflight-reset-methods.md`).

Each attempt reloads, reconnects, installs the readers, waits one second and reads a baseline. Then it
replays every input once, never retried or replaced. It reads after each input, then samples through a
settle window that reaches at least `settle_s` past the evidence's offset. An effect check does the same
replay but reads snapshots instead of readers (see below).

## Two kinds of check

A `repro_check` is one field, one comparator and one value. The field is a reader or an action's effect.

| | Reader check | Effect check (#154) |
|---|---|---|
| Field | `public.<reader>` | `effect.<action>` |
| It is | a manifest reader's value | the net displacement, in world units, of one execution of the action |
| Measured | by the readers: after the reset, after each input, through a settle window | between two position snapshots of the manifest's entity, around each replayed execution of the action |
| Comparators | all eight | `<`, `<=`, `>`, `>=` with a number and no tolerance; `changed`, `unchanged`, `==` and `!=` are refused |
| Compared with | the reading after the reset (the baseline) | the displacement calibration measured for the action |

The contract refuses an effect check with any other comparator, a value that is not a finite number, a tolerance
or no action name (`ReproCheck._effect_is_a_bound`), so a plan, a replay and a trace entry never hold one.

**Why an effect check.** A reader check needs a state that goes wrong: false right after the reset, true once the
bug shows. An action that stops having its effect makes no such state. A controlled entity blocked by an obstacle
stays where it started, every reader reads what it reads at rest, and "nothing moved" is exactly the rest the
baseline rule sets aside as `unknown`. What is wrong is the action's effect, and calibration measured that on the
same build before play: the net displacement of one execution of the action. An effect check compares one replayed
execution's measured displacement with a threshold the frontier chose.

**What the plan records.** `plan.json` has an `effect` block (null for a reader check): the action, the
calibrated expected displacement (`build_choices(...).expected`, the figure the live probe states), the
allowance around it (`probe.allowance`: the square root of the calibration's sample variance plus the stage 7
measurement allowance; null when calibration recorded no variance), the hold it was calibrated for, and which
input is scored. The scored input is the last replayed execution of the action before the evidence, and it is
marked `scored` in `inputs`. Every replayed execution of the action is measured; only the scored one decides the
attempt. The plan is refused, with the reason, when
- the action is not in the manifest, or has no calibrated expected displacement (a skill, a chord, an unmeasured
  hold, or an action that is not supported);
- no input that completed before the evidence is an execution of the action (a rejected one sent nothing);
- the scored input was not the calibrated declaration: it carried parameters, or ran for another hold than the
  one calibrated. Calibration measured one declaration, so its number is not the displacement of any other. A
  lease recorded as null is the declaration's own hold and passes.

**What an effect attempt measures.** It reads no reader. After the reset it replays every input as before. For
each execution of the action it takes a position snapshot of the manifest's entity (`inspect("transforms")`,
through the runner's `make_snapshot`) just before the observation the input is bound to, and another right after
the input's terminal record, as `DisplacementProbe` does. The reading before an input is never taken between
observing and acting, so it adds nothing to the age of the observation the act is bound to. The attempt records the
distance between the two positions with both snapshots' inspection seqs and game times. A snapshot that cannot be
taken, or one with an unknown position, leaves the displacement `null`: unmeasured, never zero. There is no
settle window, because a displacement is measured at its own input.

The PlayMode host needed no change. `BridgeEditorHost` attaches its inspection source whatever catalog it
serves, so the replay host answers `inspect("transforms")` the way the session host does.

## Scoring

### Reader check

The check is scored with the shared evaluator, `roles.checks.evaluate`, first on the baseline.

| Attempt | Result |
|---|---|
| The check already held after the reset, before any input | `unknown`: the replay cannot show it |
| The baseline could not be scored | `unknown` |
| The baseline did not hold, and a later reading satisfied the check | `reproduced` |
| Later readings were scored and none satisfied it | `not_reproduced` |
| No later reading could be scored | `unknown` |
| A replayed input was rejected or failed | invalid: the replay diverged |

The baseline rule is what makes a reproduction mean something: from a reset where the check is false, these
inputs lead to a state where it is true. An `unchanged` check always holds at the baseline, so it is always
`unknown` here, and so is a check of a value the game already shows at rest, such as a speed of zero. All six
candidates the frontier proposed in the #132 and #133 validation runs were `unchanged` checks or equality
checks against zero (a motion vector, the view bob, the yaw). The frontier's prompt and its `repro_check`
description now state this rule (#141).

### Effect check

The counterpart of the baseline rule is the calibrated displacement. A replay can show a difference only if the
check is false for the calibrated value: a check the calibrated displacement satisfies is satisfied by the
action working as calibrated, so it can never show a bug.

| Attempt | Result |
|---|---|
| The check holds for the calibrated displacement | `unknown`: the check holds for the calibrated effect, so the replay cannot show a difference |
| The scored execution's measured displacement satisfies the check | `reproduced` |
| The scored execution was measured and its displacement does not satisfy the check | `not_reproduced` |
| The scored execution was not measured: a snapshot failed or a position was unknown | `unknown` |
| A replayed input was rejected or failed | invalid: the replay diverged |

The first row is decided by the check and the plan alone, but the attempt still replays, so the measured
displacements are kept as facts beside the `unknown`. The comparison is the check's own value against the
measurement, with the shared evaluator. The allowance is recorded and reported beside the calibrated value so a
reader can see how far a threshold lies from ordinary variation (the frontier is shown it with the expected
value), but no code widens a check or decides on it. Another execution of the action that was measured but not
scored never changes the result.

k counts reproduced attempts and n counts reproduced plus not reproduced, for either kind of check. Unknown and
invalid attempts are in neither, and each is listed with its reason. The report also gives the largest deviation
between a replayed input's start and its recorded offset.

## The finding's next state

The session trace is closed when the session ends and is never rewritten. The report carries the finding's
next state instead: `reproduced` when k ≥ 1, `repro_attempted` when k = 0 with n ≥ 1, and none when nothing
was scored. Its `repro` field records n, k, the clock mode (`realtime`), the reset (`scene_reload`) and the
timing tolerance (for an effect check, that it is a net displacement between two position snapshots, since the
check takes no tolerance). The report export (#39, `report.md`) joins it to the finding; it shows an effect
check's measured displacement for every attempt, beside the calibrated one, and `unknown` where none was measured.

An operator check that overrides the finding's own is a different test, such as the controls in
`docs/probes/2026-10-29-fpskit-reproduce.md`. Its replay is reported, but its `finding_update` is null and
`finding_update_reason` says why. Only the finding's own check, or an operator check for a finding that had
none, can move the finding.

## Limits

- **Realtime replay.** Inputs start within a measured deviation of their recorded offsets, not exactly on
  them. The deviation is reported with every attempt.
- **Readings between inputs.** Like the session's own observations, readings are taken after each input
  is released and while the game settles. A value that exists only while an input is held is not sampled.
  On FPSKit, `movement_vec` reads zero at every such moment (`docs/probes/2026-10-29-fpskit-reproduce.md`).
- **What a displacement includes.** It is the distance between two snapshots, so it includes anything else
  that moves the entity while the input runs, such as gravity or a push. Like the live probe, and unlike
  calibration, it subtracts no idle baseline. The snapshot before an input also starts that input later than its
  recorded offset by the snapshot's own time; the deviation is reported with every input.
- **One entity.** An effect check measures the manifest's one entity, found by the id the manifest names. That id
  is `scene:<path>@<index>::<hierarchy indices>`, so a reload that puts the scene back at the same place finds
  it again. This has not been exercised against a live Editor (#154 ran no Unity); an entity that is not found
  leaves the displacement unknown.
- **One check per run.** A second check needs a second run.
- **A reset is not a fresh session.** Each attempt reloads the scene inside one PlayMode session. Anything
  the scene reload does not reset is not reset.
