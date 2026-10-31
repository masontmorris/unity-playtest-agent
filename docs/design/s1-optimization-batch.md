# S1 request optimizations: judgment of the batch

Five opt-in changes to how S1 is asked, each built on its own branch and merged here. Every one is off by
default, and with all five off a request is byte-identical to `main` at 4966ee7. No provider was called.
Nothing below shows that any change improves S1's decisions. These are offline judgments of mechanism,
cost and risk. Each says what evidence would settle it.

| Branch | Option | What it does | Verdict |
|---|---|---|---|
| `feat/s1-opt-batch-pending` | `pending_question="batched"` | asks `while_pending` in the same request as `next_action` | **keep; bench first**: a latency lever, and on d1-ultra an 18–22% token saving (`s1-token-budget.md`) |
| `feat/s1-opt-option-order` | `option_order=sorted\|manifest\|shuffled` | chooses the order the options are presented in | **keep as an instrument**, not as an optimization |
| `feat/s1-opt-state-order` | `state_order="stable-first/1"` | sends turn-stable state first and the turn's news last | **keep; one probe decides whether it can matter at all** |
| `feat/s1-opt-direct-instructions` | `--s1-instructions direct/1` | a shorter primary question text | **keep, but split before benching**: two variables in one arm |
| `feat/s1-opt-derived-facts` | none | audit for arithmetic S1 must do | **correct null result**: nothing passed the rule |

## Each change on its own

**Batched pending question.** On d1-ultra, 16–25% of turns asked for help, and the second sequential call
on those turns cost about 530–550 ms. Batching removes that round trip. In exchange, every eligible turn
carries a second question, about 2,600 tokens, because the pending options repeat their full label text.
In tokens that breaks even at an 11–19% help rate, and only if the provider counts the shared state once,
as Baseten documents. Liquid's behaviour is unverified. The trade becomes clearly positive if the second
question's options can be sent without descriptions (`criteria: {id: null}`, which vLLM's decision
endpoint accepts), because the labels already sit in `state.choices`. That would shrink the question to
about 200 tokens. Neither hosted provider has been checked for this. The answer-quality risk is that the
secondary is chosen hypothetically ("if help is requested"), without knowing that help was chosen.

**Option order.** The useful finding is that the order existed before anyone chose it: sorted-key JSON
sends options alphabetically by id. Debiasing probabilities in code is forbidden, so only presentation can
vary. Shuffling does not remove a position bias. It turns a stable error into per-turn noise, and in a
closed loop a stable error can hold S1 in a cycle where noise would let it out. A stable, deliberate order
(`manifest`) is the better default candidate. The 9 in 746 first-option picks against about 28 expected
are confounded and show nothing. The open question behind every order claim: 660 of 757 turns offered 27
options, more than open-source d1's 26 letter labels, yet the hosted model answered. How it labels the
27th option is unknown.

**State order.** The worker measured the shared leading bytes of the JSON body: 7.5k characters today
against 29.8k ordered, about 4×. But the body is not the prompt. The server parses the body and renders
its own prompt, so key order matters only if the hosted renderer keeps it. If it re-sorts, the option is a
no-op. That makes this the cheapest change to settle: one paired probe, with the same state in both orders
and a question whose answer depends on a value placed first or last.

**Direct instructions.** It saves about 260 tokens per request, 1.3–2.2% of a real later-turn payload.
Any benefit would come from framing, not from size. The variant makes two changes at once: it reframes the
text as one question, and it drops three play-policy clauses, one of which balances pursuing a destination
against broad exploration. That clause is behaviour, not padding. A bench of `direct/1` alone cannot say
which change moved the result. Add a reframe-only variant, with every clause kept, and bench that first.

**Derived facts.** A one-pass decision model is weak at arithmetic and time comparison, so the audit
looked for computation S1 would have to do itself. It found the payload already gives remaining directive
time, observation spacing, reader ranges and counts, and resource remainders. Each candidate addition
either repeated what was there or edged toward advice, such as a direction of change toward a target. No
code is the right result. The audit's lasting value is its overhead list.

## The five together

**One hidden variable, now explicit.** Both ordering changes ran into the same fact: `canonical` exists to
make hashing stable, and it also silently fixed two presentation orders. The merge renders both through
one function (`playtest.runner.wire.render`). The adapter hashes the text it sends, and an opt-in order
hands that exact text to the sender. This is the batch's structural contribution, whatever the benches
show.

**Interactions found while merging:**

- **Batched + option order** crashed: only the primary question's options were ordered, so a second
  question raised a `KeyError`. Fixed. Every question in a body now gets its own order, and the ledger
  records `presented_ids_by_question`. `test_every_opt_in_combines_in_one_request` covers the batched,
  shuffled and stable-first options together.
- **Batched + `dup-state-ref/1` layout**: the layout only acts on a single-question body, so batching
  silently turns it off on every eligible turn. It fails safe, since nothing is lost, but the layout's
  duplicate-label savings (most of a ~9k-character choices block) vanish exactly when batching adds 2,600
  tokens. Teaching the layout to reference the primary question's labels from both questions would fix
  it. Not done here.
- **State order + layout**: these compose, and the marker still restores the body exactly. But the marker
  is a key the order does not name, so it is sent last, as a ~400-character machine rule immediately
  before the question. That is the worst place for noise.
- **Direct instructions + state order**: stable-first moves `state.instructions` to the very front, away
  from the question. The question carries its own copy, so S1 still reads the text next to the options.

**Correction (token study, `s1-token-budget.md`).** The hosted d1-ultra bills the state, not the option
text, so the 2,600-token cost charged to batching above is wrong. On d1-ultra the batched question costs
about 90 billed tokens, and batching saves 18–22% of billed tokens per turn. The same study adds the
missing density lever: a lossless compact state (`--s1-compact-state`), −30% alone and −42–46% with
batching.

**The batch does not touch the dominant cost.** The night traces' mean S1 state was 30–51k characters
(12–20k tokens): recent history about 11k characters, guidance up to 14k, choices about 9k, facts about 4k.
The instructions are 1.2k. With batching on, the batch makes a request larger, not smaller. These five
changes optimize presentation and round trips, not information density. The next lever for latency and
throughput is density: the compaction candidates B and C in
`runs/b04-hunt/compaction-review-13/REVIEW.md`, null option descriptions where labels are already in
`state`, and the audit's overhead list.

## How to settle them, cheapest first

Never bench these factorially. Five options make 24 arms. Bench each against the default, and stack only
the winners. Every step needs the user's budget approval. The Liquid credit was exhausted on 31 October,
so nothing can run on `d1-ultra` until it is topped up.

1. **Renderer probe** (a handful of calls): does the hosted d1-ultra keep `state`'s key order, and does it
   accept `criteria: {id: null}`? This decides whether state order can matter, and whether batching gets
   its cheap form.
2. **Batched pending question**: paired latency and token use on frozen eligible contexts, plus agreement
   between batched and sequential `while_pending` answers (design in `s1-opt-batch-pending.md`).
3. **Option order**: `manifest` against `sorted` first, then k shuffles, scored by top-1 acceptable and
   acceptable mass on the frozen frames (`s1-opt-option-order.md`). Its cost estimate assumed 6,000-token
   calls. Real later-turn calls are 12–20k, so scale it by 2–3×.
4. **Instructions**: reframe-only against `policy/1`, then `direct/1` against reframe-only
   (`s1-opt-direct-instructions.md`). Score from the existing frozen labels rather than new human ratings.

## Verification

`feat/s1-optimization` merges the five branches. The full test suite passes (exit 0). Each branch was
reviewed and its own acceptance tests rerun before it was committed. Review fixes: batch-pending stopped
writing a synthetic key into the provider's recorded response, and spells out its conditional wording;
direct instructions dropped the `direct/1` tag from the prompt text, since the model would read it; state
order gained a reviewer note that the body is not the prompt; the merge added the shared serializer and
the combined test.
