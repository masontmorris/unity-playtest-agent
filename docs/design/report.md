# The report export

Issue #39. `playtest-report` turns one session's run folder, plus any `reproduce` outputs for its findings
(#38), into `report.json` for tools and `report.md` for people. It reads files only: no model call, no
Unity, and nothing in the run folder is written.

```sh
uv run playtest-report --run runs/<session> [--reproduce runs/<replay> ...] [--finding f-17] \
    --output runs/<new folder>
```

`--trace` reads a trace other than `<run>/trace.jsonl`. The output folder must be new.

## One finding, one claim

The eval scores each exported finding as one claim: under these conditions, this state is wrong. It matches a
claim on the build, the root behaviour, a compatible trigger, and a specific expected and actual state. So each
finding in the report carries:

- **Build:** the game, its commit, and the paths its worktree had modified.
- **Trigger:** the scene, the conditions and the steps.
- **Wrong state:** expected, its source, and actual.
- **Evidence:** each cited trace entry. For an observation, that is the game time, the scene, the value of the
  repro_check field and the moment in the session video.
- **Severity** and the reason given for it.

The finding appears as recorded. The state the replays measured is shown next to the recorded one, never in
place of it.

## What the report adds

| Section | Source | When it is missing |
|---|---|---|
| Replays | each `reproduce` folder whose plan was made from this trace (same SHA-256) for this finding | "No replay has been run" |
| Clip | the frontier call for the request whose adopted answer recorded the finding, checked against the SHA-256 it recorded | the reason: no proposal in the trace, no call record, no clip, or file gone |
| Session video | `recording/recording.json`; offsets are approximate, as its caveat says | "not recorded" |
| Run configuration | the trace's `run` and `end` entries and `run.json` | each value it lacks is `unknown` |
| Cost | `calls/`, one record per paid call | an unknown cost stays unknown |

- **Replays.** Every attempt is listed with its result and reason, including unknown and invalid ones. A replay
  folder for another trace, or for a finding not in the report, is listed under "Replays not applied", with
  the reason.
- **A replay moves the finding only on its own check.** That means the finding's own `repro_check`, or an
  operator check when the finding had none. An operator check that overrides the finding's own is a different
  test, such as a control. It is shown, but it never changes the state. The report applies this rule even to
  replay reports written before `reproduce` did.
- **Proposals the runner could not record** are listed with their reason, for example a citation with no trace
  entry.
- **Cost.** Measured spend is summed per role. Calls of unknown cost are counted apart, with what was reserved
  for them. The upper bound is null when any of them had no reservation. The preflight that made the manifest
  is outside the run and is not included.

## Remaining uncertainty

Each finding ends with the frontier's own uncertainty, followed by what the measurements leave open:
- no replay yet, or no replay that could change the state, and why;
- unknown and invalid attempts with their reasons;
- how far replayed inputs drifted from their recorded times;
- a missing or mismatched clip.

## Checked on real files

The #132 session, with its rebuilt candidate finding and the three replays from
`docs/probes/2026-10-29-fpskit-reproduce.md`, gives this report:
- **Finding:** it stays `candidate`. Its own check was unknown in 3 of 3 attempts, and the two operator controls
  are shown without moving it.
- **Evidence:** the cited observation is 18.9 s into the session video.
- **Cost:** 14 frontier calls measured at $0.1710. The 96 mammoth-1 S1 calls are listed as unknown cost,
  with $0.096 reserved.

#39 is done when a report is exported for a reproduced seeded FPSKit bug. That is the Day 6 gate run (#41).
