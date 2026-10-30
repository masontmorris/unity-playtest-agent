# Reproducing a finding k of n times

Issue #38. `reproduce` takes a candidate finding from a finished session and replays the session's recorded
inputs after a reset, n times, then reports how many attempts showed the finding's `repro_check`. For Day 6 it
is run by an operator: a code-driven procedure, not an S1 turn, and it calls no model. Making the same engine
a choice S1 can take mid-session is #127.

## Where findings come from

When S1 escalates, the frontier may return a `candidate_finding` with a `repro_check`: one manifest field, one
comparator and one value, the same shape as a directive's setpoint. Until #38 the runner dropped it. Now
`FrontierHelp` writes it to the trace as a `finding` entry in state `candidate`, carrying its severity, the
severity reason and the `repro_check`.

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
settle window that reaches at least `settle_s` past the evidence's offset.

## Scoring

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

k counts reproduced attempts and n counts reproduced plus not reproduced. Unknown and invalid attempts are
in neither, and each is listed with its reason. The report also gives the largest deviation between a
replayed input's start and its recorded offset.

## The finding's next state

The session trace is closed when the session ends and is never rewritten. The report carries the finding's
next state instead: `reproduced` when k ≥ 1, `repro_attempted` when k = 0 with n ≥ 1, and none when nothing
was scored. Its `repro` field records n, k, the clock mode (`realtime`), the reset (`scene_reload`) and the
timing tolerance. The report export (#39, `report.md`) joins it to the finding.

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
- **One check per run.** A second check needs a second run.
- **A reset is not a fresh session.** Each attempt reloads the scene inside one PlayMode session. Anything
  the scene reload does not reset is not reset.
