# The real-time turn loop (#29)

`playtest.core.turn_loop.TurnLoop` is the product's loop: it observes, states what was measured, builds
the context S1 reads, asks S1, and executes only what S1 chose. The core is game-agnostic. The live runner
(`playtest.runner`, `playtest-run`) binds it to one game through a preflight manifest. Everything is built
and tested against the in-process fake game; a live FPSKit run is the Day 4 gate's job (#32), and nothing
here claims one.

## What a turn does

1. **Adopt.** If the pending helper has finished, its result is adopted, once, before anything else
   (`help` `adopted`). Then the effect probe, if any, takes its reading for this turn.
2. **Observe** once (`observation`). No other reading is taken between this and the act.
3. **Bind.** The choice set is built from the manifest's choices, versioned, and bound to this
   observation's `seq` (`choice_set`). `set_version` increases every turn.
4. **State the facts** the loop can measure from its own records (`playtest.core.facts.RecordFacts`). A facts
   source that tracks directives is then asked for them (`directive_view`, see "The context"), once, for this
   same observation: after the epoch check, after any adoption, and after its `facts` scored the open directive.
5. **Build the context** with `build_context` (`context`): the objective, `as_of` with the observation's
   age and the gap since the previous observation, the facts, the directives, `pending_help`, any adopted
   `guidance`, the choices. The hash in the trace is the hash of the exact payload S1 receives.
6. **Ask S1** through the port (`s1_call`, one per provider call) and validate the answer with
   `parse_decision` (`decision`). An invalid answer, or a provider that gave none, rejects the turn:
   nothing is sent, repaired, retried or substituted.
7. **Act on the choice, and only the choice.**
   - action or skill: one `ActRequest` bound to the observation (`execution`, purpose `choice`); the
     bridge checks freshness per action, and a rejection is recorded as `rejected_stale` with no input.
   - `wait` or `finish`: no input. In real time, a `wait` choice whose params hold a positive finite
     `duration_game_s` then sleeps `duration / time_scale` wall seconds, so the next observation is taken
     after it (see "The wait choice").
   - `escalate` or `delegate`: open the one help request, or record a repeat (`help` `opened` or `repeat`),
     then run exactly the `while_pending` choice S1 gave (`execution`, purpose `while_pending`). A missing
     or invalid `while_pending` is `wait` with `policy_default` recorded; a valid primary choice stands.
8. **Measure** each input the bridge accepted (`effect`) and write the `turn` record.

`stop(reason)` cancels a pending helper (`help` `cancelled`), asks the bridge to release every held input
and verify it, and writes `stop`. It runs once. Reset, scene change and stop all end the session this way;
the bridge itself closes the session on a scene change, so the next request fails closed.

The loop never writes `run` or `end`: the harness owns them.

## Who decides what

| Turn | Condition | Selected | Effective | Executed | Outcome |
|---|---|---|---|---|---|
| invalid | unknown id, wrong `set_version`, non-object | raw choice or none | none | none | `rejected_invalid` |
| provider failure | HTTP or transport error, unparseable or unoffered answer, a port that raised | none | none | none | `rejected_invalid` (the problem is on the `s1_call` entry) |
| action, skill | valid | the choice | the choice | the choice | `executed` |
| action, skill | bridge rejects (age or fields) | the choice | the choice | none | `rejected_stale` |
| `wait`, `finish` | valid (a timed `wait` also sleeps) | the choice | the choice | none | `finished` |
| escalate, delegate | valid, `while_pending` wait | the choice | the choice | none | `escalated`, `delegated` |
| escalate, delegate | valid, `while_pending` continue | the choice | the choice | the continued choice | `escalated`, `delegated` |
| escalate, delegate | `while_pending` missing or invalid | the choice | the choice | none, `policy_default` | `escalated`, `delegated` |
| repeat help | a request is already open | the choice | the choice | as above; no new call | as above |

Nothing ranks, reads the distribution, falls back to a safe action or turns a fact into help. A stale or
failed turn is data; S1 reads it next turn as a fact and decides again. `wait` and `finish` both record
`finished` (the contract has no separate outcome); the effective choice id tells them apart.

## Ports

All are in `playtest.core.ports`; each takes only what it needs and returns data the loop records.

| Port | Signature | Notes |
|---|---|---|
| S1 | `async decide(context) -> S1Answer(answer, calls, failure, halt)` | `answer` is exactly what `parse_decision` judges. `calls` are the `s1_call` bodies. `failure` is a provider failure. `halt` asks the harness to end the run (a spent budget). It receives only the context S1 sees, and a copy of it. |
| Help | `async run(HelpRequest) -> HelpResult(source, result)` | Run as a task on escalate or delegate; cancelled on stop. No implementation exists yet. |
| Facts | `async facts(reports, observation) -> list[str]`; optional `directive_view(observation) -> DirectiveView \| None` | `RecordFacts` is the default. Measurements and counts only. A source that tracks directives also implements the hook; a wrapper forwards it unchanged (`SessionFacts`, `PacingFacts`). |
| Effect probe | `async before_turn(turn)`; `async measure(choice, execution) -> EffectReport(expected, observed, fact)` | Optional. `before_turn` runs before the observation; `measure` right after the terminal record. |
| Trace | `append(kind, body, *, author, turn, refs) -> trace_seq` | `playtest.core.trace_sink`. None by default. |

A scripted source passed to `run_turn` (the Day 2 invariants and the live smoke use one) replaces the S1
port for that turn: no context, no provider call.

The loop also takes `sleep` (default `asyncio.sleep`), used only to wait out a wait choice's duration, so
tests never really wait.

## The context

- `as_of.age_s` is measured when the context is built: wall seconds since the observation's `wall_utc`.
  It is `null` if the clock reads earlier than the observation by more than the clock can resolve.
- `as_of.sample_gap_s` is the wall seconds since the previous turn's observation, `null` on the first turn.
- `facts` for the last turn: its outcome; a bridge rejection with its reason; each accepted input's
  status, measured applied duration and release verification (`unknown` when the bridge did not say); and
  the probe's expected-versus-observed line.
- `recent_history` keeps two independent chronological windows: the last eight completed turns and
  the last eight execution attempts, including rejected attempts and `while_pending` continuations.
  Waits advance the first window without erasing physical attempts from the second. Each window states
  total and omitted counts. Rows retain choice identities, status, measured effects, and separate bridge
  observation and trace references. Missing evidence stays null. Scene/session changes, regressing
  frame/game clocks, and explicit reset/scene stops start a new epoch with discarded counts.
  Full observations and image bytes are not duplicated in this memory. Direct builder calls that omit
  history retain their original payload bytes.
- Live-session facts include remaining wall time and S1/frontier call slots. A missing counter is null;
  disabled help and an exhausted call cap are explicit. Slots do not promise money or provider availability.
  These facts never remove a choice, open help, select an action, or make S1 finish.
- `pending_help` is always present. `null` when no request is open. Open, it carries the choice and its
  label, S1's reason if it gave one, the turn and `seq` it opened on, its age in game seconds and in wall
  seconds, what it waits on (`waiting_on`, the choice's role) and whether a helper is attached.
- `guidance` appears only once a helper's result has been adopted, so a payload without any keeps its exact
  bytes (frozen corpora hash their payloads). Each entry carries the source, the request's kind and turns,
  the result and its age. It stays in every later context with a growing age; the loop does not window it.
  A help port may lay the list out differently (`render_guidance` and `restore_guidance`, see `frontier-runner.md`).
  The loop sends that layout only if `restore_guidance` gives back exactly this list; otherwise it sends the list.
- `directive`, `directive_history` and `directive_provenance` state the directives a facts source tracks (the live
  runner's `DirectiveTracker`). Before this, `directive` was always `null` and `directive_history` always `[]`
  although the facts carried every directive as prose.
  - **When.** The loop asks for a `DirectiveView` once per context, straight after that source's `facts` for the same
    observation. That is after the epoch check and any adoption, and after the tracker scored the open directive and
    took its deadline and baseline from the observation that first states it. A source returns nothing for an
    observation it has not evaluated, so nothing is stated early. Only `adopt_result` hands the tracker a
    directive, so a pending result still running, finished but not adopted, replaced, cancelled, or from an earlier
    epoch appears in none of the three keys. A previously adopted directive closes as invalidated when the
    session or scene changes or the measured frame or game time regresses; its closed record stays in history.
    A source with no hook, one that tracks nothing, and one that raises or
    returns something malformed leave the context as it would have been.
  - **What.** `directive` is the open directive, `null` when none is open. `directive_history` lists the closed ones,
    oldest first, each with the outcome it ended with and the window of what S1's inputs did. Both are the
    contract's `Directive` record, copied: a later update cannot change a payload already built or traced. A
    directive replaced by a later one is `invalidated`, and its provenance names the one that replaced it. Text,
    target and setpoint are the frontier's own; a target nobody gave is absent, not invented.
  - **Provenance.** `directive_provenance` is keyed by directive id and is present only when something is tracked.
    `requested` is the turn the help was asked on, the bridge observation `seq` the answer was composed against and
    that observation's `game_s`; `first_stated` is the same three for the first context that stated it to S1 (the
    adoption). Open directives only carry `age_game_s`: game seconds from each of those to this observation, so
    the age of the advice and the time S1 has had it are told apart. `scored_on`, `baseline` and `superseded_by`
    are described below. Anything unmeasured is `null`. A missing game time has no wall-time stand-in, a negative or
    non-finite age is `null`, and no age is compared with a limit: an old directive stays, with its age.
  - **Which number is which.** Every `seq` in the provenance is a bridge observation sequence number, comparable with
    `as_of.seq`; a trace entry number never appears there. A closed directive's `evidence_seq` is the contract's own
    citation of the trace entry that closed it, as for findings and suspicions, and is not comparable with `as_of.seq`.
    The clip a request was answered from ends when the request started, a little after `requested`'s observation.
  - **What an outcome means.** It scores one thing: whether a measured reader value satisfied the directive's setpoint
    (`scored_on` is `reader_setpoint`, or `null` when it has none, and then nothing was scored). `changed` compares
    with `baseline`, the reader value and where it was taken (`first_statement`, `escalation`, or `null` when it was
    never read). A `met` can follow a change S1 did not cause (the window's `inputs_completed` shows how many of
    S1's inputs finished); nothing reads the directive's text, so no field says a destination was reached or a task
    was done. Elapsed time and a finished input are not completion either.
  - **Advice, not fact.** The text is the frontier's advice about the observation it was composed against. It is
    not a measurement and not a commitment S1 made, and it may disagree with what the current observation or image
    shows; the provenance is what lets S1 compare them. The loop does not judge which is right.
  - **Duplication.** The fact lines still carry one `Frontier directive ...` line per directive, with the status
    reason that the typed record does not repeat, and the frontier's own packet is built from those facts, so they
    stay. The typed record repeats the prose. Replaying the 155 contexts of the session 12 trace through the real
    tracker (`runs/b04-hunt/session-12`, offline; the replayed directive lines equal the lines S1 received in
    every context) adds a median of 4,837 and at most 8,489 characters (14% and 20% of the payload, which has a
    median of 33,481). Characters, not tokens, and no latency was measured; #132 found that prompt growth raises
    S1's latency and so the share of stale actions, so the next live validation should measure it before the fact
    lines are dropped or the record is trimmed.

## Help

1. On escalation S1 also chooses an accompanying action or a wait (`while_pending`).
2. While help is pending S1's ordinary selections execute exactly as chosen, and an explicit wait sends
   no input.
3. At most one request is open. A repeat is recorded (`help` `repeat`) and creates no new call.
4. The loop remembers which result was ready when a turn began, then checks the fresh observation's
   epoch before adopting it and collecting facts. A scene/session change or regressing clock/frame
   discards the prior epoch's request, including completed, externally resolved or late results.
   The adopted result carries its source and age; S1 decides what to do with it. The frontier stages
   updates while its task runs, and the synchronous `adopt_result` hook publishes them at adoption.
   A result that finishes during observation capture waits until a later eligible turn. The directive
   tracker is updated only by that hook, which is why `directive` cannot show a result before adoption.
5. Stop cancels the helper and releases inputs. The optional `discard_result` hook clears unadopted
   state and prevents late completion from publishing after cancellation or invalidation.

Live frontier packets forward the same bounded `recent_history` S1 saw, including window omissions,
prior failures and effect measurements. Packet `seq` citations remain bridge observation IDs;
`trace_seq`/`effect_trace_seq` remain trace references. Action rows are anchored to their decision
observations and retain the effect measurements' own references; these are not interchangeable clocks.

With no helper attached, an escalation still opens a request. It stays pending, visible in `pending_help`
with `helper_attached: false`, until stop. A helper that raises is adopted as `{"error": <class name>}`.

## The S1 adapter and the `while_pending` follow-up

`playtest.runner.s1.SystemOneS1` is the `mammoth-1` adapter on Baseten System One. The request is the
shape proven live: `{"model", "questions": {"next_action": {"type": "choice", "instructions", "criteria":
{id: label}}}, "state": <context>}`. The API answers one choice per question, and multi-question requests
are unverified, so when the answer names an escalate or delegate choice in real time the adapter makes one
follow-up call in the same shape, with the question `while_pending`. Its criteria are the wait choice and
the executable choices, its instructions are neutral, and its state is the same payload. A wait choice maps
to `{"mode": "wait"}` and an executable one to `{"mode": "continue", "choice_id": id}`. A failed or
invalid follow-up leaves `while_pending` absent, so the loop applies the recorded default of wait.

Both calls are `s1_call` entries; the distribution is recorded and never used to select. Each call
reserves a ledger row before it is sent (`bench/call_budget.py`), the HTTP call runs in
`asyncio.to_thread`, and the adapter exposes its provider, model and a SHA-256 of every prompt text it can
send. The follow-up is bound to the same observation as the primary answer, so on a help turn the
continued input is older by a second call's latency when the bridge checks it; `--max-age-s` is the knob.

### The default age limit

`--max-age-s` defaults to 0.8 s. An action is bound to the observation S1 decided on, so it reaches the
bridge at least one S1 call old.

| Run | Median age at the bridge | 95th percentile | Rejected as stale at 0.5 s |
|---|---|---|---|
| 29 October reader run | 0.41 s | 0.65 s | 12% |
| Day 5 gate run-02 | 0.52 s | 0.75 s | 85% |

Below the decision latency, a stale rejection buys no fresher decision; the next turn is just as slow, so
S1 cannot act at all (#132). The limit should follow the measured latency down if S1 gets faster.

A frontier answer stays in S1's guidance for the rest of the run, and the frontier restates every open
hypothesis in each answer, usually reworded. The frontier helper now lists a hypothesis in full whenever
any content or evidence changes, including when its status is unchanged. Only an identical version is
listed by id in `restated_hypotheses`. Earlier support and contradiction citations remain available to
later frontier requests; S1's adopted guidance and the trace retain the revisions. The helper names its directive
by id. The directive tracker states every directive's setpoint,
deadline and current or final outcome as facts on every turn. Before this, the facts showed an outcome
only on the turn it closed, and guidance kept a copy frozen at `open`.

On run-02, the full copies added about 5,500 characters to every later context, and S1's median call rose
from 422 ms to 484 ms. Replaying run-02's three answers through the compact form, directive lines included,
gives:

| | Before | After |
|---|---|---|
| Guidance | 5,553 characters | 2,894 characters |
| Final context | 14,578 characters | 12,780 characters (about 720 tokens fewer) |

Growth per later answer is now its new or changed hypotheses plus one directive line.

## Pacing the calls

Baseten rate-limits `mammoth-1` to 100,000 tokens per minute. The 27 October live smoke sent about 2,200
input tokens a call, and an executed turn took about 1.46 s (a 1.2 s hold and a 0.27 s call): about 41
calls and 90,000 tokens a minute, right at the limit. A wait, a stale rejection or a help follow-up has no
hold, so a run can outpace it. A 429 is a provider failure: the turn is rejected, nothing is retried, and
three in a row end the run. So the runner paces itself first.

`playtest.runner.pacing.TokenBudget(tokens_per_minute, *, window_s=60.0, clock=time.monotonic,
sleep=asyncio.sleep, initial_estimate=3000)`:

- `record(tokens, *, estimated=False)` stores a call's tokens at the current clock time.
- `async ready() -> float` waits until the tokens recorded in the trailing window, plus the next call's
  expected tokens, fit under the limit, and returns the seconds it waited: exactly `0.0` when it did not.
  It sleeps just until enough of the oldest calls have left the window, then looks again.
- The next call's expected tokens are the most recent recorded call's tokens, or `initial_estimate` before
  the first. A call whose expected tokens alone exceed the limit goes out once the window is empty, since
  waiting longer could never make it fit.

The session awaits `ready()` before every `run_turn()`. That is before the turn adopts a helper's result,
takes the probe's reading and observes, so a wait never ages the observation an action is bound to. The
wall limit is checked before the wait and again after one. The help follow-up call is not paced
separately: it goes out right after the primary call, and a 429 on it leaves `while_pending` absent, so the
loop records the policy default as before.

`SystemOneS1` reports every call it sent to the budget: `usage.input_tokens` plus `usage.output_tokens`
when present, or, when the reply has no usable input count (a 429, a transport or adapter error, missing
usage), `ceil(len(canonical(request body)) / 2.5)` reported as an estimate. A call refused before it was
sent (the call cap, the approved envelope) reports nothing. An estimate counts in the window like a
measurement and is only marked; the summary says how many calls were estimated.

**The wait is a measured fact, not a decision.** `PacingFacts` wraps the loop's facts source. When the wait
before a turn was above zero, that turn's facts end with `Before this observation the harness waited 0.84 s
to stay under the provider's token rate limit.` It states the measurement and never advice, it appears in
that turn's context only, and the trace records it there (the `context` payload); no entry kind was added.

Pacing only delays when the next turn begins. It never skips, replaces or alters a decision, it is not
an escalation and can never open help, it is not a stop rule, and nothing retries. `--tokens-per-minute`
(or `UPA_RUN_TOKENS_PER_MINUTE`, or `-TokensPerMinute` in `run-session.ps1`) sets the limit. When none of
them gives one, the runner uses the selected arm's entry in `playtest.runner.pacing.TOKENS_PER_MINUTE_BY_ARM`;
an arm without an entry uses the 85,000 fallback, which leaves 15% headroom under Baseten's 100,000 limit
for `mammoth-1`. `liquid-d1-ultra` uses 280,000, derived with headroom from the
[paced probe](../probes/2026-10-30-liquid-d1-ultra-rate.md); its provider limit remains unknown.
`run-session.ps1` exports `UPA_RUN_TOKENS_PER_MINUTE` only when its parameter
is given, so an unset parameter does not override the arm's default. The next call is expected to cost what
the last did, and a help turn makes two calls, so one call's tokens of overshoot are possible; the headroom is
what covers it.

## The wait choice

The runner's wait is `{"id": "wait", "kind": "wait", "label": "Wait 0.5 s with no input", "params":
{"duration_game_s": 0.5}}` (build bible 5.3's example). Before, a wait had no duration and the next S1 call
followed at once. In real time the loop now sends no input and sleeps `duration_game_s / time_scale` wall
seconds, with `time_scale` read from the observation the decision was made on, so the wait is game
seconds at any speed. The sleep is skipped when `time_scale` is not positive. A wait without the param, or
with one that is not a positive finite number, keeps the old behavior and ends at once. The core reads only
the generic `duration_game_s` param; the 0.5 s is the runner's.

The sleep falls between the `decision` and `turn` entries, so the trace and its replay are unchanged: the
outcome is still `finished`, nothing is executed, and the turn's `latency_ms` includes the sleep. A `wait`
in `while_pending` (help is pending) names no choice and no duration, so it does not sleep. A stepped clock
never sleeps: waiting out game time there means stepping it, and stepped waits are out of scope here.

## Stop rules

The per-turn logic never stops the run. The session driver (`playtest.runner.session`) does, and every rule
is a fail-closed harness rule, never an action choice or an escalation (the pacing wait before a turn is a
delay, not one of them):

- S1 chose `finish`, or the wall-clock limit passed (default 600 s): a normal end, `complete`.
- N consecutive turns got no usable answer from the provider (default 3), or the port asked to halt (the
  call cap or the approved envelope is spent): `stopped`.
- An exception, a lost bridge, an input whose record never arrived: `inconclusive`.

Whatever the cause, the loop is then stopped. A run whose release the bridge did not verify, or whose
turns fail `check_adherence`, is `inconclusive`.

## Running a manifest on the bridge

A session serves the declarations the manifest proves, exactly as declared. `playtest.preflight.catalog.export`
is the one authority for which declarations can run: it keeps the actions and skills whose
`execution_supported` is true (exact, release-verified execution evidence) and writes each as the strict
`ActionDefinition` (`serialize` is the file shape), keyed by the declaration's own name. A chord, a stick,
trigger or dpad value, a mouse impulse or an ordered skill is one such definition; nothing is reduced to its
first input, and no second exporter or weaker evidence rule exists.

**The host.** `scripts/unity/run-session.ps1` runs `playtest-export-catalog` on the manifest into
`<output>/catalog.json`, pins the file's SHA-256, and sets `UPA_SESSION_CATALOG` and
`UPA_SESSION_CATALOG_SHA256` (restored when it exits). `BenchRecordingPlayModeTests` hosts the bench client
modules, and for the session module (`playtest.runner.cli`) it registers exactly that file through
`BridgeSessionCatalog.Select`: the pinned digest of the file's bytes is checked, the strict
`BridgeActionCatalog.Parse` reads it, and an empty catalog is an error. This happens before the scene loads.
A missing path, digest or file, an altered or invalid file, and an empty catalog all throw; a session never
falls back to the keyboard catalog. Every other module keeps the keyboard bench catalog (one action per key,
`preflight_key_<key>`, default lease 0.4 s) and the session variables play no part in it. The stage 7 host
`StageSevenPlayModeTests` still registers the one action it was told to verify and cannot host a session.

**The client.** `playtest-run` requires `--catalog` and `--catalog-sha256` (the same two variables the host
reads) and, before any call, any plan or any contact with the bridge, `playtest.runner.catalog.verify_host_catalog`
re-reads the file, checks the pinned digest, parses it strictly and requires it to equal
`export(manifest)`: a missing, extra or changed declaration (a chord cut to its first key, a different mouse
or stick value, hold, wait or bound, a legacy keyboard catalog) is an error. The run summary and `plan.json`
record the catalog's `catalog_sha256`. The wire has no field for the host's catalog, so this checks the file
the host was given, not what the running host holds; the Unity host remains unverified here.

**The choices.** `playtest.runner.choices.build_choices` takes the supported declarations from that export
and offers every one, in the manifest's order (its actions, then its skills), under its own name: an `action`
choice carries `action: <name>`, a `skill` choice `skill: <name>`, `params: {}`. The request names the
declaration, so the bridge runs that declaration or rejects the name. An action's `lease_s` is the exported
`default_lease_s`; a skill carries none, because its steps and `max_s` are its own bound and the loop's default
action lease never replaces them (the bridge accepts a skill lease only when it is absent or exactly `max_s`).
The label states the declaration as declared: each input's device, control and value (a mouse impulse as one
relative impulse of `x * 64`, `y * 64` pixels), the hold, and for a skill each step's holds and waits.
The four core choices follow. A declaration with `execution_supported` false is not offered and is listed with
the reason the manifest gives (no exact release-verified execution evidence, or no resolved positive hold).
The per-key bridge names in `selection.offered_controls` are not consulted. Preconditions are passed through as
text and nothing evaluates them. A name that collides with a core choice id is an error, not a rename.

**The expected effect.** Calibration measures one control at a time, at one declared value, over tested
holds. The label and the effect probe's `expected` carry a calibrated displacement only for exactly that: a
single input, at the value calibration measured, held for a tested hold. A chord, a skill, or a single input
at another value has `expected: null` and says so (`no calibrated effect is recorded for this combination of
inputs`, `... at this declared value`, `... for this sequence`). The effect of one key is never reported as
the effect of a combination, and no outcome number is invented for a compound declaration.

## The effect probe

`playtest.runner.probe.DisplacementProbe` reads `inspect("transforms")` before the turn's observation and
right after the terminal record, and reports the calibrated entity's net displacement against the
calibrated value for that declaration, when calibration measured exactly it (see "The expected effect"); a
declaration with none has `expected: null` and its fact line says no calibrated effect is recorded for it.
A reading that cannot be taken leaves `observed` `null` with the reason in the fact line. The reading before is not between observing and acting, so it adds nothing to the age of
the observation an act is bound to; the reading after follows the act.

## Output

`playtest-run` writes, under the output directory, `run.json` (the summary), `trace.jsonl`, `plan.json`
and `calls/`. The trace goes through the #30 writer (`playtest.core.trace.TraceWriter`, see
`run-record-trace.md`). The runner opens it at `<run dir>/trace.jsonl` before the first turn, so the `run`
entry is first and every line is validated and flushed as it is appended, and closes it exactly once, with
the session's status and error, after the loop's `stop` entry. That includes an exception that escapes: the
loop is stopped and its inputs released first, and `end` then reads `inconclusive` with the exception. The
writer replaces the value of every `*_API_KEY` environment variable and counts the replacements
(`redactions`, in `end` and in the summary). A harness that injects its own sink owns `run` and `end`
instead. Full traces stay local.

The run record comes from `playtest.core.run_record.build_run_record`, which measures the game's commit and
worktree state with git and reads the Unity and bridge versions from the bridge's status. The runner gives it:

- `game_project` (stored as the directory name only) and `status` (the bridge's);
- `scene`, from one `observe_once()` taken after the entity check and before the trace opens;
- `manifest_fingerprint`, the SHA-256 of the manifest file's bytes (the same value as `manifest.sha256` in
  the summary). It hashes the file; it is not the authored-structure fingerprint the preflight cache keys
  on (`playtest.preflight.cache.fingerprint`);
- `clock_mode` realtime, `input_mode` the first of the status's input backends, `reset_method` `none`
  because this runner never resets the game;
- `models`, one `s1` entry with the provider, the model and the adapter's prompt hash. `resolved_model`
  stays `unknown` at the start; each `s1_call` entry records the model the provider resolved;
- `objective`, and `budget`: the wall limit, the approved cost cap and no frontier calls.

The summary adds `settings.tokens_per_minute` and `pacing`: `waited_s` (the total seconds the session
waited before turns), `longest_wait_s`, `turns_waited`, and `calls_counted` and `calls_estimated` (how many
calls the budget counted, and how many of those were estimates). `plan.json` records the effective
`tokens_per_minute` the run used: the explicit flag, the environment value or the selected arm's default.
The trace's `end` states the session's own status; the summary is written after it and can only downgrade
it (for example when the game worktree changed during the run).

## Readers and the watch

With a manifest that declares readers, `playtest-run` installs them when the session starts and reads them
before every observation (`playtest.runner.readers`); the loop puts the values in the observation's
`public` and the `reader_errors` of the ones it could not read. A watch (`playtest.core.suspicion`) then
states one line per reader (current, previous, range of the earlier readings) and records a `suspicion`
entry when an invariant does not hold, a reader leaves its range, or an effect differs from its calibration
by more than the calibration's recorded spread allows. All of it is facts. See `suspicions.md` for the records,
the order of the entries and what the next steps can cite.

## Not done yet

- **Deciding during a hold** (pipelining) and stepped-clock work, including stepped waits (a wait choice's
  duration is honored only in real time). A hold blocks the bridge client until its terminal record, so the
  cycle is the hold plus the S1 call.
- **The frontier and helper implementations.** The port, request and result types exist; nothing answers.
- **A facts window.** Facts describe the last turn only, and guidance is never windowed or summarised (#170 lays
  it out more compactly and drops nothing).
- **Per-action freshness.** `depends_on` is empty because reader freshness is `null` in the manifest.
- **A runtime choice builder.** The choice set is static and preconditions are not evaluated. (The
  manifest's readers are installed in the live observation now; see "Readers and the watch".)
- **Any live run.** The runner has never talked to a real Editor; its script has been parsed, not run. The
  session host's catalog selection (`BridgeSessionCatalog`) and the exported declarations' execution on the real
  lease and executor run under `dotnet test` against a simulated device, and the client is tested against a
  protocol peer that serves the exported catalog. The PlayMode host itself (the scene, the Input System
  devices, the process it starts) has not been run with a session catalog, so a chord, mouse or gamepad
  declaration has not been executed in Unity through this path.
