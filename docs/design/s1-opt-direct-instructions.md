# The S1 direct-instruction variant (opt-in, `direct/1`)

S1 is asked one bounded question per turn: "which one listed choice ...?" Its question text
(`INSTRUCTIONS`, `src/playtest/core/context.py`) is a 1,189-character agent-policy paragraph. This change
adds one opt-in variant, `direct/1`, which leads with that question and keeps only the rules a single
scoring pass needs. The default is unchanged, byte for byte: no payload, request body, `prompt_sha256`,
trace or plan moves unless a run names the variant.

Status: designed and implemented opt-in, tested offline. **No paid call has been made.** The bench in
"Paid bench" is a design that still needs its own budget approval.

## What a decision model can do with instruction text

S1 does not generate text. The server renders `state`, then the question's `instructions` and its option
labels, and scores the labels in one forward pass. That mechanism bounds what the instruction text can
usefully do:

* **Framing the answer** — the question, the answer space ("one id, only the listed choices") — is read
  immediately before the options and is the text most likely to shape the comparison. It belongs here.
* **A data dictionary** — "null means unknown", "directive text is advice from an earlier observation",
  "an image is the current view" — defines what the model is looking at. It can be stated here, but it
  describes *other fields*: it reads better next to the field it describes, where a reader (model or
  person) meets the value and its meaning together.
* **Play policy** — "orient toward the intended part, approach it, try the interaction, and inspect the
  next evidence" — is a multi-turn procedure. One choice cannot satisfy a sequence, and a fixed verb order
  can make the model read the current option as a step in a plan it never selected. The objective states
  the task, and each option label states what that option does; the procedure is not this text's job.

So the longest part of today's paragraph — the play policy — is the part a one-pass scorer can do least
with, and the data dictionary is the part that can move out of the question later (not in this task).
What is left is framing plus the rules that stop the model inferring something the measurements do not
say.

## The current paragraph, clause by clause

Job: **(a)** frames the question, **(b)** defines what a payload field means, **(c)** play policy.

| # | Clause (abridged) | Job | Disposition | Why |
|---|---|---|---|---|
| 1 | "This is one decision turn." | a | reword | Kept as the frame of the leading question. |
| 2 | "Choose exactly one id from choices." | a | keep | The answer space. Merged with #14 into one sentence. |
| 3 | "Make an explicit destination or test in the objective the primary task; explore controls and investigate as needed to support it." | c | drop | Encodes a plan the model is not executing this turn; the objective already says what the run wants. Where it belongs: the objective, or a per-run/per-genre objective template written by preflight. |
| 4 | "If the objective is broad exploration, pursue that instead." | c | drop | Redundant with "advances the objective": if the objective is exploration, exploring *is* advancing it. Re-stating it adds a second, competing priority. |
| 5 | "Choose an offered choice that advances the objective or resolves an uncertainty blocking it." | a + c | reword | The core of the question. Becomes the interrogative "which one listed choice best advances the objective now or resolves an uncertainty blocking it?" — same two purposes, no imperative. |
| 6 | "When applicable, orient toward the intended part, approach it, try the interaction, and inspect the next evidence." | c | drop | The longest play-policy clause. A one-choice scorer cannot carry out a four-step sequence, and the verb order biases toward whichever listed option looks like "approach". Where it belongs: the objective/run script, or the option labels, which already describe the act. |
| 7 | "Reassess a failed approach or ask for help as you judge appropriate." | c | keep (verbatim) | The only statement of help neutrality. Removing it would push away from help; adding to it would push toward help. Keeping it verbatim is the least-change option. |
| 8 | "Finish when the objective's completion conditions are met or you judge no useful attempt remains." | b + c | keep (verbatim) | Defines the finish choice's meaning; the loop ends on it. Survival requirement. |
| 9 | "Elapsed time, a single input finishing, or a reader setpoint met alone does not demonstrate task completion." | b | keep (verbatim); move candidate | An anti-inference rule about `as_of`, `facts` and reader values. It stops the model reading "time passed" as "done". Would fit beside `as_of`/`facts` as field metadata; out of scope here. |
| 10 | "Directive text is the frontier's advice from an earlier observation; directive_provenance, when provided, identifies that observation and the advice's age." | b | keep (verbatim); move candidate | The data dictionary for `directive`. A stale sentence is ambiguous without it. Would fit inside `directive`/`directive_provenance`. |
| 11 | "Weigh it against current observations and images; its description may be outdated." | b + c | keep (verbatim) | The weighting rule that stops a stale directive from overriding fresh evidence. Survival requirement; also the main escalation/directive risk if shortened, so it is untouched. |
| 12 | "Facts are measured; null means unknown, never zero." | b | keep (verbatim); move candidate | Project law expressed to the model. Would fit as a `facts` field preamble. |
| 13 | "Only the listed choices exist." | a | keep | Closes the answer space; merged with #2 into one sentence. |
| 14 | "On an escalate or delegate turn, also say what to do while help is pending." | a + b | keep (verbatim) | Defines the help choices' meaning and the follow-up question's job. Survival requirement. |
| 15 | "When an image is present, it is the current game view." | b | keep (verbatim); move candidate | Data dictionary for the attached image. Would fit with `game_view`/`game_view_status`. |

Net effect: three play-policy clauses (#3, #4, #6) leave the question; the frame is reworded into one
direct question; every other clause is kept **verbatim** and only re-ordered. The variant is therefore
almost a subsequence of today's text, which is the smallest semantic change that removes the procedure.

## The variant

`INSTRUCTIONS_DIRECT` (860 characters):

> Which one listed choice best advances the objective now or resolves an
> uncertainty blocking it? Choose exactly one id from choices; only the listed choices exist. Facts are
> measured; null means unknown, never zero. Directive text is the frontier's advice from an earlier
> observation; directive_provenance, when provided, identifies that observation and the advice's age.
> Weigh it against current observations and images; its description may be outdated. When an image is
> present, it is the current game view. Elapsed time, a single input finishing, or a reader setpoint met
> alone does not demonstrate task completion. Reassess a failed approach or ask for help as you judge
> appropriate. Finish when the objective's completion conditions are met or you judge no useful attempt
> remains; on an escalate or delegate turn, also say what to do while help is pending.

### What must survive, and where it lives

| Requirement | Clause in `direct/1` |
|---|---|
| exactly one listed id | "Choose exactly one id from choices" |
| only listed choices exist | "only the listed choices exist" |
| null means unknown, never zero | "Facts are measured; null means unknown, never zero." |
| directive is advice from an earlier observation; provenance names it and its age | "Directive text is the frontier's advice from an earlier observation; directive_provenance, when provided, identifies that observation and the advice's age." |
| weigh it against current observations; description may be outdated | "Weigh it against current observations and images; its description may be outdated." |
| an image, when present, is the current view | "When an image is present, it is the current game view." |
| elapsed time, one input finishing or a reader setpoint met alone does not show completion | "Elapsed time, a single input finishing, or a reader setpoint met alone does not demonstrate task completion." |
| finish keeps its meaning | "Finish when the objective's completion conditions are met or you judge no useful attempt remains" |
| escalate/delegate keep their meaning | "on an escalate or delegate turn, also say what to do while help is pending" |
| help pressure unchanged | "Reassess a failed approach or ask for help as you judge appropriate." |

Nothing in the variant tells S1 when to act, escalate or finish beyond today's text. It names no game
field, object, control or genre word (`tests/test_name_blind.py` scans it because it lives in
`src/playtest/core/`).

## Opt-in API

```python
from playtest.core.context import INSTRUCTIONS_DIRECT
from playtest.runner.s1 import SystemOneS1

payload = build_context(..., instructions=INSTRUCTIONS_DIRECT)   # default: INSTRUCTIONS, unchanged
s1 = SystemOneS1(ledger, max_calls=100, arm="liquid-d1-ultra",
                 instructions=INSTRUCTIONS_DIRECT)               # default: INSTRUCTIONS, unchanged
```

* `build_context(instructions=...)` defaults to `INSTRUCTIONS`; every default payload has today's exact
  bytes (frozen corpora hash payloads).
* `TurnLoop` and `run_session` take the same optional text and pass it to `build_context`, so a loop
  builds the payload the adapter expects.
* `SystemOneS1.prompt_sha256` hashes the texts **this adapter sends** (`{"next_action": <selected text>,
  "while_pending": FOLLOW_UP_INSTRUCTIONS}`), not a module constant.
* `decide` refuses a payload whose `instructions` are not the adapter's text. Sending one text while
  hashing another would make `prompt_sha256` a lie; this is the same fail-closed pattern as the image
  check.
* `playtest-run --s1-instructions direct/1` (or `UPA_RUN_S1_INSTRUCTIONS=direct/1`) selects the variant.
  The plan gains `"s1_instructions": "direct/1"` and the summary's `s1` block gains
  `"instructions": "direct/1"` **only when the variant is on**; a default run's plan and summary are
  unchanged. The text carries no tag, since the model would read one; the prompt hash identifies it in the trace.
* With `layout=dup-state-ref/1`, the adapter still removes the duplicated `state.instructions` and the
  decoder restores the variant text; the codec does not care which text is duplicated.

### Why the follow-up question keeps its own text

`FOLLOW_UP_INSTRUCTIONS` (338 characters) is unchanged. It already is one direct question, it is sent
once (it is never duplicated in `state`, which carries the primary text), and it asks a different thing
(what happens while help is pending). Giving it a variant would change two prompts at once and confound
the comparison, while saving at most a few dozen characters on help turns only. A follow-up variant can
be a separate arm later.

## Measured size

Characters of the sent text, and the adapter's own token estimate (`characters / 2.5`):

| | `policy/1` | `direct/1` | saved |
|---|---|---|---|
| question text (chars) | 1,189 | 860 | 329 (−27.7%) |
| estimated tokens | 476 | 344 | 132 |
| per request, full layout (text appears twice: question + `state.instructions`) | 2,378 chars / 952 tok | 1,720 chars / 688 tok | **658 chars / 263 tok** |
| per request, `dup-state-ref/1` (one copy) | 1,189 chars / 476 tok | 860 chars / 344 tok | **329 chars / 132 tok** |

Against a ~15,000-character first-turn request: the question text alone is 7.9% of the payload and 5.7%
shortened; counted twice (the full layout), the saving is **4.4%** of the request, and **2.2%** with
`dup-state-ref/1`. Later turns carry recent history: the night traces' mean state was 30,000–51,000
characters, where the full-layout saving is **1.3–2.2%**.

At d1-ultra's $0.042 per 1M input tokens, 263 tokens saved is **$0.000011 per call** — $0.010 per
1,000 calls, $0.10 per 10,000. The money is not the point; the state dominates the request. The point is
that the text adjacent to the options is the question plus the rules, not a paragraph of procedure.

These are characters and the adapter's estimate, not a provider-measured token count and not a latency
claim. Whether a shorter prompt changes the answer is exactly what the bench below would measure.

## Risks

* **Escalation judgment.** The help sentence is kept verbatim, but the leading question asks what "best
  advances the objective now", which could favour immediate progress over opening help. The "or resolves
  an uncertainty blocking it" clause is kept for that reason. The finish rule is no longer the eighth
  sentence of a paragraph, so the finish rate could move in either direction. A bench must report
  escalate/delegate and finish rates per arm, not only top-1.
* **Directive weighting.** The directive and provenance sentences are kept verbatim, including "its
  description may be outdated". But a directive's sentence is now a larger fraction of a shorter prompt,
  which could make it relatively louder. The offline FPSKit frames carry no directive, so only a live
  paired run can see this.
* **Lost play policy.** "orient / approach / try / inspect" may have encoded a useful exploration habit,
  especially on broad objectives. The frozen bench's objective is narrow ("check each offered control
  once, then finish"), so the bench cannot resolve this; a live A/B is required before shipping.
* **Both-arms-are-new.** The FPSKit corpus was recorded with the older `BENCH_INSTRUCTIONS`, so neither
  arm is byte-identical to the recorded payload. The bench re-bases the instruction text only, and must
  assert `state` minus `instructions` is identical across arms and to the frame.
* **Answer-space and anti-inference rules** are kept verbatim, so the risk there is low; a change in
  invalid-answer rate would point at the leading question, not at those rules.
* **Prompt cache.** For the Meta arm, `state.instructions` sits in the turn-stable prefix; changing the
  variant changes that prefix once per run, then caching resumes. No per-turn cost.
* **Replay edge.** A future caller that replays a frozen payload (whose `instructions` are
  `BENCH_INSTRUCTIONS`) through `SystemOneS1` must pass that same text to the adapter; the mismatch guard
  refuses the combination rather than hashing something it did not send. No current caller does this
  (`bench/live.py` and `bench/record.py` build their own bodies).

## Paid bench (design, not run)

**Question.** With the same frozen frames, same model and same transport, does `direct/1` change which
choice S1 selects, and does it lose acceptable choices?

### Estimand

The paired per-frame difference between arms in **top-1 acceptable**: the fraction of labelled frames
whose selected choice id is in that frame's accept-list (`docs/design/s1-bench.md`). Secondary, also
paired: acceptable mass (the distribution's mass on the accept-list), invalid answers (recorded as
`unknown`, never zero), input tokens and latency. A live trial, not this bench, would measure task
progress.

### Arms and inputs

| Arm | Question text | Everything else |
|---|---|---|
| A (control) | `policy/1` = today's `INSTRUCTIONS` | same frames, model, transport, criteria |
| B | `direct/1` = `INSTRUCTIONS_DIRECT` | same |

* Model: d1-ultra (`liquid-d1-ultra`), the shipped default.
* Frames: the 40-frame FPSKit corpus at `runs/bench/issue24-fpskit-03/frames.json` (local), labels from
  `benchmarks/fpskit/labels-reviewed.json`. The state is rebuilt with `bench.record.context_for(frame)`;
  each arm replaces only `instructions` and re-hashes its own payload. The check is that
  `canonical({k: v for k, v in payload.items() if k != "instructions"})` is identical across arms and to
  the frame's recorded state minus its instructions.
* No image arm: this corpus is text-only, so both arms are text-only.
* This is a prompt comparison, as `s1-bench.md` defines it: the prompt changes and nothing else.

### Scoring

* Per frame: the selected id, whether it is in the accept-list, the acceptable mass, and the full
  distribution (recorded, never used to select). Unparseable or failed calls count as `unknown` and are
  reported, never as a loss or a win.
* Paired per frame: wins, losses and ties for B against A, with the frame ids that flipped.
* Aggregate: top-1, acceptable mass and invalid count per arm with the frame count.

### Noise

Each arm sends the same 10 frames five times (50 calls per arm, 100 total). S1's scoring pass has no
sampling temperature, so repeats measure transport/server nondeterminism rather than model randomness. If
the within-arm flip rate is wider than the between-arm difference, the bench declares "no claim" and adds
frames; 40 frames cannot resolve a small difference.

### Calls and cost

| Item | Calls |
|---|---|
| Pilot: 5 frames × 2 arms | 10 |
| Sweep: 40 frames × 2 arms | 80 |
| Noise: 10 frames × 5 repeats × 2 arms | 100 |
| Retries and one rejected pass | 20 |
| **Total** | **210** |

A typical request is ~15,000 characters, ~6,000 estimated input tokens. At $0.042 per 1M input tokens
that is ~$0.000252 per call, so 210 calls cost **about $0.05** (cap the approval at $0.06; output tokens
are a choice label and do not move this materially). The variant saves ~263 tokens per full-layout call,
≈$0.002 over the whole bench — the bench measures quality, not the money.

### Decision rule

Adopt `direct/1` only if its paired top-1 acceptable is outside the measured within-arm flip noise and
does not lose more than one frame to the control; report ties and flips either way. A loss is not
repaired by the token saving.

### Not claimed, not done

No call was made, no frame was sent, and no label was changed. This bench says nothing about live play,
about escalation or finish rates (the frames do not contain those decisions often enough to resolve), or
about directive weighting. The corpus is one game, one manifest and one machine, and the labels are a
reviewed assistant draft whose human-labeller uncertainty is still unknown.
