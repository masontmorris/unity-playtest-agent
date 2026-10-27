# The S1 bench: FPSKit corpus, labels and scoring

Issue #24. The bench exists to compare prompts, contexts and S1 models on frozen inputs before paying
for live runs. It is not a probe: a probe measures capability, latency and cost on invented packets,
which is what `scripts/probes/` does. The bench measures choice quality on frames recorded from the
game, against labels a person wrote.

This note predeclares the experiment and records its preparation. A 40-frame corpus and
assistant-drafted labels now exist. The first replay is recorded in
`docs/probes/2026-10-27-fpskit-s1-bench.md`; its labels have not yet been reviewed by a person.

## Decision

- Frames come from **FPSKit**, recorded through the bridge from the same checkout the preflight runs
  use. No frame, label or body is taken from the prototype.
- A frame is replayed through the production context builder (`playtest.core.context.build_context`),
  so the bench measures the payload S1 will actually receive, not a copy of it.
- Labels carry the labeller, date and rationale. On 27 October the user asked for assistant-drafted
  labels to review later, and approved proceeding with low-cost replay before that review. The
  draft's authorship and pending review are explicit; results using it are preliminary.
- No paid call starts before the call count and cost envelope below are approved (#25).

## Unit and estimand

- **Unit:** one decision frame — one observation, the facts known at that point, the recent record and
  the choice set S1 had.
- **Primary metric:** top-1 acceptable — the fraction of labelled frames whose selected choice id is in
  that frame's accept-list. Higher is better.
- **Secondary metrics:** acceptable mass (sum of the returned distribution over acceptable ids; `null`
  when the provider returns no distribution, which is not zero), p50/p95 latency, billed cost per 100
  decisions.
- **Direction and stop:** a model comparison picks the higher top-1 only when the paired per-frame
  difference is larger than the measured noise; otherwise it reports `inconclusive`. There is no
  threshold that converts a metric into an action: the bench reports, a person chooses.

## Corpus

- **Source:** one recording run on FPSKit `25e43fb`, Unity `6000.5.4f1`, bridge and core versions
  recorded with the corpus.
- **Recording policy:** a scripted sequence, not a model. The recorder drives the eight calibrated
  controls for 1.2 s each in a fixed order and observes before and after each hold. The pre-hold
  observation is the decision frame; the post-hold observation is retained as measurement evidence,
  not counted as a second frame. A scripted policy is allowed here because the bench measures S1's
  answers on fixed inputs; no model chooses the corpus.
- **Clock and execution:** each decision is recorded in unpaused real time at time scale 1. The
  recorder requests a 1.2-second controller lease, requires a measured positive hold and a verified
  release, and stops on errors. Actual holds and their range are recorded: an overrun is not
  silently reported as 1.2 seconds. A missing duration is not a valid hold. Replaying the same
  provisional choices in a live loop would require the same lease setting; the choice's
  `duration_game_s` parameter alone does not configure the bridge lease. An initial recording was
  stopped by an earlier ±0.15-second cutoff after a 1.450-second hold. Before any labels or model
  calls, this rule was changed to retain and report measured durations rather than discard such
  observations; the incomplete initial recording stays local.
- **Frame contents:** the payload `build_context` produces — objective, `as_of` with age, observation
  (scene, playing, paused, time scale, `public`, `ui`, `reader_errors`), measured facts, the open
  directive and its history, `pending_help`, the choice set and the optional game view.
- **Choices:** the eight calibrated controls as actions with their measured effect, plus `wait` and
  `finish`. They are the manifest's choices once preflight stages 4–6 exist (#27); until then they are
  built from the calibration record, which is the same measured data the manifest will carry.
- **Facts:** measured values only, for example "the previous hold moved the entity 0.0 m; the measured
  effect is about 6 m". A missing measurement is `null`, never zero.
- **Size:** 40 frames over five passes of the scripted sequence.
- **Task for each pass:** check each offered control once, then finish. A check is a completed hold
  with verified release; the facts available at each decision record the completed checks and their
  measured effects. The five passes are separate checklists within one recording, not independent
  game sessions. The calibrated choices are a provisional substitute for a preflight manifest and
  their provenance is recorded as such.
- **Freezing:** the recording records the game commit and changed-file hashes, Unity version,
  repository commit, source hashes, prompt hash and date. The replay plan binds both the recording
  and label file by hash. A changed payload or unresolved control stops the run instead of being
  silently repaired.
- **Images:** if capture works, the frame image stays under `runs/` (it is game pixels) and the frame
  carries its hash and path. Text-only arms are scored separately from image arms.

## Labels

- Each labelled frame stores `acceptable` (choice ids), `labeller`, `date` and `rationale`. The
  first pass is an assistant draft, not a human label; its review status is stored with the labels.
- **Rule:** a choice is acceptable when it advances the frame's stated objective without violating a
  declared precondition. `wait` is acceptable only when no action can produce a new observation (for
  example while an input is still held). `finish` is not acceptable before the objective's checks are
  complete.
- The user plans to review the draft later. A second pass of at least 10% was originally planned;
  separate annotations and disagreements are retained, not averaged away. Until human review,
  agreement and labelling uncertainty remain unknown.
- The draft labels live in `benchmarks/fpskit/labels.json`; the complete recorded frames stay under
  `runs/bench/issue24-fpskit-03/frames.json`. The review sheet's `contexts.json` presents the same
  decision payloads, so a label can be reviewed without re-recording.

## Noise

Before the sweep, each arm sends the same ten frames five times. Report the top-choice flip rate and
the acceptable-mass range per arm. With the prototype's measured 6–33% flips, 40 frames cannot resolve
a small difference; if the noise is wider than the expected effect, add frames or declare
`inconclusive` rather than reporting a winner.

## Arms

- `mammoth-1` as the control (Baseten, free during the trial week), `elephant-1`, and `liquid-d1.1`.
- The same corpus, prompt, schema and transport for every arm. A prompt comparison changes the prompt
  and nothing else; a model comparison holds the prompt fixed.
- An arm that cannot take an image runs text-only and is compared only with other text-only arms.

## Budget envelope

| Item | Calls | Cost |
|---|---:|---:|
| Sweep: 40 frames × 3 arms | 120 | Baseten $0 during the trial; Liquid about $0.01 |
| Noise: 10 frames × 5 repeats × 3 arms | 150 | about $0.01 |
| Retries and one pilot pass | 50 | about $0.01 |
| **Total** | **320** | **under $0.05 at current prices** |

`docs/budget.md` prices; the user directed the replay to proceed despite the unavailable balance
check and unverified Liquid d1.1 quote. The run records these assumptions. The sweep stops at the
envelope, writes each call as it arrives, caches by canonical payload hash, and a billing or auth
failure stops that arm only.

## Scoring and reporting

- Paired per frame: wins, losses and ties against the control, with the frame ids that flipped.
- Report attempted, succeeded and billed per arm. Under the primary metric an API failure is `unknown`,
  not a loss.
- Report top-1, acceptable mass, latency and cost with the frame count and the number of labelled
  frames. No claim from a single pass and no unpaired arm comparison.

## Limits

- 40 frames cannot resolve differences smaller than the flip rate; the noise measurement decides.
- Scripted frames are not live play. This bench measures control-check decisions, not bug discovery,
  and says nothing about the turn loop, adherence or findings.
- One game, one checkout, one machine. The objective and labels encode a task, so a result generalises
  only to that task.
- If the recording run cannot produce images, image support is untested and stays `unknown`.

## Running the bench

The opt-in recorder runs the first enabled scene with a bridge action catalog derived from the
calibration record. Give it the stage 2 and calibration records from the same game checkout, and
wire that checkout to this repository's bridge first:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/unity/record-s1-bench.ps1 `
  -ProjectPath "<FPSKit checkout>" -EntityPath "<stage-2 entity.json>" `
  -CalibrationPath "<calibration record.json>" -OutputDirectory "<new local run directory>"
```

The raw `frames.json`, Unity log and results stay local under `runs/`. The recorder refuses a
changed action descriptor or entity, and writes partial evidence on a failed attempt. Only a
complete recording with verified final release is eligible for replay. Missing images stay `null`.

Create a blank review sheet; this command does not choose or propose labels:

```powershell
uv run python -m playtest.bench.labels --corpus "<run>/frames.json" --directory "<new review directory>"
```

A labeller reads `contexts.json` and fills each row in `labels.json` with the acceptable ids,
labeller, ISO date and rationale. The existing assistant draft is in `benchmarks/fpskit/labels.json`;
both it and the blank sheet are bound to the local corpus and each payload by SHA-256. Add separate
rows for the 10% second pass. Disagreement is reported and the affected frame is unscored, not
automatically resolved. Blank, stale, incomplete or invalid labels stop replay.

The paid runner accepts a separate approval JSON naming the approving person and time, global
call and cost caps, and for each arm its call limit, conservative per-call cost bound, total cost
cap, verified balance and balance-check time. Arms sharing a provider share its balance. Keep one
pricing validity deadline per arm; an expired quote stops that arm before another call. Keep one
run directory for the full approved envelope: its plan pins the corpus, labels, approval, replay
code and seed. On the user's explicit instruction, an approval can record an unverified balance
override; an unverified price estimate must include its basis and a short use-by time. Neither is
reported as a verified balance or a provider quote.
Run the `noise` phase first, then `sweep`, using `python -m playtest.bench.replay run`; use its
`score` subcommand to report with the control arm. No call is made by creating the sheet or
scoring. The runner reserves one bound and writes one file before each call; a crash while a call
is in flight requires reconciliation, never an automatic retry. Transport, auth, billing and
server failures stop that arm. Repeats use separate call slots even when their request bytes are
identical. Billed cost remains `null` unless it is actually established; reservation is not a bill.

To score a later human-reviewed revision without new model calls, keep the frozen draft intact,
review a separate copy, and set its `authorship.human_review` to `completed`, `reviewer` to the
reviewer's name, and `reviewed_at` to an ISO date. Supply the revised file as `--labels` and the
original as `--frozen-labels` to `score`. The scorer checks both against the recorded corpus and
the original against the run plan; its output identifies the result as a post-hoc review and records
both label hashes and the reviewer. Preserve the original report alongside the revised report;
review after seeing model outputs can introduce bias.

The scorer shows paired wins, losses and ties, unknowns, noise flips, mass ranges and per-pass
differences. It does not claim a sampling confidence interval: the frames and passes share one
recording, so independence is not established. It does not name a winner automatically; a person
interprets the variation and noise.
The corpus is a control-check task on one game, and the provisional catalog is not the finished
preflight manifest. Synthetic tests validate the machinery only and say nothing about S1 quality;
results against unreviewed assistant-drafted labels are preliminary. The user approved the low-cost
envelope and explicitly directed the run despite the unverified balances and Liquid d1.1 price.

## S1 ownership and payload fidelity

Each stored frame contains the original bridge observation, its sequence-bound choice set and
facts; replay reconstructs the payload with `playtest.core.context.build_context` and requires its
SHA-256 to match the recording. Facts about earlier holds cite the inspection sequences and measured
durations. The live directive, directive history and pending help are empty for this scripted
control-check task; no image was captured. Unknown observation age and sample gap remain `null`.

The recorder's scripted input is a corpus-collection procedure, not an S1 turn. Replay records the
provider's selected id; an invalid or unoffered id is unknown for scoring and is never repaired from
the returned probabilities. Replay sends no game input and never escalates, chooses a fallback or
substitutes an action. Consequently, this corpus does not establish live-loop adherence.
