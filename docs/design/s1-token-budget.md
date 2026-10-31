# S1 token budget: where the billed tokens go and how to halve them

**Result.** On the two `d1-ultra` night sessions of 31 October, the opt-in compact state cuts billed input
tokens per turn by 30–31%. Every byte is restorable. With batching, the cut is 42–46%. Adding choice labels
by reference brings it to 50–56%. Pacing held those runs under the token budget for 62–76% of their wall
time. So the paced floor per turn falls from 4.3–4.9 s to 2.3–2.8 s (1.9–2.4 s with labels by reference): about
1.7–1.9× the decisions per minute, or 2.0–2.3× with labels by reference. Nothing here shows that decisions stay as good. That needs the paid check at the end.

Everything below was measured offline on local traces with the open `LiquidAI/d1-2B` tokenizer and prompt
code (revision `10e7671d`). No provider was called.

## 1. What the provider bills

Liquid's open d1 code (`prompt.py`) renders a request as ChatML: a user turn, the media, then
`json.dumps(state, ensure_ascii=False, indent=2)` for an object state, `QUESTION:`, the question's
instructions, one line per option (`<code> <description>`), and the assistant header. **A string state is
written verbatim.** Options are lettered A–Z up to 26, and `00`, `01` and so on beyond that, which is the
answer for our 27-option turns. Key order is kept: `json.dumps` writes keys in the order they arrive.

The hosted `d1-ultra` reports `usage.input_tokens` per call. All 196 calls of sessions 01–02 carried a
640×480 view. Tokenizing each call's state both ways:

| Hypothesis for the billed state | Slope, reported tokens on state tokens | Residual median, sd |
|---|---:|---:|
| `indent=2`, as the open code renders an object | **1.001** | −1,636, 120 |
| compact sorted JSON, the bytes we post | 1.300 | +2,098, 1,139 |

So the state is billed as indented JSON, and the tokenizer matches. Per question type, the residual is
nearly constant: reported tokens − indented state = 472 (next_action, sd 16) and 547 (while_pending, sd 17).
It does not move with the number of options (22 or 27) or their text (+104 tokens of labels). The best fit
is: **billed ≈ indented state + ~450 (the image) + about one token per option + the question's instructions,
unless they equal `state.instructions`.** The option descriptions, about 1,850 tokens of labels, are not
billed. Whether the hosted renderer *shows* them is not known from billing. This matters for `labels-ref`
in section 5.

## 2. Where the billed state tokens go

Median over the 157 primary calls, with each key indented as it sits in the state:

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
- The choice labels take 75% of choices, about 1,860 tokens. Each label also carries calibration statistics.
- Floats from derived arithmetic carry 16–17 digits.

The serialization alone costs 22%. `indent=1` saves nothing, because the tokenizer merges runs of spaces;
the newlines themselves are the tax.

## 3. Why tokens set the decision rate

The runner paces S1 at 280,000 tokens a minute (`docs/design/s1-model.md`). The harness writes every wait
into the facts:

| Session | Turns | Billed today, per turn | Waiting on the token budget | Paced floor per turn |
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
be undone exactly for that payload.

| Rule | What changes | In `default` |
|---|---|---|
| (format) | compact JSON instead of the server's indentation | yes |
| `instructions-ref` | `state.instructions` omitted when it is the question's instructions | yes |
| `history-tables` | history turns, history attempts and probe samples as `{"columns", "rows"}`, only when all rows share keys | yes |
| `same-as-raw` | `"="` for `effective_choice`/`executed_action`/`choice_id` equal to `raw_choice` | yes |
| `effect-ref` | the part of `effect_fact` the effect probe writes from the row's own fields becomes `"@"`; the surroundings tail is kept verbatim | yes |
| `empty-omitted` | a choice's empty `params` and `preconditions` omitted | yes |
| `labels-ref` | a choice label omitted when the question's criteria state it | `all` only |

**Exactness.** All 763 context payloads of the six night sessions round-trip under `format`, `default` and
`all`: 2,289 of 2,289 are canonically identical. `tests/test_state_codec.py` covers each rule, each guard
that withholds a rule, refusal of undecodable text, and the adapter path, including follow-up and batched
calls.

**Measured effect, billed-equivalent state tokens, header included:**

| | d1-ultra sessions, median | d1-ultra sessions, total | all six sessions, median |
|---|---:|---:|---:|
| format only | −22.0% | −22.3% | −21.8% |
| `default` | **−31.6%** | −31.1% | −36.0% |
| `all` (labels by reference) | **−43.1%** | −42.3% | −52.4% |

**With batching** (`--s1-pending-question batched`): a follow-up call bills the whole state again. Under the
billing fit, a batched question costs only its instructions plus one token per option (~90 tokens). Billed
tokens per turn:

| Session | Today | `default` | `default` + batched | `all` + batched | Paced floor, `all` + batched |
|---|---:|---:|---:|---:|---:|
| session-01 | 22,969 | −29.7% | **−42.2%** | **−50.4%** | 2.44 s |
| session-02 | 19,989 | −31.2% | **−46.4%** | **−56.4%** | 1.87 s |

This corrects the batch-pending note (`s1-opt-batch-pending.md`). That note charged batching ~2,600 tokens
per turn for the second question's option text, but option text is not billed. On d1-ultra, batching is a
net saving of about 18–22% on its own.

## 5. What is not done, and why

- **Labels by reference is not the default.** It is the largest remaining lossless lever (−11%). But
  billing suggests the option descriptions might not be rendered. If the hosted renderer does not show
  them, `labels-ref` would remove the only copy S1 sees. The open d1 code does render them.
- **Lossy candidates** (measured on compact text, so not offered as options):

  | Candidate | Saving | Cost |
  |---|---:|---|
  | 7 significant digits on floats | −1.4% | precision |
  | 4 significant digits | −2.2% | precision |
  | Drop superseded hypothesis versions (`guidance.earlier_versions`) | −4.7% | Could hide how a hypothesis was refuted (context-fidelity rule) |
  | Drop the raw effect objects, keep the sentence | about −4% | precision and provenance fields |
  | Smaller history windows | unmeasured | Policy, not packing |
  | A 320×240 view | about −2% (estimated from the ~450-token 640×480 view) | visual detail |

  Each changes what S1 can know, so each needs an ownership audit and a paired bench.
- **Diminishing lossless returns.** Guidance tables would add an estimated 1–2%. A fact-line table would need
  exact parsing of monitor prose. A directive-line rebuild is not lossless without a `reason` field
  (compaction review, option B).
- **The other arms.** Baseten's renderer is unknown and Meta has its own shape, so both refuse the option.

## 6. Risks

- **Format shift.** d1 likely saw indented JSON in training, and S1 will now read compact JSON with a
  header. The content is identical and the open code accepts string states. Whether choices move is
  unmeasured.
- **Billing model.** It is a fit to 196 calls of one game and one night. It is the best explanation of the
  numbers, not documentation.
- **The tokenizer.** It is d1-2B's. The 1.001 slope says it matches d1-ultra for these payloads.

## 7. The paid check that would settle it (needs approval and Liquid credit)

1. **Renderer probe, about 4 calls, under $0.01.** Use a state whose choices carry no labels, and ask a
   question that can only be answered from a criteria description. This decides whether `labels-ref` is
   safe. A second pair, with the same state as an object and as a string, confirms the billed difference
   live.
2. **Choice agreement, paired.** 40 frozen night contexts × {object state, `default`, `all`} × 5 repeats =
   600 calls at about 12–17k tokens. That is about $0.30–0.45 at $0.042 per million. Estimand: top-choice
   flips against the object-state arm, beyond its own repeat noise, plus acceptable mass on labelled frames.
3. **A live paired session**, `default` + batched against today's settings, measuring decisions per minute,
   token-budget waits and stale rejections. Same game, same objective, one Editor at a time.
