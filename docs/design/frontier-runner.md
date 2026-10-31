# Frontier escalation in live runner sessions

`playtest-run` calls the frontier only when S1 chooses `escalate`. Enable the path with `--frontier`
and set `--max-frontier-calls` to a positive approved cap. The same approval file must include the
frontier model's arm and enough overall call and cost capacity for both providers. `META_API_KEY` is
required only when this flag is enabled. Without the flag the runner has no frontier helper.

The runner's `FrontierHelp` is a `HelpPort`. It reads the exact `HelpRequest.context` S1 saw, including
reader values and the measured fact lines, and uses the request's turn and observation sequence as
provenance. It captures the existing short clip and sends one schema-constrained frontier request.
The call is reserved in the shared paid-call ledger before the provider request and settled with its
reported cost after decode. An invalid answer is recorded as such; no hypothesis or directive is made
up. S1's selected `while_pending` choice remains owned by the core loop.

Accepted hypotheses and directives are written as `investigation` trace entries. Directive scoring
runs in a runner facts source against the manifest's reader aliases using the shared `roles.checks`
evaluator. The directive goal, setpoint, deadline and current status are shown to S1 as measured facts.
The background request stages updates; the loop publishes them through `adopt_result` only at its
adoption boundary, after checking the fresh observation's epoch. A completion during observation
capture cannot start that turn's scoring window, and a result from the prior epoch is discarded.
Candidate findings are staged too: only the exact adopted result can publish one. Its citations are
frozen at request time and retain validated execution/effect entries for cited history attempts;
an unavailable or mismatched reference produces an explicit refusal to record the candidate.
Cancellation/invalidation discards staged state. Same-status hypothesis revisions retain their changed
text and evidence; only an identical version is compacted as a restatement. Live packets include the
bounded action/turn history S1 saw, and facts state remaining wall time and call slots.
The tracker records `met`, `not_met`, `expired`, `invalidated` or `unknown` with a `trace_seq` reference
to the observation evidence. Missing readings stay unknown; a deadline with no scoreable reading is
recorded as expired. A session ending before an active directive can be scored records unknown.

## When a directive's test never ran (#133)

**The deadline.** The frontier gives a relative deadline. The tracker counts it from the observation that
first states the directive to S1, which is when S1 received it, as `to_directive` documents. It used to count
from the escalation's own observation. The answer reaches S1 only after the frontier call and the next turn's
pacing, so on the #132 validation run, 11 of 14 deadlines had already passed when S1 first saw its directive.
The relative length is kept, and only its start moves. When either game time is unknown, the deadline stays
as given.

**The window.** When a directive closes, its record carries a `window` (`DirectiveWindow`): what S1's inputs
did from that first statement up to the outcome.
- `turns`;
- inputs sent and completed;
- inputs rejected, by the bridge's reason;
- failed, cancelled, timed-out and unrecorded inputs;
- waits and escalations.

These are counts from the bridge's own terminal records, never a verdict.

**Untested outcomes.** A `not_met` whose window completed no input gets an `error_class`:
- `no_attempt` when no input was sent;
- `action_rejected` when every input that reached the bridge was rejected.

Any outcome whose window completed no input says so in its reason. A `met` reached without S1's input is
labelled the same way, because the setpoint already held.

**Where it shows up.**
- The fact line S1 reads states the window and the error class.
- The next frontier request's directive history carries both, because they are fields of the `Directive`
  contract.
- The trace's `outcome` entry records them.

Offline coverage uses a fake bridge, fake provider response, fake clip capture and scripted S1. It does
not make a live model call or launch a Unity Editor.

## How a setpoint is scored (#141)

On the last two live runs, 4 of 6 and 3 of 5 directives were scored `met` on the turn S1 first saw them, so
most tested nothing. Three causes, three changes.

**`unchanged` is a hold.** Every other operator is met on the first scored reading that satisfies it, and
`changed` is met on the first reading that differs from its baseline. `unchanged` used to be scored the same
way, so it was met on its first reading and a hold could never be tested. Now, from the observation that first
states the directive to S1:
- the first scored reading that differs from the baseline closes it `not_met`, with the reason "the value
  changed before the deadline";
- when the deadline passes with at least one reading scored and none changed, it closes `met`, with the reason
  "the value stayed unchanged through the deadline";
- a reading that cannot be scored is skipped, never counted as held. If none could be scored by the deadline it
  closes `expired`, as any directive does.

The window and `error_class` logic of #133 runs as before in `_close`, so a hold that was missed or met without
a completed input says so.

**Review rejects setpoints that test nothing.** `roles.frontier.review` rejects a directive, with the reason in
the answer's `review` list (which S1 reads in its guidance) and in the investigation entry's `reason`, when
- its setpoint is `unchanged` and it has no `deadline_s`; or
- its setpoint uses any operator but `changed`/`unchanged` and already holds at the request observation: "the
  setpoint already holds at the request observation, so it tests nothing".

The check lives in `review` so that it stays the one place that rejects answer items. `build_packet` copies the
observation's public readings into `PacketRefs.public`, so the provider path and the runner reach it through the
packet they already pass. It judges with `roles.checks.evaluate`, the evaluator the tracker scores with. An
unknown evaluation (a missing, null or wrong-typed reading, or no readings at all) is not "already holds". A
finding's `repro_check` is not judged, because a finding is often true right now. A rejected directive is not
adopted, and the answer's other items are unaffected.

**The baseline is the value when S1 receives the directive.** A `changed`/`unchanged` baseline is taken in the
tracker's first-statement block, next to the deadline anchor in `_anchor_deadline`, from the observation that
first states the directive to S1. It used to be the escalation's observation, which is older by the frontier call
and the next turn's pacing, so a value that moved in between counted as a change S1 never caused. The
escalation's value remains as the fallback when that observation cannot read the field. The tracker item's
`baseline_source` records which was used (`first_statement`, `escalation`, or null when neither could be read,
in which case nothing scores and the directive expires). When the fallback was used the closing reason says so.

**What the frontier and S1 are told.** The frontier's system prompt and the `expected`/`deadline_s` descriptions
in its schema state the three rules above. S1's fact line states a hold as one: "setpoint public.x unchanged
until game_s 12.0". Other setpoints keep the line they had.

**Not covered.** A reach setpoint that does not hold at the request observation can still hold at the
observation that first states the directive, because the game keeps running while the frontier thinks. It is then
met on that turn; its window shows whether S1 sent any input.

## Candidate findings (#38)

An accepted `candidate_finding` is written to the trace as a `finding` entry in state `candidate`, with
its severity and `repro_check`. Its citations are mapped from bridge observation seqs to those
observations' trace entries. If any cited seq has none, nothing is written and the reason is returned.
S1's guidance names the finding id and that reason. `reproduce` replays the finding later; see
`reproduce.md`.

### Effect checks (#154)

A `repro_check` may name `effect.<action>` instead of a reader: the net displacement, in world units, of one
execution of that action. It is for an action that stops having its calibrated effect, which leaves every reader
as it is at rest. A reader check that already holds right after the reset is `unknown` to `reproduce`, and a
stopped action looks like rest, so no reader check could reproduce it.

- **What the frontier is shown.** The manifest message gets an `effects` row for each action with a calibrated
  expected displacement: `path` (`effect.<action>`), `expected` (what calibration measured on this build) and
  `allowance` (how far a measurement may differ before the calibration's recorded spread no longer accounts for
  it; null when calibration recorded no spread). `FrontierHelp` takes them from `build_choices(...).expected` and
  the live probe's allowance (`runner.probe.allowance`), so they are the figures S1's fact lines state. They are
  run-stable: a choice hidden from S1 during a directive is still an action a check may name. A manifest with no
  calibrated action lists none, and its message is worded as it was.
- **Where an effect field is allowed.** The answer schema has a second field enum, `check_fields` (the readers
  plus `effect.<action>` for each effect), used only by a finding's `repro_check`. A directive's setpoint keeps
  the readers-only `fields` enum, and review rejects an effect field there ("not a manifest field").
- **What review checks.** An effect check must name an action with a calibrated expected displacement, use `<`,
  `<=`, `>` or `>=`, a finite number and no tolerance. A violation rejects only the candidate finding, with its
  reason. Review does not judge a check against the calibration: the prompt says the check must not hold for the
  expected displacement, and `reproduce` scores a check that does as `unknown` and says why.
- **The prompt** says that when an action stops having its calibrated effect, the check may name
  `effect.<action>` with an ordering operator and a number, and must not hold for the expected displacement the
  manifest lists.

The code names no game action: the actions and their displacements come from this run's own manifest and
calibration. Code still never chooses, rejects a finding as a duplicate, or sets a threshold; the frontier does.

## What the frontier knows about fields and recorded findings

The first session of the Day 6 gate (#41) used all 15 frontier calls the same way. Each call recorded the
same candidate finding: "`public.movement` stays at zero while the player moves". Two facts the frontier
never had would have changed that.

- **Calibration per field.** Preflight calibration had sampled that field on the same build, both idle
  and with every control held, and never saw it change. The manifest message now gives each reader the
  class calibration measured: changed only while a control was held, only with none held, both, or no
  change seen. A reader calibration could not classify carries no label, so it stays unknown rather than
  looking measured. The prompt says what calibration does and does not tell it; see the next section.
- **Recorded findings.** The packet now lists the candidate findings already recorded in this run (id,
  title and `repro_check`), and the prompt asks for none of them again. Code still never compares,
  merges or rejects a finding as a duplicate; the frontier decides.

The code names no game field and no seeded bug: both facts come from this run's own preflight and trace.

## What the frontier is told about a field's note and the run's suspicions

In four live sessions of the Day 6 gate (#41), a reader drifted far past what its own field description allows,
and the frontier never proposed a finding. The drift suspicion was restated in the facts every turn. Two things
kept it from being weighed.

- **Calibration was read as approval.** The prompt said behaviour calibration already saw "was there before S1
  played", so a value that had changed in calibration looked normal. It now says calibration is whether a field
  changed on this build, not whether the change is right. A reading that departs from a field's note, such as a
  countdown, fraction or flag running past the bounds the note gives it, or still moving after the event it
  describes has ended, is weighed even when calibration saw it change. What the note is, and what such a reading
  can establish, is the next section.
- **Only the fresh suspicion was answered.** The frontier answers whichever suspicion is new in the facts and
  ignores the ones only restated. The packet now lists every suspicion raised so far in the run (`suspicion_id`,
  `basis`, `subject`, `turn`, `statement`), oldest first, and the prompt asks it to account for each in a
  hypothesis or a candidate finding. At most `MAX_SUSPICIONS` (40), the most recent, are listed, and
  `suspicions_omitted` states how many older ones are left out. Code never ranks, filters by content or merges
  them: the cap is the only cut.

`FrontierHelp` reads the suspicions from the trace when a request starts, before the clip is captured. A
`MemorySink` lists its `suspicion` entries; the file-backed `TraceWriter` keeps no entries, so its file is read
back (each entry is flushed as it is written). A trace that cannot be read fails the request before any call is
reserved; it is never shown to the frontier as "no suspicions".

## What a field's note is, and is not (#171)

A reader's `preflight_note` is the preflight model's own one-line reason for choosing the field; stage 4 asks it
for "one line on why". The manifest message first sent it as "what the field is meant to be" (#150), and the
prompt then said a reading that contradicts it contradicts an expectation (#152). The pipeline knows neither
claim. No code or measurement checks a note against the game, and the alias in a path is another label the
preflight model chose. In a live session a candidate finding's only expectation was a note's wording, and a later
read of the game's code showed the reading's direction was intended. The reading was real; the expectation was
never established.

Each field row in the manifest message now keeps three kinds of statement under separate keys:

| Key | What it is | Where it comes from |
|---|---|---|
| `calibration` | How the field changed when preflight sampled it on this build, idle and with each control held. | Measured. Left out when calibration could not classify the field. |
| `source` | The declaring type and field name the reader reads. | Reflected from the game by preflight's field inspection. It identifies the field and says nothing about how the field should behave. |
| `preflight_note` | The preflight model's reason for choosing the field, exactly as recorded. | A model's inference. |
| `preflight_note_origin`, `preflight_note_verification` | `preflight_model_inference` and `unverified`. | Sent with every note, whatever the note says. |

A key a row lacks was not recorded, so it is unknown. Nothing is filled in: a field with no note carries no origin
or verification, and half a descriptor yields no `source`. Every note that exists is sent as written. Nothing
trims, drops, ranks or trusts one more because of its wording, and no keyword, suffix or field-family rule is
involved, so the same code serves any game.

The prompt says a note or alias is an inference nothing has verified, so what it suggests is a possible
interpretation, not a confirmed expectation of the game. A reading that departs from it is a hypothesis: the game
may depart from the note, or the note may be wrong, and the answer should say what would tell the two apart.
Calibration seeing the field change does not excuse the reading, and neither does S1 having escalated about
something else. A candidate finding from such a reading keeps `expected_source: interpretation`, never
`invariant` or `developer_assertion`, says in `uncertainty` that the expectation rests on a note or name nothing
has verified, and has severity `unknown` unless something besides that note or name supports one.

**What this does.** It labels what the frontier is shown and asks it to reason accordingly. No code rejects, ranks
or suppresses a finding on this basis; the answer schema, the packet and S1's choices are identical with and
without notes (`tests/test_frontier_help.py`), and no branch of the loop reads a note.

**What it does not do.** It does not make the frontier model heed the labels, and no model call was made for this
change, so whether it does is untested. Asking for a hypothesis first and for `unknown` severity may also lower
recall for a true defect whose only support is a note. That cost is unmeasured as well.

## How S1 reads the adopted guidance (#170)

An adopted frontier answer stays in S1's context for the rest of the run, and every answer lists its hypotheses in
full, so a revised hypothesis recurred once per answer. In the b04 session-05 trace the guidance reached 31,622
characters, 55% of the final 57,538-character payload, and the larger requests made token pacing wait longer.
`FrontierHelp.render_guidance` (the codec is `runner/guidance_codec.py`) lays the same adopted list out as three blocks:

| Block | Holds |
|---|---|
| `current_hypotheses` | The latest complete row of every hypothesis id, with the `requested_turn` of the answer that stated it. A closed hypothesis (`supported`, `contradicted`, `withdrawn`) stays, with its status. |
| `earlier_versions` | Every older row of those ids, newest first. A row states only the fields that differ from the next newer version, and `other_fields_same_as_requested_turn: N` says every field it leaves out equals the version stated at request turn N. A null is always stated, never left out. |
| `help_adoptions` | One entry per adopted answer, in order, with `source`, `kind`, `choice_id`, the turns, `age_s`, `waited_s` and the result as adopted. An answer whose rows moved to the first two blocks lists their ids in `hypothesis_ids`. Another helper's result, and a frontier result with no usable hypothesis row (an error, the call cap, a replacement), are copied in byte for byte. |

Only the layout changes. Every statement, `confirm_if`, `refute_if`, status, citation, candidate finding, error and
review stays in S1's view, nothing is ranked or reworded, and `1`, `1.0` and `true` stay distinct.

**Exact roundtrip, or the list.** `TurnLoop._guidance_view` builds the list of adopted items as before. If the help
port also offers `render_guidance(items)` and `restore_guidance(view)` (both are given copies), the loop sends the
layout only when `restore_guidance` returns that very list, compared as canonical JSON. With no such pair, or a layout
that is lossy, malformed or raises, S1 reads the list unchanged. The core names no frontier word: it holds the generic
hook and `build_context` takes either a list or a mapping. With no guidance the key is absent and the payload bytes are
unchanged (golden hash in `tests/test_loop_guidance_layout.py`). The trace keeps today's per-answer result in each
`help` `adopted` entry, and the `context` entry holds the payload as sent, so `restore_guidance(payload["guidance"])`
gives back the list and the request hash can be rebuilt. Cancellation, scene boundaries and operator results behave as
before: a cancelled request adds no row, version or entry, adopted guidance stays after a boundary, and an operator's
result sits in the ledger unchanged.

**Measured size** (canonical-JSON characters, not tokens; offline, no model call; `scripts/guidance_layout_trace.py`
on the 94 contexts of the b04 session-05 trace, 77 of which carry guidance):

| | List | Layout |
|---|---|---|
| Final guidance | 31,622 | 22,342 (29.3% smaller) |
| Final payload | 57,538 | 48,258 (16.1% smaller) |
| Median payload, contexts with guidance | 39,406 | 35,989 (8.7% smaller) |
| State characters summed over the 123 S1 calls | 4,461,714 | 4,128,624 (7.5% smaller) |

All 77 layouts restore to the traced list exactly, and the recorded context hash is rebuilt from the restored list in
all 77. At the final context the blocks hold 4,651 characters (10 current rows), 13,999 (47 earlier versions) and 3,631
(15 adoptions); the layout is larger than the list in the first 19 contexts that carry guidance, by at most 155.

**What this does not show.** Only bytes were measured. Whether S1 reads the layout better or worse, or decides
differently, is untested, and a claim about decisions needs a paired evaluation on frozen contexts. The earlier
wordings stay in view, so growth is still linear in the number of answers (this trace averages 1.5k characters per
answer in the layout against 2.1k in the list), and nothing bounds it: dropping an old hypothesis would hide an open or
negative one. With the frontier attached, `guidance` is a mapping once anything is adopted (an error-only first answer
gives empty first two blocks); it was a list.

## Exploration targets (#179)

Across six b04 sessions S1 never reached the stairs' front (closest measured:
4.42 units in sessions 4–6, beside the stairs; once 3.24 units from a side
staircase in session 4). In session 6 it picked `wait` (63) or `move_forward`
(36) on 99 of 146 turns, and the adopted frontier hypotheses explained
suspicions (jump displacement, `move_forward` shortfalls, `jump_ttl`) rather
than pointing at untried level parts. The system prompt now lets the frontier,
when its clip shows a part of the level no recorded action appears to have
reached or used, add a hypothesis about it with a directive that sends S1 to go
and try it. The wording stays generic
(`tests/test_name_blind.py` bans feature words in roles): a part, grounded in
what the clip shows and where in the view it appears, with `unknown` for what
the clip and the bounded recent rows do not settle.

**What this does.** Advice only, inside the existing contracts: a hypothesis
whose `confirm_if`/`refute_if` name readings or an `effect.<action>`
displacement, and a directive with a `location` target proposing packet choices
by id. It adds to the answer and never replaces accounting for each suspicion;
the clip alone supports no candidate finding. `review` accepts such an answer
unchanged (`tests/test_roles_frontier.py`).

**What this does not do.** No code chooses, ranks or substitutes: S1 still
decides whether to follow the directive. Whether the frontier proposes such
targets, and whether S1 follows them, is untested — no model call was made for
this change — and any claim about findings needs a paired evaluation.

## Exact request artifacts

Before reserving or dispatching a frontier call, the helper writes the canonical UTF-8 JSON body to
`<run>/requests/frontier/<request_sha256>.json`. The artifact includes the full inline clip, packet, manifest
message, system prompt and answer schema. HTTP headers and transport credentials are not part of the body.
The ledger records the same hash and a relative `request_body_path`, so later review can verify the exact
request without reconstructing transient helper state. These full artifacts stay local under `runs/`.

Identical bodies reuse their content-addressed artifact; each authorized call still reserves and dispatches
normally. An existing artifact whose bytes disagree with its hash is refused. A storage failure prevents
reservation and dispatch and is reported through the helper error path. A body may remain when a later budget
check refuses the call; an artifact alone does not prove dispatch. This capture applies to future calls and
does not establish the contents of earlier calls whose bodies were not retained.

`tests/test_frontier_request_archive.py` checks the actual helper request, inline media, ledger linkage, absence
of a supplied transport credential, exact serialization, damaged-artifact refusal and storage failure before
dispatch. These are offline fake-provider checks, not evidence from a new paid run.

## S1 ownership and evidence audit

| Branch | Runtime behavior | Evidence |
|---|---|---|
| Invalid S1 answer | Rejects the turn without input; no helper starts. | `TurnLoop.run_turn`, `core/turn_loop.py:322`; parser and loop coverage in `tests/test_decisions.py` and `tests/test_turn_loop.py`. |
| Action or skill | Executes only the selected choice through the bound observation; a stale rejection sends no substitute. | `core/turn_loop.py:328`, `_send` at `:446`; adherence tests. |
| Finish | Ends normally, then stops and releases inputs. | finish choice branch after `:328`; `tests/test_runner_session.py`. |
| Escalate or delegate | Only the selected help choice calls `_help_request`; runner calls the frontier only for the attached `escalate` helper. | `core/turn_loop.py:336-337`; `runner/frontier_help.py:42`. |
| Pending help | Executes exactly S1's valid `while_pending` continuation; otherwise waits under the existing default. | `core/turn_loop.py:343`; `tests/test_turn_loop.py`. |
| Helper adoption | A result ready at turn entry is adopted once after the fresh observation's epoch check; prior-epoch help is discarded. | `core/turn_loop.py` (`run_turn`, `_adopt_finished_help`); end-to-end tests in `tests/test_loop_help_publication.py`. |
| Directive scoring | Reads the current observation and shared comparator result to write a fact and trace outcome; it cannot call the help port or choose input. | `runner/directives.py:45-71`; all terminal outcomes covered in `tests/test_frontier_help.py`. |
| Directive view | After a facts source's `facts` for the observation, the loop asks it for the directives it tracks and states them in `directive`, `directive_history` and `directive_provenance` (`turn-loop.md`, "The context"). It reads only what `adopt_result` already published, so it cannot show an unadopted, replaced or prior-epoch pending result. A previously adopted directive invalidated at a boundary remains in the closed history. It chooses no input, opens no help, drops no choice or directive, and compares no age with a limit; a source that is absent, silent or faulty leaves the context as it was. | `TurnLoop._directive_view`; `DirectiveTracker.directive_view`; `tests/test_directive_context.py` (serialized contexts; a run with and without the view differs only in those keys). |
| Guidance layout | Lays out results already adopted. The loop sends the layout only if `restore_guidance` returns the adopted list exactly, else the list. No choice, help request, default or adoption is read or changed, and no count, score or timer is involved. | `TurnLoop._guidance_view`; `runner/guidance_codec.py`; `tests/test_guidance_codec.py`, `tests/test_loop_guidance_layout.py`. |
| Reader notes | Labels each recorded note an unverified preflight-model inference in the frontier's manifest message. Every note is sent as written; no code filters, ranks, or acts on one, and the answer schema, packet and choices do not change. | `provider.manifest_message`, `FrontierHelp.reader_notes`; `tests/test_frontier_provider.py` and `tests/test_frontier_help.py` (actual request). |
| Exploration targets | Prompt text invites a hypothesis plus a go-try directive about a clip-shown part no recorded action tried. Advice only: no branch chooses, escalates, or substitutes, and the directive is scored like any other. | `roles/frontier.py` SYSTEM; `tests/test_roles_frontier.py` (prompt needles, review acceptance). |

The request uses the exact context S1 received and the explicit request turn and observation sequence. Investigation rows cite the corresponding observation's trace entry. The fake end-to-end trace passes the trace-entry validator, adherence checker and trace replay checks. No live trace was collected, as requested.
