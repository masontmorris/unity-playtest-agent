# Multi-speed S1: a fast inner loop and a slow outer loop

Status: design study only, with no paid call and no Unity run. It was written by a DeepSeek worker and
reviewed by the orchestrator, whose one correction is marked in section 4. `src/playtest/runner/fast_context.py`
and `tests/test_fast_context.py` are an unwired prototype: nothing in the turn loop calls them, and no run
flag selects them. Every number below is one of three things: measured on the 31 October trial traces,
produced by the calibrated estimator in section 5 (error ≤ 1.1% against the provider's own counts), or
derived arithmetic marked as such.

**Question.** S1 is one decision model, `liquid-d1-ultra`, answering one typed choice question per call
over a JSON `state`, with no memory between calls. Would a *fast loop* of small-context decisions
(move, step, turn, stop) plus a *slow loop* of full-context decisions (strategy, help) buy enough
decision rate to matter?

**Short answer.** No. The split is not what makes S1 faster: the token bill is. Three quarters of every
request is what S1 must see to decide at all, and the removable quarter is 20–27% of the bill, worth
1.3–1.7× decision rate in one loop with one question and every choice still offered. A second loop costs
4–7% of that rate, needs a second pacing queue, and has no information the single loop cannot have by
re-reading the full context every k-th turn. Below about 6.7k billed tokens the binding constraint stops
being tokens and becomes model latency plus skill execution: the fastest S1 loop the budget and the model
can produce is about 1 Hz, and "moment to moment" control is 10–60 Hz. What closes that gap is held input
(`docs/design/continuous-input.md`), not more decisions.

---

## 1. The problem, measured

`climb-continuous-01`, 31 October, 180 s wall limit, `--tokens-per-minute 420000`, `d1-ultra`,
`compact-object/1`, `policy/2`, game view on. 104 decisions in 178.0 s from the first to the last turn.

| Measurement | Value |
|---|---|
| decisions | 104 (109 calls: 5 `while_pending` follow-ups) |
| decision rate | 35.0 / min, median start-to-start interval **1.71 s** |
| pacing | 70.5 s waited across 76 turns (**0.68 s/turn**, longest 1.53 s) |
| S1 call latency | p50 **313 ms**, p90 469 ms, max 1391 ms |
| billed tokens per call | median **9,171**, mean 9,049, min 5,955, max 10,977 |
| achieved rate | 332k tokens/min = **79%** of the 420k setting |
| outcomes | 83 executed, 11 wait, 5 escalated, 3 `rejected_stale`, 2 restarted |

The given decomposition of a turn, 0.69 s pacing + 0.37 s call + 0.72 s skill execution, is additive in
this run: the 5-step turn skill blocks for about 0.72 s, and continuous input only overlaps the *hold* of
an eligible single-key action, not a skill.

Two facts about that decomposition matter more than the total:

1. **The cap is a token rate, not a call rate.** 420k ÷ 9.2k ≈ 45 decisions/min is the ceiling; the run
   achieved 35. Latency alone would allow ~190/min. So tokens, not the model, set the pace today.
2. **The bill is mostly not history.** On the wire (after `compact-object/1`), one turn's 9.25k tokens
   decompose as:

| Field | Marginal billed tokens | Share |
|---|---:|---:|
| question block (instructions + every option line) | 2,407 | 26% |
| `observation` (readers, UI, scene) | 1,411 | 15% |
| `recent_history` | 1,491 | 16% |
| `facts` | 1,048 | 11% |
| `guidance` (present on 27 of 104 turns) | 813 | 9% |
| `directive_history` (2 closed directives) | 743 | 8% |
| `directive_provenance` | 338 | 4% |
| `objective`, `as_of`, `directive`, `pending_help`, view keys | ~390 | 4% |
| game view (640×480 JPEG) | 300 | 3% |

Marginals are per-field medians and do not sum exactly. `climb-stairs-02` (43 turns, 46 calls) has no
guidance, no provenance and a larger history: `recent_history` is 2,789 tokens (29%) and the whole turn
9,512.

`recent_history` is the only field a fast context can remove in bulk, and it is not unique evidence: the
`facts` block already states the latest turn's outcome ("Turn 69: mouse_turn_right was executed") and
every directive's goal and status, and each history block reports its own `total` and `omitted`. What the
history adds is seven more turns of "who chose what, with what outcome". `directive_history` is the same
shape of duplication: `facts` restate each directive's goal and status, and what is genuinely lost is a
closed directive's `replan_if`. `guidance` and `directive_provenance` are *not* recaps — the first holds
refute conditions and negative results, the second is the only statement of how old advice is — and they
are cheap: 9% and 4%.

## 2. Hierarchical control framing

An inner loop and an outer loop, with the inner running faster:

| | inner (fast) | outer (slow) |
|---|---|---|
| decision | one step of control: which input now | which plan, whether to ask for help, whether to stop |
| needed state | this observation, the current plan, the last turn | all of it: history, guidance, closed directives, advice age |
| rate | f · 420k / T_fast | (1 − f) · 420k / T_slow |
| horizon | one execution, ~0.7 s | one directive, median **7.5 game-s** of deadline left |

Three consequences follow from the measurements rather than from control theory:

* **The rates are not independent.** Both loops draw on one token cap, so `f·B/T_f + (1−f)·B/T_s ≤ B/T_f`:
  any split spends part of the budget on the more expensive loop and therefore yields *fewer* decisions per
  minute than spending it all on the cheap one. Section 4 quantifies the tax.
* **The outer loop's horizon is shorter than the inner loop's refresh period.** The median open directive
  had 7.5 game-s left. An outer loop refreshing every 8 inner turns at 1 s is already executing an expired
  plan between refreshes. The single-loop alternative has no such coupling, because the *directive itself*
  is in every context; only the history is on a slower cadence.
* **The inner loop is memoryless, so the plan must be re-presented every turn.** d1 carries nothing between
  calls. A fast context that omits the plan does not produce a fast controller; it produces a controller
  with no setpoint, which is what "acting on a stale plan" really means here.

## 3. The five options

Worked numbers use `climb-continuous-01` (T_full 9,246; T_fast 7,394 for the shipped prototype; T_lean
7,054; T_motor 5,684). Decisions/min at the cap is `420000 / T`; the budget floor per decision is
`60·T/420000` seconds. Both games are in the table in section 5.

### (a) Two context sizes, same choices, two rates

Budget split with `f` on the fast loop, T_f 7,394, T_s 9,246:

| f | fast/min | slow/min | total/min |
|---:|---:|---:|---:|
| 1.0 (one loop) | 56.8 | — | **56.8** |
| 0.80 | 45.4 | 9.1 | 54.5 |
| 0.75 | 42.6 | 11.4 | 54.0 |
| 0.67 | 38.1 | 15.0 | 53.1 |

The split costs 4–7% of decisions (6.5% at `f` = 0.67) and buys 9–15 slower turns per minute. Those slow
turns see 25% more context — but they see it *in a separate decision stream* whose plan the fast stream
cannot read except through the context the fast stream no longer carries. Two further costs are concrete:

* **One `TokenBudget` or two.** `playtest.runner.pacing.TokenBudget` is a single trailing-window budget.
  Shared, a 9.2k slow call blocks the fast loop for up to ~1.3 s, and *which* stream gets to run when is
  then decided by code — the same class of decision the project forbids. Split, the session's spend rises
  to 840k tokens/min: above the 478k/min the 30 October probe saw without rate-limit errors
  (`docs/probes/2026-10-30-liquid-d1-ultra-rate.md`), i.e. unvalidated territory.
* **Two staleness rules.** The fast stream's answers are bound to their own observation, so the runner
  needs a freshness rule per stream, and the existing `max_age_s = 1.0` already rejected 3 of 104 turns.

Ownership: acceptable in principle (each turn still offers every choice), but it is a *rate tax* for
information the single loop can get more cheaply. **Reject.**

### (b) Fast loop with motor-only choices, slow loop with everything

Same arithmetic as (a) with T_f 5,684: 73.9 fast/min at f = 1; at f = 0.75, 55.4 + 11.4 = 66.8/min.

Two problems, one fatal:

* **Wall time does not improve.** 5,684 tokens has a budget floor of 0.81 s, below the 0.95 s floor set by
  latency (0.31 s) plus average execution (0.60 s). Cutting the choice set from 27 options to 14 saves
  1,400 tokens — roughly one history block — and buys **zero** wall time while skills still take 0.72 s. It
  would only pay if actions were also shortened to single steps (~0.15 s), which is a preflight/manifest
  change, not an S1 architecture change.
* **The narrow form is unlawful.** If the fast turn's criteria omit `escalate`/`finish`, then *code* decides
  when S1 may ask for help or stop, by choosing when a slow turn happens. That is exactly the rule
  "no score, threshold, stagnation detector or timer may turn into a help request". Escalation is not a
  rare corner: mean probability mass on `escalate` was **0.102**, `delegate` 0.045, `finish` 0.033, and
  escalate was chosen 5 times in 104 turns. Making it available only on schedule would gate 10% of S1's
  option mass.

The version that keeps `escalate`/`finish`/`wait` in the fast criteria is lawful but pointless: it saves
tokens that no longer bind the pace. **Reject.**

### (c) Several questions per call

d1 answers independent questions over one state in one call, so the marginal cost of a second question is
its criteria text, not a second state or a second round trip. Measured: the same 27 options asked twice
costs **+2,395 tokens (+26%)**; a second question over six options costs about **+500 (+7%)** (option lines
run 6–266 tokens each, median ~70).

What it buys depends entirely on whether the answer is carried forward:

* **More choices, same decision:** two questions that both select an action are two independent opinions
  with nothing to arbitrate between them; using either would be code choosing, using both is undefined.
  **No.**
* **A plan statement:** `next_action` plus `after_next` gives S1 a place to say what it intends next, which
  is the memory d1 lacks. That is only useful if the next context *shows* it, which means a new S1-authored
  artifact in the payload — a contract, a schema and a trace change, and one that risks outranking the
  frontier's directive in S1's reading. Worth a small separate probe; too big to smuggle into this build.
* **Batched `while_pending`:** already implemented (`--s1-pending-question batched`) and the right use of
  the mechanism: one extra question whose criteria are the small pending set, saving a whole follow-up call
  (~9k tokens and 0.3 s).

Ownership: clean, since every answer is S1's and the runner only carries it. **Keep as a separate probe.**

### (d) Action chunking

S1 already selects chunks: `mouse_turn_right` is a declared 5-step skill executed open-loop in 0.72 s.
Longer chunks (k steps chosen once) reduce calls by k and raise the rate by up to k, but:

* the loop is blind for the whole chunk, and the observation S1 chose from is stamped evidence that ages
  while it runs (the trace already has 3 `rejected_stale` turns at 1.7 s turns);
* the decision is not the bottleneck during execution — the skill is;
* a plan of length k must come either from k questions (each ~+500 to +2,400 tokens, see (c)) or from
  composite options (27² = 729 sequences, which no manifest should enumerate).

The lever for speed here is *shorter* executions (single-step skills), which lowers the 0.95 s floor and
then makes tokens bind again. **Reject as a speed measure; keep skills as they are.**

### (e) Cut tokens per call, one loop

Drop the two recaps the `facts` block already restates, keep guidance, provenance, every choice and the
question untouched, and declare what is missing in the payload. Measured on the shipped prototype
(`derive_fast_context` defaults): **7,394 tokens** on the continuous run (20% saved, 56.8/min, floor
1.06 s) and **6,897** on stairs-02 (27% saved, 60.9/min, floor 0.99 s). Hand-built without the declaration
text it is 7,080 and 6,712; the declaration itself costs, and is worth it (section 8). **Recommended.**

### Comparison

| Option | decisions/min at cap (cont. / stairs) | wall-time gain | ownership | what S1 loses |
|---|---:|---|---|---|
| (a) two loops, two context sizes | 54 / 57 at f = 0.75 | 4–7% of (e) | needs a second pacing queue and a second staleness rule; *code* arbitrates stream priority | nothing per turn, but the plan is up to k turns old |
| (b) motor-only fast choices | 74 / 79 | none while skills cost 0.72 s | **unlawful** if the fast turn cannot escalate | 13 capabilities per fast turn |
| (c) extra question | −26% for a second question over all options; −7% over six | none by itself | clean | nothing; adds S1's own plan, if a contract carries it |
| (d) chunking | ×k, but execution-bound | ~0 under 0.7 s skills | clean but blind for k steps | every observation inside the chunk |
| (e) one loop, smaller payload | **56.8 / 60.9** | **1.3–1.7×** | unchanged | 8 turns of recap and closed directives' replan conditions, declared |

## 4. Token and rate arithmetic

**The cap is a rate, so a split is a tax.** For one loop at T, decisions/min = 420,000 / T. For a split,
`f·420k/T_f + (1−f)·420k/T_s`, which is at most `420k/T_f`. With T_f < T_s the maximum is always the
all-fast allocation. A split can only win if the fast loop's T falls *because* it is a different loop — and
it cannot: T_f is set by what a decision needs to see, not by which stream asks.

**The irreducible core.** On a representative turn, the parts a sighted fast context cannot drop:

| Part | Tokens | Share |
|---|---:|---:|
| question block: instructions + 27 option lines | 2,407 | 37% |
| observation | 1,689 | 26% |
| facts | 1,113 | 17% |
| choice objects (id, kind, action, params — labels already de-duplicated by `labels-ref`) | 1,075 | 16% |
| game view | 300 | 5% |
| **total** | **6,584** | |

That is 71% of the 9,246-token turn, and 63.8 decisions/min at the cap (floor 0.94 s). But the core table
leaves out fields that are not optional: `objective`, `as_of`, `directive`, `pending_help` and the view
keys are about 390 tokens together, and declaring what was withheld costs about 310 more. The practical
ceiling for a sighted, declared fast context is therefore **57–60 decisions/min** (6,974–7,284 tokens,
floor 1.00–1.04 s), and the shipped default (7,394) sits within 2% of it. Going below it means withholding
the observation's readers, the facts, or option lines — blinding S1 or shrinking its choice set, not
"speeding up the loop".

**Three constraints, three regimes.**

| Budget, T | token floor `60T/420k` | latency + execution floor | binding constraint |
|---:|---:|---:|---|
| 9,246 (today) | 1.32 s | ~0.95 s | tokens (+ window burstiness) |
| 7,394 (fast) | 1.06 s | ~0.95 s | tokens, just |
| 5,684 (motor choices) | 0.81 s | ~0.95 s | **latency + execution** |
| ~0 | 0 s | ~0.31 s + execution | model latency |

The measured interval today (1.71 s) sits 30% above its 1.32 s floor because the run realised only 79% of
the cap (window bursts: 76 of 104 turns waited, 0.68 s on average). So the honest projection for (e) is a
range, not a point:

* **1.3×** — 45 decisions/min, 1.34 s, if the same 21% under-utilisation persists;
* **1.7×** — 57–60 decisions/min, 1.0–1.1 s, if the smaller payload keeps pacing slack and the interval
  settles on the latency + execution floor.

Either way the second loop cannot exceed 56.8/min, which is what (e) already proposes.

**Correction (orchestrator, 31 October): the lost 21% is the spacing anchor, not window bursts.**

The pacer's spacing rule waits from the previous call's *end*. `record()` runs when the response arrives,
and `_delay` spaces the next call from that entry, so every turn adds two things on top of the spacing:
- the call's own latency, about 0.3–0.4 s;
- the work before the send, 0.11–0.15 s.

The traces confirm it. Across `climb-continuous-01`, `climb-held-pattern-01` and `climb-stairs-02`, 149 of
150 waited turns had `ready()` return within 50 ms of the previous call's end plus its spacing (median
8 ms).

Anchoring the spacing at send predicts these rates on the two 3-minute runs, with nothing S1 sees changed:
- 35.1 → 41.8 decisions/min;
- 31.7 → 37.1 decisions/min.

The fast context would pay the same tax. Under today's anchor it would reach about 38–40/min, not 45–60.
With both changes it would reach about 48–50/min, where turn skills' execution starts to bind. The anchor
is being added separately as opt-in `--pacing-anchor send`.

**What the rate means for control.** One decision per second with a 0.3 s call and a 0.7 s skill is not a
control loop: a symmetric obstacle needs a correction every 100–200 ms. The trial's own evidence says the
same thing from the other side: 11 wait turns, one run of **6 consecutive waits** — about 10 s with no input
at all — while help was pending. That is a policy problem (it is what `policy/3` and continuous input
address), and no decision rate fixes it: a fast loop would have waited six times faster.

## 5. Offline sizing

Method: each recorded `context` payload was rebuilt into the exact wire form the 31 October runs sent
(`state_codec.encode_object`, `compact-object/1`, all rules that apply), then measured as
`tokens(json.dumps(state, indent=2)) + tokens(canonical(question block)) + 300` with the d1 tokenizer
(`tokenizers`, `C:\Users\mason\AppData\Local\Temp\s1tok-hf\tokenizer.json`). Against the provider's own
`usage.input_tokens` on the same calls: **MAPE 1.0%** (continuous) and **1.1%** (stairs-02), slope
0.991/0.989. This reproduces the rendering model in `docs/design/s1-token-budget.md` §1. Every row labelled
"shipped" imports `playtest.runner.fast_context` and runs it on the recovered payload, so those rows measure
the module itself rather than a hand edit of the JSON.

Median billed tokens per decision, and decisions/min at the 420k setting:

| Context | continuous-01 | /min | stairs-02 | /min | saved |
|---|---:|---:|---:|---:|---:|
| today (full, compact-object) | 9,246 | 45.4 | 9,512 | 44.2 | — |
| no `recent_history` | 7,824 | 53.7 | 6,719 | 62.5 | 15% / 29% |
| + no `guidance` | 7,761 | 54.1 | 6,719 | 62.5 | 16% / 29% |
| + no `directive_history` ("lean") | 7,054 | 59.5 | 6,712 | 62.6 | 24% / 29% |
| lean + no `directive_provenance` | 6,772 | 62.0 | 6,712 | 62.6 | 27% / 29% |
| drop both recaps, **keep guidance + provenance** | 7,080 | 59.3 | 6,712 | 62.6 | 23% / 29% |
| lean + motor-only choices (14 of 27) | 5,684 | 73.9 | 5,342 | 78.6 | 39% / 44% |
| **shipped `derive_fast_context` defaults** | **7,394** | **56.8** | **6,897** | **60.9** | **20% / 27%** |
| shipped, keeping 1 `recent_history` row | 7,887 | 53.3 | 7,628 | 55.1 | 15% / 20% |
| shipped, keeping 1 closed directive | 7,740 | 54.3 | 6,897 | 60.9 | 16% / 27% |
| shipped, keeping 1 turn + 1 closed directive | 8,226 | 51.1 | 7,628 | 55.1 | 11% / 20% |
| shipped, keeping 2 turns + 2 directives | 8,616 | 48.7 | 7,937 | 52.9 | 7% / 17% |

The last three rows are the honest price of keeping recap history. On the continuous run, one row of
`recent_history` on every turn costs 493 tokens and one closed directive costs 346 — together the 832
above, and 45% of the whole saving. But the history block's cost is mostly its *shape*: eight rows cost
1,491 (the block's marginal in section 1) while one row costs 493, so the marginal row is about 143
tokens. Eight contexts each carrying one row therefore pay 3,944 tokens for the rows that one refreshed
full context shows for 1,491 — which is why the recommendation refreshes rather than retains. Guidance and
provenance are kept in every row that does not say otherwise, because they are not recaps. (Stairs-02 has
no closed directives in its payloads, so that row is unchanged there.)

Periodic refresh — full context on every k-th turn, the shipped fast context otherwise. Tokens are run
means and `/min` is `420000 / harmonic-mean tokens`, because the rate depends on the per-turn bill rather
than on its average:

| k | continuous mean tokens | /min | floor | stairs-02 /min |
|---:|---:|---:|---:|---:|
| 2 | 8,301 | 51.7 | 1.16 s | 54.0 |
| 4 | 7,885 | 54.3 | 1.11 s | 58.1 |
| 8 | 7,682 | 55.5 | 1.08 s | 59.8 |
| 16 | 7,583 | 56.1 | 1.07 s | 61.0 |
| fast only | 7,480 | 56.7 | 1.06 s | 61.7 |

(The fast-only row reads 56.7 here against 56.8 in the table above only because this table uses run means
and that one uses medians.) k = 8 costs **2%** of the rate (56.7 → 55.5; 61.7 → 59.8 on stairs) and re-reads
the full context roughly every 9 s of play at 1.1 s turns. That is the cheapest fidelity insurance in this
study, and it is the only thing standing between the recommendation and an 8-turn blind spot.

## 6. Recommended design

**One loop, one question per turn, every choice offered, a smaller payload, and a declared cadence of full
re-reads.**

1. `derive_fast_context(payload)` with its defaults — withhold `recent_history` and `directive_history`,
   keep `guidance`, `directive_provenance`, `observation`, `facts` and every `choice` untouched — as an
   opt-in `--s1-fast-context` (default off; default payload bytes unchanged).
2. Every k-th turn (k = 8, declared in the run plan and the run record) send the full context instead. This
   is a property of the turn index, not a judgement about the turn.
3. `pending_help`, `directive` and `as_of` are never withheld, so help state, the plan and the reading's age
   are on every turn. That is the difference between this and a fast loop: the setpoint never goes stale.
4. `context_scope` in the payload states what is missing, with measured counts (section 8).
5. No motor-only criteria, no chunking, no second pacing queue, no second decision stream.

Expected effect, stated as a range because one of the two constraints must bind: **45–60 decisions/min
against today's 35**, turn interval 1.0–1.35 s against 1.71 s. Cost per wall-clock minute of play *rises*
(faster decisions spend the same cap more often: 420k tokens/min is $0.018/min either way at $0.042/M, but
more turns per minute means more total calls per minute of play — 1.71 s turns at 9.2k cost $0.0135/min of
play, 1.05 s turns at 7.4k cost $0.0177/min). Cost per *completed task* is unmeasured; do not claim a
saving.

## 7. Ownership audit of the recommended option

| Branch | What code does | Who decided |
|---|---|---|
| every turn | builds the fast payload from the same measured inputs | `build_context`, unchanged; no field is ranked, reordered or rewritten |
| every turn | offers every option the manifest declared | the manifest; `choices` is never filtered |
| every turn | asks one question, `next_action`, over all options | S1 |
| every k-th turn | uses the full payload | a declared cadence of the run plan; not a decision about the turn's content |
| escalate / delegate / finish | offered every turn, including fast turns | S1 — code never gates when help is possible |
| wait, help pending | unchanged; existing recorded default still applies | S1, then the recorded policy default |
| pacing | unchanged, single budget, time only | never escalates, never opens help |
| a withheld field | named in `context_scope` with counts | measured; the module cannot drop an unknown field silently |
| invalid / failed answer | recorded, no retry, no substitute | unchanged |

Legal checks:

* **"Only the choice S1 selected is executed."** Unchanged: one answer, one execution, no second stream to
  reconcile.
* **"No code path substitutes, ranks, suppresses or reinterprets it."** No option is removed at any turn.
  Context *content* is selected, which is what `build_context` already does, and the selection is declared
  in the payload rather than hidden.
* **"No threshold or timer may turn into a help request."** A token budget and a turn counter still only
  delay and size requests. `escalate` is on every turn, so nothing schedules help.
* **Context fidelity.** Every withheld field is stated with its counts; guidance and advice age are never
  withheld; the full context returns every 8th turn. The one thing the design cannot promise is that S1
  *reads* the declaration — that is measurable in the paid bench (section 11, metric 4).

## 8. Context fidelity: what is withheld, and how it is declared

The rule is "do not let a shortened history hide an unresolved hypothesis, a failed attempt, a contradiction
or a negative result". The prototype therefore withholds exactly two recaps, and says so. These are the two
statements it produced on turn 68 of `climb-continuous-01`, verbatim:

```
Fast context: recent history is not shown (26 earlier rows in turns, 19 earlier rows in
action_attempts), so the choices and outcomes of earlier turns are not in this request; facts still
state the latest turn's outcome and each directive's status.
Fast context: 2 earlier closed directive(s) are not shown; their replan conditions are not in this
request, while facts still state each directive's goal and status.
```

`context_scope` carries `withheld` (each field left out, with its per-block totals taken from the payload's
own `total` fields and the reason that field is a recap), `truncated` (each field shortened, with
`kept`/`total`/`omitted`), `statements` (one measured sentence per entry, as above) and `complete`. A field
the version does not recognise is withheld *and* reported as unclassified, so a future payload key cannot
be dropped in silence. The declaration costs about 310 tokens on a typical turn — 4% of the request, and
the difference between the 23% saving of a hand-built variant (7,080) and the 20% the shipped function
achieves (7,394).

What survives the cut, field by field:

| Evidence | After the fast context |
|---|---|
| latest turn's outcome, applied duration, release verification | in `facts` |
| each directive's goal, status, deadline, setpoint window | in `facts` |
| open hypotheses, refute conditions, help results | **kept** (`guidance`) |
| when each directive was asked and stated, its age | **kept** (`directive_provenance`) |
| the open directive and its `continue_if`/`replan_if` | **kept** (`directive`) |
| an open help request | **kept** (`pending_help`) |
| reader values, previous values, earlier ranges | **kept** (`facts`, `observation`) |
| 8 turns' choices and outcomes | withheld, counted, and restored every 8th turn |
| closed directives' `replan_if` | withheld, counted; their goals and status remain in `facts` |

The known weakness: the counts in the declaration are read from the payload's own `total` fields. If a
monitor under-reports its own total, the declaration repeats the error rather than correcting it. It states
what it was given.

## 9. Risks

1. **A changed prompt moves near-ties.** This project has already measured that: a *lossless* restructure
   (`compact-state/1`) changed d1-ultra's modal choice on 11–12 of 40 frozen contexts, and the string path
   moved them so much that the option was left off by default (`docs/design/s1-token-budget.md` §8). A
   20–27% lossy cut is a larger change. Expect a similar or larger flip rate at margins around 0.04–0.15,
   and do not read flips as improvement or regression without an outcome measure.
2. **`finish` at a near-tie.** Both `compact-object` trials ended after ~45 s when S1 chose `finish` with
   untried actions left. A fast context changes the same distribution; the bench must count `finish`
   separately, and a live trial needs a control arm.
3. **Silent re-tries of a failed approach.** Dropping 8 turns of "what I tried" is exactly the failure the
   fidelity law names. Mitigated by the declaration and the k-th-turn refresh, not eliminated.
4. **Faster turns shorten the input lease.** The continuous-input lease is twice the largest observed
   interval, capped at 10 s; at 1.0 s turns it is ~2 s instead of ~3.4 s. That reduces overshoot and makes
   expiry more likely during a slow call.
5. **A new payload key.** `context_scope` reaches trace consumers, the report, replay corpora and payload
   hashes. Frozen corpora hash payloads, so the mode must stay opt-in with byte-identical defaults.
6. **Prompt caching.** A smaller, differently shaped state may change the provider's cache behaviour in a
   direction this study cannot see; latency is the only observable.
7. **The agreement oracle is weak.** A 104-turn trial with 5 escalations, 11 waits and a 6-turn wait run is
   not a model of good play. Agreement with it bounds *change*, not *quality*.
8. **Cost.** More decisions per minute of play means more tokens per minute of play. Only wall time per task
   improves, and only if the task completes sooner — unmeasured.

## 10. Recommendation

**Do not build the multi-speed split.** Build the fast context, behind a flag, and let a frozen-context
bench decide whether it ships.

* (a) two loops: rejected — a 4–7% rate tax, a second pacing queue, a second staleness rule, and a plan that
  ages up to 8 inner turns against a 7.5 game-s median deadline.
* (b) motor-only fast choices: rejected — no wall-time gain while skills cost 0.72 s, and the narrow form
  lets code decide when help is possible.
* (c) extra questions: deferred — cheap and lawful, worth a separate probe only if S1 needs a place to state
  a plan.
* (d) chunking: rejected — skills are already chunks, and longer chunks trade sight for calls the loop does
  not need.
* (e) smaller payload in one loop: **recommended**, 20–27% fewer tokens, 56.8–60.9 decisions/min at the cap,
  with the full context every 8th turn and a declared scope on the rest.

If the bench clears the predeclared rule below, ship (e) with `k = 8`. If it does not, the next question is
not a second loop but a live A/B of the fast context against the full one, measuring findings and progress
per minute — the same conclusion the compact-state probe reached, for the same reason.

**What would change this recommendation.** If a live trial shows the binding cost has moved from tokens to
execution (turn interval ≈ skill duration, pacing wait ≈ 0), then single-step skills become the lever, and
*motor-only* fast criteria would start to pay — because the token floor (0.81 s) would then be below the
execution floor. That is a manifest change, and it should be measured before any criteria are withheld.

## 11. Paid evaluation design (designed, not run, not approved)

Frozen inputs only; no Unity, no frontier calls, no edits to `runs/`.

**Corpus.** All 147 recorded primary contexts (104 continuous, 43 stairs-02), with their payloads,
instructions text, criteria ids and labels, and the JPEGs recovered from the session recordings by
`game_view_sha256`. Freeze by sha256 of `canonical(payload)` per turn and record the list in the probe note.
Option order `sorted`, `state_order` canonical, `compact-object/1` with the same applied rules, primary
question only.

**Arms.**

| Arm | State |
|---|---|
| `full` | the recorded payload, as sent on 31 October |
| `fast` | `derive_fast_context(payload)` defaults, including `context_scope` |
| `refresh` (optional) | `full` on every 8th turn of the sequence, `fast` otherwise |

**Repeats and order.** 5 repeats per (context, arm), seeded random order, as in the 31 October bench, so
leave-one-out self-agreement gives the noise ceiling for each arm.

**Calls and cost.**

| Arm | calls | tokens (mean) | cost at $0.042/M |
|---|---:|---:|---:|
| full | 735 | 6.72 M | $0.282 |
| fast | 735 | 5.35 M | $0.225 |
| refresh | 735 | 5.53 M | $0.232 |
| **total (3 arms)** | **2,205** | **17.60 M** | **$0.739** |

Ceiling with 10% margin: **$0.81**, 2,205 calls, one approval. (Two arms only: 1,470 calls, 12.07 M,
$0.51.) No retries beyond one repeat replacement on a transport error, which is recorded.

**Metrics.**

1. Modal exact-id agreement of `fast` with `full`, read against `full`'s leave-one-out self-agreement.
2. Agreement by outcome class (execute / wait / help / finish / restart), because the classes fail
   differently.
3. The help subset: every turn where `full`'s modal choice is `escalate`, `delegate` or `finish` — report
   `fast`'s modal choice and the `finish` count per arm (the compact-object trials' early ends make this the
   one class that must not drift).
4. Distribution distance: d1 returns probabilities, so report the per-turn L1 distance and the top-two
   margin on turns that disagree. A flip at margin 0.04 is noise; a flip at 0.4 is a change in what S1
   understands.
5. Billed tokens and p50 latency per arm, against the estimator's prediction.
6. Cross-tab of "the scope said something was withheld" against "the choice differed", to see whether the
   declaration is doing any work.

**Predeclared rule, written before the first call.**

* Adopt `fast` if: modal agreement ≥ `full`'s leave-one-out self-agreement − 0.05; **no** turn where
  `full` chose `escalate`, `delegate` or `finish` flips class; per-arm repeat disagreement ≤ 2%; median L1
  distance ≤ 0.06; billed reduction ≥ 18%.
* Otherwise report, do not ship, and run the live A/B instead.
* Any change to the arms, the corpus or the thresholds after the first call is reported as a deviation.

## 12. Reproduce

* Traces: `C:\Users\mason\.t3\worktrees\unity-playtest-agent\t3-d30009ac\runs\s1-trial\climb-continuous-01\trace.jsonl`
  and `...\climb-stairs-02\trace.jsonl` (read-only; `context` records carry `body.payload`, `s1_call`
  records carry `usage.input_tokens`).
* Sizing: rebuild each payload with `state_codec.encode_object(payload, instructions=…, criteria=…,
  rules=state_codec.ALL_RULES)`; measure `tokens(json.dumps(state, indent=2)) + tokens(canonical(question
  block)) + 300` with the d1 tokenizer. The repo venv has no `tokenizers`; use `python -E` with the
  tokenizer file, and `uv run python` for the `playtest` import. Both stages are in `%TEMP%\opencode\` on
  the machine that ran this study: `stage1c.py` → `stage2c.py` for the hand-built variants and the
  irreducible core, and `stage1e.py` → `stage2e.py`/`stage2f.py` → `stage2g.py` for the shipped function,
  the refresh table and the arm totals.
* The prototype: `src/playtest/runner/fast_context.py`, tests `tests/test_fast_context.py`
  (`uv run pytest tests/test_fast_context.py tests/test_name_blind.py tests/test_context.py -q`).
* The bill model this reproduces: `docs/design/s1-token-budget.md` §1.
