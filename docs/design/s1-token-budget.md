# S1 token budget: where the billed tokens go and how to cut a third of them

**Result.** The two `d1-ultra` night sessions of 31 October ran with `-S1Layout dup-state-ref/1`. Against
that configuration, `--s1-compact-state all` cuts billed input tokens by about 31% per call, and every byte
stays restorable. Per turn that is −30% to −31%, or −34% to −37% with `--s1-pending-question batched`.
Against the production default `full` layout it is −36% to −39%, or −40% to −44% batched.

Pacing held those runs under the token budget for 62–76% of their wall time. With `all` plus batching, the
paced floor per turn falls from 4.9 s and 4.3 s to 3.3 s and 2.7 s: about 1.5× the decisions per minute.

**But it is not a transparent optimization.** A live paired bench on 40 frozen night contexts (section 8)
confirmed the savings, −29.0% billed and −15% p50 latency for `all`. It also found that the compact formats
change d1-ultra's modal choice on 11–12 of 40 contexts, which the predeclared rule calls "changes S1's
choices". The flips are near-ties. A 200-call follow-up traced the cause: Liquid renders a **string** state
with a fixed extra ~158 tokens. The same content sent as an indented string shifts choices about as much
as compaction does, so the shift belongs to the string path, not to compaction. Whether the changed choices
are better or worse is unmeasured, so the option stays off. A lossless restructuring that stays on the
object path saves about 13% of the state with no string wrapper, and is the next candidate (section 8).

An earlier draft of this note fitted the full state, labels included, and concluded that option
descriptions are not billed. That was wrong: the night runs sent labels only in the question.
Sections 1 and 4 replace it.

## 1. What the provider bills

Liquid's open d1 code (`LiquidAI/d1-2B`, revision `10e7671d`, `prompt.py`) renders a request as ChatML: a
user turn, the media, then `json.dumps(state, ensure_ascii=False, indent=2)` for an object state,
`QUESTION:`, the question's instructions, one line per option (`<code> <description>`), and the assistant
header. **A string state is written verbatim.** Options are lettered A–Z up to 26 and `00`, `01` and so on
beyond that, which covers our 27-option turns. Key order is kept.

All 196 billed calls of sessions 01–02 were rebuilt exactly. That means the payload, the
`dup-state-ref/1` layout and the JPEG recovered from the session recording by its SHA-256, and 196 of 196
request hashes equal the recorded ones. Tokenized with the d1-2B tokenizer:

| Hypothesis | Slope, reported on predicted | What is left over |
|---|---:|---|
| indented sent state + question block (instructions and every option line with its description) | **0.998** | 297 tokens, sd 17: the same for both questions and for 18–27 options |
| compact sorted JSON, the bytes we post | 1.30 | grows with the payload |

The leftover is the image: a 640×480 frame is 20×15 = 300 patches of 32 px, d1's own count. So:
**billed = indented state + full question block + about 300 for the view.** Option descriptions are billed
and rendered.

## 2. Where the billed state tokens go

Median over the 157 primary calls of the full payload, with each key indented as it sits in the state. The
night layout already removes the labels and instructions: 1,928 tokens net of its marker (medians 16,558 and
14,630).

| Key | Tokens | Share | Indentation tax |
|---|---:|---:|---:|
| recent_history | 4,411 | 26% | +32% |
| guidance | 3,614 | 21% | +24% |
| choices | 3,025 | 18% | +22% |
| observation | 1,766 | 11% | +65% |
| facts | 1,489 | 9% | +4% |
| directive_history | 1,323 | 8% | +41% |
| directive_provenance | 530 | 3% | +59% |
| instructions, objective, the rest | ~650 | 4% | |

Inside these keys:
- History attempts take 83% of history. Each row repeats 22 key names, and the choice id four times.
- Floats from derived arithmetic carry 16–17 digits.

The serialization alone costs 22%. `indent=1` saves nothing, because the tokenizer merges runs of spaces;
the newlines themselves are the tax.

## 3. Why tokens set the decision rate

The runner paces S1 at 280,000 tokens a minute (`docs/design/s1-model.md`). The harness writes every wait
into the facts:

| Session | Turns | Billed per turn | Waiting on the token budget | Paced floor per turn |
|---|---:|---:|---:|---:|
| session-01 | 97 | 22,969 | 394 s of 519 s (76%) | 4.92 s |
| session-02 | 60 | 19,989 | 125 s of 202 s (62%) | 4.28 s |

Work outside the waits took about 1.3 s a turn. The token budget, not latency, set how often S1 decided.

## 4. The opt-in compact state (`compact-state/1`)

`SystemOneS1(compact_state=...)`, or `playtest-run --s1-compact-state {off,format,default,all}`, sends
`state` as a string on Liquid arms. The string is a header line naming the rules applied, with one sentence
each on how to read them, followed by compact JSON. The traced context is untouched. The adapter encodes a
copy at the request boundary, the request hash covers the sent text, and the ledger records the rules.
`state_codec.decode` restores the exact payload from the body alone. Each rule is applied only when it can
be undone exactly.

| Rule | What changes | `default` | `all` |
|---|---|---|---|
| (format) | compact JSON instead of the server's indentation | yes | yes |
| `instructions-ref` | `state.instructions` omitted when it is the question's instructions | yes | yes |
| `history-tables` | history turns, history attempts and probe samples as `{"columns", "rows"}`, when all rows share keys | yes | yes |
| `same-as-raw` | `"="` for `effective_choice`/`executed_action`/`choice_id` equal to `raw_choice` | yes | yes |
| `effect-ref` | the part of `effect_fact` the effect probe writes from the row's own fields becomes `"@"`; the surroundings tail is kept | yes | yes |
| `empty-omitted` | a choice's empty `params` and `preconditions` omitted | yes | yes |
| `labels-ref` | a choice label omitted when the question's criteria state it, which is where `dup-state-ref/1` puts it | no | yes |

**Use `all`.** It places the labels where the night runs already had them, in the question. `default`
keeps a second copy in the state.

**Exactness.** All 763 context payloads of the six night sessions round-trip under `format`, `default` and
`all`: 2,289 of 2,289 are canonically identical. `tests/test_state_codec.py` covers:
- each rule, and each guard that withholds one;
- refusal of undecodable text;
- the adapter path, including follow-up calls, batched calls, and a view sent or stale.

**Offline effect** on the 157 primary calls of sessions 01–02:

| | State tokens, median | vs the night's sent state | Billed per primary call, median |
|---|---:|---:|---:|
| night (`dup-state-ref/1`, indented) | 14,630 | — | 17,030 |
| compact `default` | 11,241 | −23% | 13,659 (−20%) |
| compact `all` | 9,370 | **−36%** | 11,770 (**−31%**) |

**Per turn**, including the help turns' second calls:

| Session | Night config | `full` layout | `default` | `default` + batched | `all` | `all` + batched |
|---|---:|---:|---:|---:|---:|---:|
| session-01 | 22,969 | +10.1% | −19.6% | −25.9% | −29.7% | **−34.1%** |
| session-02 | 19,989 | +12.6% | −18.7% | −26.6% | −31.3% | **−36.6%** |

Batching alone is −10.1% (session-01) and −12.5% (session-02). The batched question bills its whole block,
1,904 tokens in the live gate call. It still wins, because each help turn's second call bills the state
again. A cheaper batched form would send the pending options with null descriptions, which d1 renders as
the id text. That would save about 1,800 more tokens per turn, but the pending question would see ids
instead of labels, so it needs its own check.

## 5. What is not done, and why

- **Lossy candidates.** These were measured on compact text and are not offered as options:

  | Candidate | Saving | Cost |
  |---|---:|---|
  | 7 significant digits on floats | −1.4% | precision |
  | 4 significant digits | −2.2% | precision |
  | Drop superseded hypothesis versions (`guidance.earlier_versions`) | −4.7% | could hide how a hypothesis was refuted (context-fidelity rule) |
  | Drop the raw effect objects, keep the sentence | about −4% | precision and provenance fields |
  | Smaller history windows | unmeasured | policy, not packing |
  | A 320×240 view | about 220 tokens (80 patches instead of 300) | visual detail |

  Each changes what S1 can know, so each needs an ownership audit and a paired bench.
- **Diminishing lossless returns.** Guidance tables would add an estimated 1–2%. A fact-line table would
  need exact parsing of monitor prose. A directive-line rebuild is not lossless without a `reason` field
  (compaction review, option B).
- **Other arms.** Baseten's renderer is unknown and Meta has its own request shape, so both refuse the option.

## 6. Risks

- **Format shift.** d1 likely saw indented JSON in training, and S1 now reads compact JSON with a header.
  The bench in section 7 measures whether choices move.
- **Scope of the fit.** The billing fit covers 196 calls of one game and one night, and holds live within
  1% (section 7).
- **The tokenizer.** It is d1-2B's. The slope says it matches d1-ultra on these payloads.

## 7. Live checks on d1-ultra (31 October)

Every call was ledgered against `runs/s1-opt-live/approval.json` and made by
`scripts/probes/probe_s1_compact_live.py` through the production adapter, using night contexts with their
exact JPEGs.

**Quick try (3 calls).** All three returned HTTP 200, and every request hash equalled the night's. Billed
tokens were identical to the night's (6,120, 24,745 and 21,365), and so was each choice. Latency was
547–625 ms.

**Gate (3 calls, context session-01/80; night billed 24,745):**

| Arm | Billed | Predicted | vs night | Choice |
|---|---:|---:|---:|---|
| compact `default` | 19,886 | 19,723 | −19.6% | same as night |
| compact `all` | 18,015 | 17,852 | −27.2% | same as night |
| compact `all` + batched | 19,919 | — | the second question costs 1,904 | same; `while_pending` answered |

Liquid accepts the string state, and predictions land within 1% of billing.

## 8. Paired agreement bench (600 calls, $0.37): the compact formats change S1's choices

Full record: `docs/probes/2026-10-31-d1-ultra-compact-state-bench.md`. The design was 40 frozen night
contexts with their exact JPEGs × {night, compact `default`, compact `all`} × 5 repeats, seeded random order,
primary question only. The rule was written before the first call.

| Arm | Modal agreement with night | Leave-one-out self-agreement | Billed vs night | p50 latency |
|---|---:|---:|---:|---:|
| night | — | 0.905 | — | 532 ms |
| compact `default` | 28/40 | 0.865 | −18.2% (prediction +1.1%) | 484 ms |
| compact `all` | 29/40 | 0.910 | −29.0% (prediction +1.3%) | 454 ms |

How to read it:
- **The flips are near-ties.** The night arm's median top-two margin was 0.043 on flipped contexts, against
  0.155 on kept ones. The compact choice was usually the night arm's own second choice.
- **They are not one systematic effect.** The two compact arms disagree with each other as often as with night.
- **Neither arm is noisier than night.**
- **Escalation does not move significantly per context.** `finish` rose from 1 call to 4 and 6 of 200, too
  few to read.

**Follow-up (200 calls, $0.136; full record in the probe note).** Sending the night's exact state as an
indented string reproduced neither its tokens (+158, 0 of 40 equal) nor its choices. Against the night
run's own choices, today's object-path calls agree 0.880 (no drift), and every string arm agrees
0.68–0.75 whether compacted or not. **The string path itself moves the near-ties, and compaction adds no
change this sample can separate from it.**

What follows:
1. **Keep `--s1-compact-state` off by default.** On this server a string state is a different prompt, and
   whether it decides better or worse is unmeasured.
2. **Object-path compaction is built: `--s1-compact-state object`.** The lossless rules are applied but
   `state` stays an object, so the server renders it as it does any state. The applied rules and their
   legend sit in a `_s1_compact` key, like the `dup-state-ref/1` marker.
   - **Offline:** all 763 night payloads restore exactly. On the 157 d1-ultra primary calls, billing falls
     from a median of 17,030 to 15,062 tokens (−11.6%).
   - **Live:** two 3-minute FPSKit trials (`docs/probes/2026-10-31-fpskit-compact-object-trial.md`) ran
     cleanly at about 30 decisions per minute. They billed 15.5% fewer tokens than the night layout on the
     same contexts, and 28.5% fewer than `full`.
   - **But both trials ended after about 45 s, when S1 chose `finish` at a near-tie with untried actions
     left.** A control session with the format off is needed to say whether the format causes it.
3. **Batching on the object path needs a layout fix first.** `dup-state-ref/1` only acts on single-question
   bodies, so a batched body sends the labels twice.
4. **The string path can only be settled on outcomes.** That means a live paired play comparison,
   measuring findings and progress per minute.
