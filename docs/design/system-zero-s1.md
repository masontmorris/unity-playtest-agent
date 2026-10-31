# System Zero as S1 (experimental)

Branch `feat/system-zero-s1`, 31 October 2026. **Status: G0–G2 and a format probe have run. As S1's chooser,
System Zero failed: 7/40 and 19/45 against d1-ultra's 40/40 and 45/45 on identical requests
([G2](../probes/2026-10-31-system-zero-s1-bench.md)). No request format tested rescued it, including a
stream-shaped log ([format probe](../probes/2026-10-31-system-zero-format.md)). Asked narrow questions about
the same state, it was 240/240 right.** It was fast, almost always exactly repeatable, and accepted the
game-view image. S1's default stays `liquid-d1-ultra` ([s1-model.md](s1-model.md)). See "What the gates say"
below for the revised plan.

## What System Zero is

From RightNow's docs (<https://www.rightnowai.co/docs/system-zero>, pages dated 24–29 October) and the
Apache-2.0 Python SDK 0.4.2, read but not installed:

- **A decision API, not a chat model.** You send a `state` (text, or a JSON object or array) and named
  questions of type `boolean`, `choice` or `score`. Each answer comes back under its question's name with
  the decision, its probability, and every alternative's probability. `calibrated` is `false` in this release.
  The request is the System One shape our other S1 arms already use: `model`, `state`, `questions`.
- **Many questions in one call, each billed on its own.** Up to 1,024 questions per request, at $0.00013 per
  answered question. Each question can be refused on its own: `question_not_grounded` ("cannot be answered
  from this state") and `injection_suspected` among others. A refused question isn't charged, and an HTTP 200
  can carry a refusal next to other answers.
- **Narrow questions.** A `choice` takes 2–10 options in a text request and 2–26 when the request carries an
  attachment. A `score` takes 2–4 levels in a text request and 2–10 with an attachment. A question may run
  to 4,096 tokens or 32,768 bytes. `GET /v1/models` served exactly these values on 31 October (G0), matching
  the SDK's generated `OrdinaryRequest` and `MediaRequest` types.
- **Inputs.** Up to 4 images at $0.011 each. Video is in preview: one silent H.264 MP4 of at most 10 s with no
  other file, sampled at 2 frames a second (at most 20 frames), at $0.018 per clip. Also one 16 kHz WAV and
  PDF documents.
- **A stream.** A WebSocket at `/v1/system-zero/stream` holds a state that you change with `append_text`,
  `append_items`, `append_attachments` and `replace`. Decisions arrive whenever the state changes
  (`on_change`) or when you ask (`on_request`). Each decision carries the `state_version` it covered and the
  `covered_event_ids` it newly took in. Only one decision is in flight at a time, with one pending, and rapid
  changes merge into the latest state. **The stream carries state continuously, but every decision is still
  a separate, billed decide.** Limits: questions are fixed for the life of the connection; 5 mutations a
  second (burst 10); one stream socket per account across all machines; a 1-hour lifetime and a 120-second
  idle timeout. The docs also list "account decision ceiling: 60 starts per minute" (burst 5) without saying
  whether "starts" means decisions or new streams. The stream's inline attachments are image or audio only,
  so video goes over HTTP.
- **Limits and price.** A standard context holds 38,000 state tokens (40,960 rendered). A key allows
  600 requests a minute and 16 calls in flight. Price card G (27 October): $0.19 per million uncached
  input tokens, $0.019 cached, $0.00013 per answered question, $0.000055 per answered call, and no
  output charge.
- **Version (settled by G0).** The integration skill names `system-zero-1.0.0`; the Models page names
  `system-zero-2.0.0`. `GET /v1/models` lists the alias `system-zero` as `alias_for: system-zero-2.0.0`, with
  `released_at` and `calibration_versions` null. The arm pins `system-zero-2.0.0`.
- **Held features (G0).** `long_context`, `long_recording`, `committee` and `deliberate`. Video isn't held.
  Limits: 4 images, 1 audio file, 16 attachments, a 3,665,920-byte body and 2,621,440 inline bytes.
- **Balance (G0).** $5.00 of starting credit (`GET /v1/credits`, 04:54 UTC on 31 October).

## The idea

Today's S1 takes one snapshot and returns one choice. That has three consequences:

1. **Latency is dead time.** The game keeps running while S1 decides, and the loop rejects an answer that is
   too old.
2. **One channel.** S1 can express itself only through the single choice that gets executed.
3. **A still.** S1 sees readers and at most one JPEG. A bug that shows up only over time (passing through
   a wall, a jump, a flicker) isn't visible in one call.

System Zero differs on each of those axes, and adds a fourth difference in shape:

| Axis | Today | System Zero |
|---|---|---|
| Time | Snapshot in, choice out | A held state; decisions stamped with what they covered |
| Breadth | One question | Many narrow questions in one call, each with its own refusal |
| Motion | One frame | A 10 s clip at 2 frames a second for $0.018 |
| Shape | One wide question (22 choices) | Narrow questions (10 options, 26 with an attachment) |

**Principle: keep the actuator exactly as narrow as it is, and widen everything around it.** Exactly one
choice that S1 selected is executed, bound to the one observation its choice set was built from. Time,
breadth and motion change what S1 sees and when it is asked. They never change what gets executed.

### 1. Shape: a layout S1 answers in one call (built)

FPSKit sessions offer 22 choices: 14 actions, 4 skills and the 4 core choices. A text request can't ask a
22-option question. Two predeclared ways fit the served limit, and `system_zero.layout` applies whichever
the request allows:

- **Flat, when the choices fit.** With a game-view image attached, the limit is 26, so 22 choices go out as
  one question: exactly the shape the other arms send. Every session since #157 has sent a frame on every
  call (b04 sessions 2–10 and the frontier difference trial each sent 108–200 frames, none stale). So this is
  the normal case, and the cleanest comparison with d1-ultra: same inputs, same question, only the model
  differs.
- **Two levels in the same call, when they don't fit.** This covers a turn with no fresh frame, the
  text-only bench corpora (12 choices), and any manifest over 26. The root question names a group, or a
  choice that stands alone. Each group of two or more gets its own question. Groups are runs of one family
  (actions, skills, core) in the context's order, split into balanced chunks. One fixed rule decodes the
  answer: the root names the choice, or it names a group whose own answer names the choice. Every other
  group's answer is S1's contingent plan, recorded and never used. The composed distribution,
  P(group) × P(choice | group), is recorded only.

Why this keeps S1 in charge: the grouping is structural (family and manifest order, no content), every choice
appears exactly once, and the decoder is a lookup. A refused or invalid root answer, or a refused or invalid
answer in the selected group, rejects the turn with no input sent. That is the existing invalid-answer
branch, with nothing repaired.

The layout costs extra questions: 5 instead of 1 for FPSKit in text, $0.00052 more per call. It is also a
change the model sees, so a comparison must name it (see the gates).

### 2. Breadth: S1 says more than its action (designed)

- **Contingent answers.** `while_pending` is already S1's own answer for what happens to input while help is
  pending. Over HTTP it stays a second call on help turns, unchanged. On the stream, where questions are
  fixed for the connection, it is asked in the same call every turn: its wording says "if you request help
  this turn", and the loop uses it under the existing rule (real-time help turns only).
- **Appraisals, recorded only.** Name-blind questions about the state, such as whether the newest observation
  contradicts the measured facts or expected effects. A `question_not_grounded` refusal makes the answer
  `unknown`, never false. No code reads an appraisal to branch, open help or promote a finding.
  `decision_memory` deliberately leaves provider output out of S1's context today, so stating an appraisal
  back to S1 would be a context change: it needs a flag, the ownership audit and a paired comparison.

### 3. Time: the stream (designed; after G2)

**P2: the stream as a transport, with the loop unchanged.** A `SystemZeroStreamS1` port opens one stream per
session (`on_request` mode, `prompt_cache_key` set to the run id). Each turn it sends `replace` with that
turn's context (attachments: the fresh frame, or `[]`; a stale frame is never retained), reads the `ack`
for version *v*, reserves the ledger row, sends `decide(v)`, and decodes the `decision` for *v*. The trace
keeps `state_version` and `covered_event_ids`. A goodbye or a dropped socket fails that turn with its outcome
recorded as unknown. The next turn starts a fresh stream with its own full context: new work, never a replay.
Pings are answered from a reader task. Because the questions are fixed and a stale-frame turn must go
text-only, the stream always uses the text layout.

What this buys: one persistent socket (the HTTP sender opens a connection per call), state held on the
server, and exact coverage stamps. The context is identical, so HTTP against stream is a clean transport
comparison.

**P4: deciding during the hold.** Open condition 2 of the 27 October S1 decision already named "deciding
during the hold" as a remedy for latency. `on_change` is that: an observation pump `replace`s each
observation (at most 5 a second), and the server decides on the latest state while the previous action
is still executing. Each decision maps through its `state_version` to the observation it covered. The
choice set was built from that observation, and the bridge's `based_on_seq`/`max_age_s` gate rejects a stale
one exactly as today. A decision overtaken by a newer one before it executes is recorded as rejected
(superseded), with no substitute. This is a new loop driver, so it needs the ownership path table before any
code. **It increases decisions per minute and responsiveness. It doesn't make a slow answer fresh:** an
answer's age at execution is still roughly the model's latency.

### 4. Motion: a witness, kept off the actuation path (designed)

Every 10 s of play, tiled with no gaps, one HTTP decide sends the clip with a short text state: the seq
range, the actions executed in the window, and the manifest's rule statements. The clip comes from the
bridge frame window that `frontier.media` and `session_video` already encode, at 640×360, under the 2.5 MiB
decoded limit. It asks name-blind questions:

- `anomaly` (boolean): does the clip show something a player would report as a bug?
- `when` (choice): which 2-second segment, or none.
- `effect_visible` (choice): yes, no or unclear.

Answers go to the trace as `witness` entries carrying the clip's SHA-256 and seq range. They are evidence
for review and for the reproduce role. They never open help, never rank, and never promote a finding. Stating
them to S1 as its own labelled appraisals (hypotheses kept apart from measured facts) is a separate flagged
experiment, under the same conditions as appraisals above. Cost: 60 clips per 10 minutes at about $0.019,
roughly $1.15 a session. Video is in preview and isn't available on the stream.

## Gates

Each gate needs the key in `.env`. Every paid gate also needs the budget approved and the balance checked
first (`playtest-eval-design`).

| Gate | What it settles | Calls | Cost |
|---|---|---|---|
| G0 (**done**) | `GET /v1/models`: served `choice_options` and `score_levels`, the pinned version, `held_routes` | 2 reads | free |
| G1 (**passed**) | The game-view image as an `attachments` entry is accepted (then add the arm to `IMAGE_TRANSPORT`) | 2 | $0.09 for G1 and G2 together, measured |
| G2 (**failed**) | Paired replay of the frozen Day 3 corpora (40 + 45 frames, plus 10 frames × 5 for noise): top-1 acceptable, acceptable mass, flips, p50/p95 latency, refusals, cached tokens | 140 | (included above) |
| G3 (not run) | One live FPSKit session with game view (flat 22): call latency, stale rejections, refusal and `injection_suspected` rates, cost | ~200 S1 | ~$2.50 + frontier |

The Day 3 corpora offer 10 choices, which fits the text limit, so G2 sent each frame's recorded request with
only the model name changed: a pure model comparison. Running it needed a bench fix. #157 added a sentence to
S1's instructions, so today's builder no longer reproduced the Day 3 payloads, and the bench had refused every
arm since. `record.recorded_payload` now retries with the instructions in force at recording, and uses that
only when it reproduces the recorded hash exactly.

### What the gates say

| Strength the design bet on | Measured |
|---|---|
| Fast | p50 ~300 ms, p95 343–437 ms over 140 text calls: inside the live age limit |
| Repeatable | identical distributions on repeated requests, 5/5 |
| Cheap per call | $0.09 for 142 calls |
| Takes the frame | `media_items: 1` on the JPEG call |
| **Chooses the next step of a task** | **no**: premature `finish` on 22 of 26 re-test misses; 7/40 on the first bench |

System Zero answers fast and the same way every time, but on this format it doesn't track the state of a
task: it doesn't subtract the controls already checked from the ones offered. That points to the other half
of the design. System Zero belongs in a perception role (appraisals, and the video witness), feeding facts to
an S1 that plans, rather than as the S1 that executes. That fits its name: a layer below System One.

**The format probe settled it** ([record](../probes/2026-10-31-system-zero-format.md), $0.18):

- With the task in its instructions, System Zero scored 32/45; with the rule spelled out too, 44/45 but
  36/40. Neither meets the bar.
- The stream-shaped log scored 14/45, worse than the snapshot.
- Asked narrow questions, it read which controls were done with 240/240 accuracy.

It perceives precisely and composes poorly. That is the shape of its API, many narrow questions, and the
opposite of the one wide decision S1 makes.

Revised order:

1. ~~Format test.~~ Done: not viable as the chooser in any of the four formats tested.
2. ~~**Witness (P3) on d1-ultra sessions.**~~ The offline check ran and failed: System Zero's video answers
   didn't register motion ([record](../probes/2026-10-31-system-zero-witness.md)).
3. The stream (P2) and deciding during the hold (P4) matter only if System Zero becomes the chooser.

**Latency decided the original plan.** Live play needs p90 under about 0.5 s (the loop's age limit is 0.5 s by default,
and 0.8 s was used live). d1-ultra's live p90 was 391 ms. The docs promise nothing: the example reply
shows `decide_ms: 40000`, the SDK allows 150 s per attempt, and `usage` has a `thought_tokens` field. If G2
shows seconds, real-time S1 on System Zero needs the stepped clock. P4 wouldn't rescue it.

## Cost

| Per call (FPSKit, ~5,000 tokens) | System Zero | d1-ultra |
|---|---:|---:|
| Text-only, two-level layout (5 questions) | ~$0.0017 | ~$0.0002 |
| With game view, flat (1 question + one $0.011 image) | ~$0.012 | ~$0.0002 |
| **Per 10-minute session (~200 S1 calls, game view)** | **~$2.40** | **~$0.05** |
| Witness (60 clips) | +~$1.15 | — |

Caching could cut the text part to a tenth ($0.019 against $0.19 per million). Whether anything is reused
depends on how System Zero renders state and questions, which isn't published; G2 and G3 read
`cached_input_tokens`. The image dominates in any case.

## Risks

- **Latency**, as above.
- **Refusals on game text.** `injection_suspected` on UI strings would reject turns. G3 measures the rate.
- **Headroom.** 22 of 26 options leaves little room; the layout covers anything larger.
- **Layout size.** A root option lists its group's ids, and each label appears in its group's question and in
  `state.choices`. Each question is bounded at 4,096 tokens and 32,768 bytes (G0).
- **One stream socket per account.** No two sessions or benches can use the stream at once. That matches
  the one-Editor rule, but a bench can't run beside a session.
- **Data terms are unverified.** The context and frames go to RightNow; the privacy policy's API section
  hasn't been read.
- **Preview features** (video) and an ambiguous stream ceiling may change.

## Built on this branch

- `src/playtest/bench/system_zero.py`: the transport (one POST, no retry, the error envelope kept), the
  layout and the fixed decoder.
- `system-zero` in `replay.ARMS` (provider `rightnow`, key `SYSTEM_ZERO_API_KEY`, pinned
  `system-zero-2.0.0`). `send_systemone`, the replay and retest benches, and the live adapter
  (`runner/s1.py`) route to it. The image goes as an `attachments` entry, in `IMAGE_TRANSPORT` since G1.
- Pacing at the provisional 280,000 tokens a minute; the launcher's credential map.
- The bench fix: `record.recorded_payload` rebuilds a frozen frame byte for byte, and the replay, re-test
  and label tools use it.
- `tests/test_system_zero.py`: the layout bounds, the decoder against repair and substitution, refusals,
  the transport, the adapter, the bench hooks and the byte-exact rebuild.

### S1 ownership audit of the built slice

The verdict is **pass, offline only**. No real trace exists, so live verification is not applicable. Path
table for the arm, with the core loop unchanged:

| Branch | Effective choice | Executed | Rule |
|---|---|---|---|
| Flat answer names an offered id | that id | that id | Only S1's choice executes |
| Root names a lone choice | that id | that id | Fixed decode; no ranking |
| Root names a group; the group answer is in it | the group answer | that id | Fixed decode; other groups unused |
| Root refused or invalid | none | nothing; turn rejected | No repair |
| Selected group refused or invalid | none | nothing; turn rejected | No repair |
| Unselected group refused | the decoded choice | that id | Its distribution is dropped, not its choice |
| Layout can't fit | none; no call made | nothing; turn rejected | Fail closed |
| Help turn | `while_pending` decoded by the same rule | per `parse_decision` | Existing pending rule |

Distributions stay record-only (`parse_decision` never reads them). Nothing in `src/playtest/core/` changed.

## Next

1. ~~Add `SYSTEM_ZERO_API_KEY` to the main checkout's `.env`, then run G0 (free).~~ Done on 31 October.
2. ~~Approve G1 and G2.~~ Done on 31 October: G1 passed, G2 failed ($0.09).
3. ~~The format test.~~ Done on 31 October ($0.18): not viable as the chooser; reads perfectly.
4. ~~The witness.~~ Run on 31 October ($0.56): not usable. P(problem) was 0.41–0.56 on every window, and a
   perception diagnostic caught 0 of 8 visible motions ([record](../probes/2026-10-31-system-zero-witness.md)).

**Where this leaves System Zero (31 October).** It isn't viable as S1's chooser, and its video doesn't register
motion. What it does well is narrow questions over text state (240/240), fast and repeatably. That isn't a gap
S1 has today: d1-ultra reads those facts itself (40/40). The experiment stops here unless a new model version
ships or a narrow-question role turns up that S1 can't already cover. Total spent: $0.83 of $5.00 credit.
