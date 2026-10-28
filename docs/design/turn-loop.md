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
4. **State the facts** the loop can measure from its own records (`playtest.core.facts.RecordFacts`).
5. **Build the context** with `build_context` (`context`): the objective, `as_of` with the observation's
   age and the gap since the previous observation, the facts, `pending_help`, any adopted `guidance`, the
   choices. The hash in the trace is the hash of the exact payload S1 receives.
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
| Facts | `async facts(reports, observation) -> list[str]` | `RecordFacts` is the default. Measurements and counts only. |
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
- `pending_help` is always present. `null` when no request is open. Open, it carries the choice and its
  label, S1's reason if it gave one, the turn and `seq` it opened on, its age in game seconds and in wall
  seconds, what it waits on (`waiting_on`, the choice's role) and whether a helper is attached.
- `guidance` appears only once a helper's result has been adopted, so a payload without any keeps its exact
  bytes (frozen corpora hash their payloads). Each entry carries the source, the request's kind and turns,
  the result and its age. It stays in every later context with a growing age; the loop does not window it.

## Help

1. On escalation S1 also chooses an accompanying action or a wait (`while_pending`).
2. While help is pending S1's ordinary selections execute exactly as chosen, and an explicit wait sends
   no input.
3. At most one request is open. A repeat is recorded (`help` `repeat`) and creates no new call.
4. A finished result is adopted once, at the top of a later turn, and shown with its source and age; S1
   decides what to do with it.
5. Stop cancels the helper and releases inputs.

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
(`UPA_RUN_TOKENS_PER_MINUTE`, `-TokensPerMinute` in `run-session.ps1`) sets the limit; the default is
85,000, which leaves 15% headroom. The next call is expected to cost what the last did, and a help turn
makes two calls, so one call's tokens of overshoot are possible; the headroom is what covers it.

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

An executable `ManifestAction` has exactly one control, `inputs[0].control_id`. The manifest's
`selection.offered_controls` row for that control carries the bridge action name the calibration catalog
gave it (`preflight_key_<key>`), and the request's `lease_s` carries the action's requested hold.

Two PlayMode hosts exist. `StageSevenPlayModeTests` registers exactly one action (the one it was told to
verify) and starts `playtest.preflight.verify` with its handshake files, so it cannot host a session of
many actions or another module. `BenchRecordingPlayModeTests` registers one action per keyboard key under
those same names with a default lease of 0.4 s, and starts the module named by `UPA_BENCH_MODULE`. The
runner uses the second. Every request carries the manifest's hold as `lease_s`, so the 0.4 s default never
applies. `scripts/unity/run-session.ps1` starts it with `UPA_BENCH_MODULE=playtest.runner.cli`; no C#
changes. The manifest supplies the module's extra inputs through `UPA_RUN_*` variables, because the host's
command line is fixed.

`playtest.runner.choices.build_choices` makes one `action` choice per executable action, in the manifest's
order, with the manifest action's name as its id. The label is the intent, the requested hold and the
calibrated effect for that hold (`mean_magnitude`, `sample_variance`, `valid_trials`); a missing
calibration reads as unknown, never zero. The four core choices follow. An action with
`execution_supported` false, and every skill, is not offered and is listed with the reason; so is an
action whose control the manifest names no bridge action for. Preconditions are passed through as text
and nothing evaluates them. A name that collides with a core choice id is an error, not a rename.

## The effect probe

`playtest.runner.probe.DisplacementProbe` reads `inspect("transforms")` before the turn's observation and
right after the terminal record, and reports the calibrated entity's net displacement against the
calibrated value for that hold. A reading that cannot be taken leaves `observed` `null` with the reason in
the fact line. The reading before is not between observing and acting, so it adds nothing to the age of
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
calls the budget counted, and how many of those were estimates). `plan.json` records `tokens_per_minute`.
The trace's `end` states the session's own status; the summary is written after it and can only downgrade
it (for example when the game worktree changed during the run).

## Not done yet

- **Deciding during a hold** (pipelining) and stepped-clock work, including stepped waits (a wait choice's
  duration is honored only in real time). A hold blocks the bridge client until its terminal record, so the
  cycle is the hold plus the S1 call.
- **The frontier and helper implementations.** The port, request and result types exist; nothing answers.
- **A facts window.** Facts describe the last turn only, and guidance is never windowed or compacted.
- **Per-action freshness.** `depends_on` is empty because reader freshness is `null` in the manifest.
- **A runtime choice builder.** The choice set is static, preconditions are not evaluated, and the
  manifest's readers are not installed in the live observation source, so S1 cannot see them.
- **Any live run.** The runner has never talked to a real Editor; its script has been parsed, not run.
