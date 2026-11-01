# The S1 state order (opt-in, turn-stable keys first)

S1 renders `state`, then the question and its options, and scores the option labels in one forward
pass. Two independent reasons to say where each top-level key of `state` sits:

* a model reads the start and the end of a context better than its middle (Liu et al., "Lost in the
  Middle", TACL 2024), and the question comes immediately after `state`, so the values that changed
  this turn belong nearest the end;
* a provider that caches a prefix can only reuse the leading bytes that are identical from turn to
  turn, so the values that rarely change belong first.

`state_order="stable-first/1"` renders the same body with `state`'s top-level keys in a declared order.
Every value is still the canonical JSON it already was, every key is still there exactly once, and
nothing else in the body moves. The option is off by default and `canonical` is not modified.

This note records the facts, the offline measurements, why the effect could be small, and the paid
comparison as a predeclaration. **No provider call was made for this note.**

## Opt-in API

```python
from playtest.runner.s1 import SystemOneS1
from playtest.runner.state_order import StateOrder

SystemOneS1(ledger, max_calls=10, arm="liquid-d1-ultra",
            state_order=StateOrder.stable_first)   # or state_order="stable-first/1"
```

`state_order` defaults to `StateOrder.canonical`, which never calls the new code: the adapter hands the
sender the same body object it always did, and the sender serializes it with `canonical` as before.
`adapter.state_order` reports the order in force (`"canonical"` or `"stable-first/1"`). There is no CLI
flag, no loop or bench wiring, and no default change.

The Meta arm (`meta-muse-spark-s1`) **refuses** the option at construction: that arm already renders its
own turn-stable-first user text in `_stable_first`, and a second order over the same state would be a
different experiment wearing this one's name. An unknown value is refused too.

## The wire order today is alphabetical (verified)

`src/playtest/bench/replay.py:send_systemone` posts `canonical(body)`, and `canonical`
(`src/playtest/core/context.py`) sets `sort_keys=True`. So for every System One arm the `state`'s
top-level keys go out **sorted**, whatever order `build_context` inserted them in.

Checked on the 97 `context` records of the local overnight session (`runs/night/session-01/trace.jsonl`,
turns 0–96, 120 `s1_call` records: 97 `next_action`, 23 `while_pending`; d1-ultra, view enabled):

* every payload is stored **as built**, and it starts with `objective`: 97/97;
* `canonical(payload)` starts with `as_of`: the wire body's state is alphabetical, not build order;
* `canonical(payload)` reproduces the record's own `body.sha256` for **97/97** payloads, so the payloads
  read here are the payloads that were hashed;
* the alphabetical wire order of a real payload is

  `as_of, choice_set_version, choices, directive, directive_history, directive_provenance, facts,
  game_view, game_view_sha256, game_view_status, guidance, instructions, objective, observation,
  pending_help`

  (`reset_summary` exists in `build_context` but was never present in this session; the other 16 keys
  were. Three of them — `directive_provenance`, `guidance`, `recent_history` — appear only after the
  events that create them, so the key set is not constant across a run.)

**What could not be cross-checked from the trace, and why.** The recorded `request_sha256` values do not
reproduce from the trace alone: `_call` attaches the view (`body["images"] = [url]`) **before**
`request_text = canonical(body)` and `request_sha = digest(request_text)`, and the JPEG is not stored in
the trace (0/120 reproduce without it; the 169/169 gate in `s1-request-layout.md` had its images on
hand). So today's hash covers the exact bytes sent including the image, which is the property this
option must keep — and the reason the alphabetical fact above is established from the code path plus
the payload, not from the recorded hashes alone.

## Stability: measured per key

For each top-level key, the fraction of the 96 consecutive-turn pairs whose canonical value is
unchanged, plus how often the key is present and its mean canonical size. Measured, not inferred.

| key | unchanged | present | mean chars | class |
|---|---:|---:|---:|---|
| `instructions` | 96/96 | 97/97 | 1,191 | run-stable |
| `objective` | 96/96 | 97/97 | 767 | run-stable |
| `choices` | 96/96 | 97/97 | 8,442 | choice-set-stable (content) |
| `game_view_status` | 96/96 | 97/97 | 6 | run-stable until the view goes stale |
| `directive_history` | 83/96 | 97/97 | 5,308 | stable between events |
| `directive` | 74/96 | 97/97 | 243 | stable between events |
| `pending_help` | 52/96 | 97/97 | 88 | usually open-or-closed for a stretch |
| `directive_provenance` | 43/92 | 92/97 | 1,264 | per-turn (ages against this observation) |
| `guidance` | 0/92 | 92/97 | 13,965 | per-turn (and the largest block) |
| `recent_history` | 0/96 | 96/97 | 9,709 | per-turn |
| `choice_set_version` | 0/96 | 97/97 | 2 | per-turn (a counter, +1 every turn here) |
| `facts` | 0/96 | 97/97 | 5,436 | per-turn |
| `as_of` | 0/96 | 97/97 | 79 | per-turn |
| `game_view` | 0/96 | 97/97 | 195 | per-turn |
| `game_view_sha256` | 0/96 | 97/97 | 66 | per-turn |
| `observation` | 0/96 | 97/97 | 2,629 | per-turn |
| `reset_summary` | — | 0/97 | — | absent here; kept for a reset run |

Two things stand out. `choices` is the largest stable block (8.4k chars) but `choice_set_version`
changes on **every** turn, and alphabetically it sits *immediately before* `choices` — so today's shared
prefix ends before the one block that could have been reused. And the per-turn news (`facts`,
`observation`, `pending_help`) is 8.1k chars that today sits partly in the middle of a 56k-character
body.

## The declared order

```python
STATE_ORDER = ("instructions", "objective", "choices",
               "directive_history", "directive", "directive_provenance", "guidance",
               "recent_history", "reset_summary",
               "choice_set_version", "facts", "as_of",
               "game_view_status", "game_view_sha256", "game_view",
               "observation", "pending_help")
```

Most turn-stable first, the turn's own news last, with `observation` and the pending/help state nearest
the question. A key the order does not name (a future payload field, the layout marker) is still carried,
once, sorted, at the end — so an unknown key can never be dropped or duplicated.

The order is a strict reading of the measurement except for two declared exceptions, and their cost was
measured on the same 96 pairs:

* `game_view_status` is 96/96 unchanged here but sits with the view it describes, late: a status flip is
  the turn's news, and keeping it late costs at most its 6 characters of prefix.
* `pending_help` is 52/96 unchanged, which a pure stability sort would put third; it is deliberately
  last, because it is the rarest and most decision-relevant event.

For comparison, a strict stability sort of the same keys shares a mean **27,573** chars of prefix; the
declared order shares **29,827** (a strict sort with only `pending_help` moved last: 29,853). The
declared order is therefore not a sacrifice for the position argument — the two reasons agree except for
those two keys, and the position argument wins them.

## Shared-prefix measurement (offline, text only)

Each turn's `next_action` body was rebuilt from the trace payload (payload + the question's criteria from
the choices), rendered both ways with the shipped `ordered_text`, and compared with the next turn's
body. 96 consecutive pairs.

| | today (`canonical`) | `stable-first/1` | change |
|---|---:|---:|---:|
| mean body | 56,252 chars | 56,252 chars | 0 |
| shared prefix: mean | 7,517 | **29,827** | +22,311 (4.0×) |
| median | 7,516 | 25,827 | |
| min / max | 7,516 / 7,519 | 17,957 / 54,285 | |
| mean share of the body | 14.5% | **53.1%** | |
| sum over 96 steps | 721,593 | 2,863,413 | |

The shared prefix under the ordered rendering reaches past these keys on this many of the 96 steps:
`instructions`, `objective`, `choices`, `directive_history` 96; `directive` 83; `directive_provenance`
68; `guidance` 43; `recent_history` 4. Under today's order it reaches past no state key but the first
(`as_of`, 96): everything before it is the question, which is where the 7.5k comes from.

**This is an upper bound, and for this session it is not reached.** Every one of the 120 calls carried a
view, and for the `images` transport the view is a `data:image/jpeg;base64,…` on the same JSON body. The
outer body keys are canonical (sorted), so `images` comes **first**: the leading bytes on the wire are a
per-turn JPEG, and no state order can change that. The measured 53.1% is what a text-only call would
share; a view-carrying call shares only the common prefix of two JPEG data URLs, which is not measurable
from the trace (the sent bytes are not stored — see the hash caveat above). Moving `images` after `state`
would be a separate, larger change to the transport, and is not done here.

## What this changes, and what it must not

* **Off:** the sender receives the same body object and serializes it with `canonical`; the request
  body, its SHA-256 and the context record are byte-identical to today. Pinned by
  `tests/test_s1_state_order.py::test_the_default_hands_the_sender_a_body_and_hashes_it_as_before` and
  `test_naming_canonical_explicitly_sends_the_same_bytes_as_the_default`.
* **On:** `ordered_text` renders the text once, the ledger row's `request_sha256` is the digest of that
  text, and the same text is what the sender posts — so the hash cannot describe bytes other than the
  ones sent, and the body is rebuildable from the traced payload plus the question, the order and, when
  a view is attached, the image.
* The text parses back to exactly the object `canonical(body)` states (`json.loads` equality and equal
  character multisets, so nothing was added, dropped or rewritten), every state key appears once, and
  the payload the adapter was given is not mutated.
* The `dup-state-ref/1` layout still composes: it encodes first, its marker is a key the order does not
  name, and it is carried last and once.
* Arms whose `state` is wrapped in a `messages` part (`mammoth-1`, `elephant-1`) are a no-op: their
  `state` has one key. The Meta arm refuses the option and keeps its own text.

## How large could the effect plausibly be here, and what would falsify it?

The evidence for position effects comes from *generative* models over long multi-document contexts:
retrieval-and-answer tasks where an answer passage in the middle is used less. Our case differs in four
ways, and each pushes the expected effect down:

1. **Not generative.** d1 scores option labels in one forward pass; the answer is not retrieved and
   written, it is compared. Attention covers every position, and there is no autoregressive drift that
   can lose a fact after reading it. Multiple-choice scoring does show position effects, but they are
   usually smaller than long-form generation's.
2. **The options are already adjacent to the answer position.** `criteria` follows `state` in the
   question, so the labels are read with the whole context in the window; the "middle" of the state is
   at most ~25k tokens back, not a haystack.
3. **Sizes are real, if not enormous.** Measured on this session's 97 `next_action` calls: 6,120 to
   25,764 input tokens, median 19,086, mean 18,737 (2.60 chars per token, over 56,252 chars a body). So
   the contexts are long enough that a middle-position penalty could apply, and the per-turn news is a
   small part of them: `facts` + `observation` + `pending_help` average 8.1k chars ≈ 3.1k tokens, 17% of
   a call.
4. **The change is a bundle.** It moves `instructions` and `objective` to the *front* as well as the news
   to the end. A recency gain for observations could trade against a primacy gain for the rules; the
   net could be zero or negative, and this note cannot tell which.

Plausible size: if a middle penalty applies at all, the thing it would cost is a few percentage points of
score mass on the per-turn facts — not the tens of points the generative papers report, because 17% of
the context is in play and only its position changes. Anything above ~5 points of top-1 would be
surprising and would suggest the model was not reading the middle at all.

What would falsify it:

* sending the *same* payload twice under the *same* order flips the top choice as often as sending it
  under the two orders (noise ≥ effect), on ≥ 40 labelled frames and on the 97 real payloads;
* the paired top-1 acceptable difference is within that noise band, or negative;
* the mean total-variation distance between the two orders' distributions is ~0 (< 0.01) across the 97
  real payloads — that would say the order is invisible to the scorer, and the option should be dropped
  rather than kept as a hopeful default;
* a latency arm shows no difference either way (expected: the bytes are the same length).

## Is the cache argument real for d1?

Partly, and not today. vLLM's note for d1 — one question over a changing state, with prefix caching
*disabled*, has lower latency — says the reuse is too small to pay for the hashing, lookup and block
bookkeeping, and that the provider's own recommended setting is off. Our measurements agree with the
first half of that: today's shared prefix is 14.5% of a text-only body, and 0% in practice here, because
the leading bytes are the per-turn JPEG. With the ordered rendering a *text-only* call would share 53.1%
(≈8.6k of 18.7k tokens), which is the size of saving a cache would need; for view-carrying calls the
prefix is unchanged by this option. So:

* the cache argument is **not demonstrated for d1** with a view, and per the provider's note there is no
  current reason to expect a latency change from ordering at all;
* it *would* be real for text-only calls if the provider turned prefix caching on, which is unverified;
* the cost argument is empty either way: the body is the same length, so billed input tokens are
  identical (mean 18,737 per call, $0.000787 at $0.042 per million, $0.0787 per 100 decisions).

## Risks and unknowns

* **Does Liquid's hosted server preserve the key order we send?** UNKNOWN. The open-source vLLM d1
  server re-renders `state` as indented JSON, and whether that rendering keeps the order of the parsed
  mapping or sorts it is not established here; a hosted wrapper could also re-serialize. If it sorts,
  this option is a no-op on the wire and the comparison below would show the null result — which is
  itself the measurement.
* **One session, one game, one manifest.** 97 turns from one overnight run; the stability table is a
  property of this run's loop, and `guidance`/`recent_history` only appear once a helper has been used.
* **The exceptions are judgment.** `pending_help` last and the view cluster late are argued, not
  measured; the variants above bound what they cost.
* **Reconstruction with a view needs the image.** Pre-existing: the hash covers the data URL, and the
  trace stores only `game_view_sha256`. Unchanged by this option, and worth its own fix.
* **`canonical` stays the one serializer for hashes elsewhere.** This module renders text; it does not
  become the definition of a payload's identity, so no context hash moves.

## Paid comparison: predeclared design, not run

**Decision it informs.** Whether to send `stable-first/1` — as a default, as an opt-in for text-only
arms, or not at all. No metric below converts itself into that decision; a person reads the numbers.

**Estimand.** The paired difference in *top-1 acceptable* (and in acceptable mass) between the two
orders on the same frozen payloads, with the same model, prompt, transport and options; and, separately,
how often the two orders pick differently at all (sensitivity).

**Unit.** One payload (one decision frame). Same payload under both orders is a pair; the two calls of a
pair are separate call slots, in randomized order, ≥ 1 s apart, never sharing a cached slot.

**Arms.** Two: `liquid-d1-ultra` + `state_order=canonical` (control) and `liquid-d1-ultra` +
`state_order=stable-first/1`. Nothing else differs; the state order is the only manipulated variable.
The bench runner must render the ordered body through `playtest.runner.state_order.ordered_text` (one
shared code path with the adapter), send it as text (the `Send` contract already accepts text), and pin
the order in `plan.json` next to the two new `request_sha256` values.

**Corpora, and why two.**

1. *The labelled bench corpus* (`benchmarks/fpskit/labels-reviewed.json`, 40 frames): the only place with
   an accept-list, so the only place a quality estimand exists. Its payloads are ~2k tokens — exactly
   where a position effect is least likely. Reported as such.
2. *The 97 real `next_action` payloads from this session*, frozen as a corpus with their SHA-256s (no new
   labels; they have none). These are the 6k–26k-token bodies the option is for. They support only
   sensitivity: paired top-1 flip rate and mean total-variation distance between the returned
   distributions. This is the phase that can say "the order does not matter to the scorer".

**Phases and calls.**

| phase | what | calls |
|---|---|---:|
| noise | 10 bench frames × 5 repeats × 2 orders | 100 |
| sensitivity | 97 real payloads × 2 orders | 194 |
| quality | 40 labelled frames × 2 orders | 80 |
| latency | 1 large payload (≈19k tokens) × 8 rounds × 2 orders + 1 warm-up | 17 |
| **total** | | **391** |

**Cost at d1-ultra `$0.042` per 1M input tokens (output is free).**

| block | tokens | cost |
|---|---:|---:|
| 211 real-payload calls at the measured mean 18,737 | 3.95M | $0.166 |
| the same 211 at the largest observed 25,764 | 5.44M | $0.228 |
| 180 bench calls at ≤ 3,000 | 0.54M | $0.023 |
| **estimate / worst case** | | **≈ $0.19 / $0.25** |

Asked as one envelope of **391 calls, ≤ $0.30**, one run directory, no retries (a provider failure is
`unknown`, never retried). This needs the user's explicit OK before the first call; nothing above has
been sent.

**Scoring.** Per payload: which id each order chose, the two distributions, and the accepted ids. Report
(a) discordant pairs as counts (order A right / order B right) with the frame ids that flipped, (b) the
paired top-1 difference and per-frame acceptable mass, (c) the sensitivity phase's flip rate and mean TV
distance, (d) p50/p95 latency per order and the per-round paired difference, (e) tokens and cost as
reported, `null` where the provider reported none. A failure is `unknown`, not a loss.

**Noise first, then the reading rule.** The noise phase gives each order's flip rate and acceptable-mass
range on repeated identical sends. Predeclared: if the between-order flip rate is inside that band, the
result is "no detectable effect of the state order" — not "the orders are equal"; if the quality
difference is inside the band, report `inconclusive`; if both orders' top-1 acceptable differ but
within noise while the sensitivity phase shows the order does change the choice, report "the order
matters, the direction is unresolved". No single pass decides anything, and no threshold selects an
order: the numbers go to a person.

**Latency arm caveat.** The order cannot be separated from the provider's own prefix-cache state and
current load. Randomize order within each round, pair by round, report p50/p95 and the paired
differences, and claim nothing about caching from it. It is worth the 17 calls because latency is the
metric the loop actually feels: this session's 120 calls took p50 594 ms, p95 1,000 ms and max 1,204 ms
at a median 19k input tokens, against the loop's 0.8 s freshness window.

## Limits

One session, one game, one manifest, one provider. Characters are not tokens for any provider but the
measured 2.60 chars/token is this corpus's ratio. Nothing here shows that a provider preserves the key
order we send; if it does not, the whole option is a no-op, and only the comparison above would say so.
The measured prefix is an upper bound that a view erases.

## Reviewer note: the body is not the prompt

The shared-prefix numbers above compare the bytes of the JSON *request body*. A provider cache works on
the *prompt the server renders* from that body after parsing it: per vLLM's d1 implementation, `state`
first (re-rendered as indented JSON), then each question and its options. So JSON key order on the wire
matters only through one unverified step: whether the hosted renderer keeps `state`'s key order. Where
`images` sits in the JSON body says nothing about where the image sits in the rendered prompt, so "a
view erases the prefix" is also a claim about the renderer, not the body, and it is unverified. Both
the position effect and the cache effect depend on that renderer behaviour. The cheapest check is one
paired probe: the same state in both orders, with a question whose answer depends on a value placed
first or last. It needs budget approval like any paid call.
