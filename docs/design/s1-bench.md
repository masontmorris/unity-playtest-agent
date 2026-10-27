# The S1 bench: FPSKit corpus, labels and scoring

Issue #24. The bench exists to compare prompts, contexts and S1 models on frozen inputs before paying
for live runs. It is not a probe: a probe measures capability, latency and cost on invented packets,
which is what `scripts/probes/` does. The bench measures choice quality on frames recorded from the
game, against labels a person wrote.

This note predeclares the experiment (the eval-design skill's binding rule) and records what is built
so far. The corpus and its labels do not exist yet; nothing in this note is a result.

## Decision

- Frames come from **FPSKit**, recorded through the bridge from the same checkout the preflight runs
  use. No frame, label or body is taken from the prototype.
- A frame is replayed through the production context builder (`playtest.core.context.build_context`),
  so the bench measures the payload S1 will actually receive, not a copy of it.
- Labels are hand-written and committed with the labeller, date and rationale. No model writes labels.
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
  controls for 1.2 s each in a fixed order, observes before and after each hold, and writes one frame
  per observation. A scripted policy is allowed here because the bench measures S1's answers on fixed
  inputs; no model chooses the corpus.
- **Frame contents:** the payload `build_context` produces — objective, `as_of` with age, observation
  (scene, playing, paused, time scale, `public`, `ui`, `reader_errors`), measured facts, the open
  directive and its history, `pending_help`, the choice set and the optional game view.
- **Choices:** the eight calibrated controls as actions with their measured effect, plus `wait` and
  `finish`. They are the manifest's choices once preflight stages 4–6 exist (#27); until then they are
  built from the calibration record, which is the same measured data the manifest will carry.
- **Facts:** measured values only, for example "the previous hold moved the entity 0.0 m; the measured
  effect is about 6 m". A missing measurement is `null`, never zero.
- **Size:** 40 frames over five passes of the scripted sequence.
- **Freezing:** the corpus file records the game commit, Unity version, bridge and core commit, the
  prompt hash, the label file hash and the date. A frame that cannot resolve its descriptor at replay
  time is skipped and counted, not silently repaired.
- **Images:** if capture works, the frame image stays under `runs/` (it is game pixels) and the frame
  carries its hash and path. Text-only arms are scored separately from image arms.

## Labels

- Each labelled frame stores `acceptable` (choice ids), `labeller`, `date` and `rationale`.
- **Rule:** a choice is acceptable when it advances the frame's stated objective without violating a
  declared precondition. `wait` is acceptable only when no action can produce a new observation (for
  example while an input is still held). `finish` is not acceptable before the objective's checks are
  complete.
- A second pass rechecks 10% of the frames. Disagreements are kept as two rows with both labellers;
  they are reported as disagreement, not averaged away.
- The labels live in `benchmarks/fpskit/labels.json`. The recording script also writes an unlabelled
  frame file so a label can be added without re-recording.

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

## Budget envelope (needs approval before #25)

| Item | Calls | Cost |
|---|---:|---:|
| Sweep: 40 frames × 3 arms | 120 | Baseten $0 during the trial; Liquid about $0.01 |
| Noise: 10 frames × 5 repeats × 3 arms | 150 | about $0.01 |
| Retries and one pilot pass | 50 | about $0.01 |
| **Total** | **320** | **under $0.05 at current prices** |

`docs/budget.md` prices; a balance check comes first. The sweep stops at the envelope, writes each
call as it arrives, caches by canonical payload hash, and a billing or auth failure stops that arm
only.

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

## What lands when

- **This PR:** the production context payload and this predeclaration. No corpus, no calls.
- **Next:** the recorder, the runner and 40 labelled frames. The recorder is a PlayMode test in the
  bridge or a scripted bridge client; the runner replays through `build_context`, caches and scores.
- **Then #25:** the noise floor and the bake-off, after the envelope above is approved.
