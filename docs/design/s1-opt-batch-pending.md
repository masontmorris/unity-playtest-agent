# Conditional pending question batching

`pending_question="batched"` asks the pending-input question alongside the primary question when a real-time choice set offers both help and pending choices. The prompt says “If help is requested on this turn” because the questions are independent. Only a valid help primary makes the secondary applicable; otherwise its recorded condition is not met. An absent or invalid secondary stays absent, allowing the loop's existing recorded wait default. One HTTP request has one ledger reservation, exact wire-body hash, primary answer and recorded conditional answer. Meta Muse Spark S1 is rejected because its request has no questions map. The default `follow_up` mode is unchanged.

## Trade and local measurements

This trades a rare sequential round trip for an extra question on every eligible turn. Local `runs/night` traces were read only, counting `context` records as turns and `s1_call` question fields as calls; p90 is nearest rank, milliseconds. A missing percentile means no sample. Some contexts did not produce a primary call, so call counts need not match contexts.

| Session | Follow-ups / contexts | Primary p50 / p90 ms | Follow-up p50 / p90 ms |
|---|---:|---:|---:|
| session-01 | 23/97 (23.7%) | 594 / 922 | 532 / 688 |
| session-02 | 16/63 (25.4%) | 562 / 719 | 547 / 734 |
| session-03 | 0/151 (0%) | 1507.5 / 2125 | unknown |
| session-04 | 21/130 (16.2%) | 1750 / 2547 | 2266 / 3360 |
| session-05 | 0/121 (0%) | 1609.5 / 2219 | unknown |
| session-06 | 0/101 (0%) | 1625 / 2375 | unknown |
| b01-elephant-01 | 1/94 (1.1%) | 469 / 515 | 469 / 469 (one sample) |

Per-context mean pending criteria was 5,993–6,617 JSON characters; conditional instructions add about 350 characters. At the adapter's 2.5 characters/token approximation this is roughly 2,540–2,790 extra question tokens per eligible turn, excluding small envelope overhead. Mean state was 30,022–50,917 characters (roughly 12,009–20,367 tokens). With shared prefix counted once, the token break-even help-turn rate is approximately Q/(S+Q), or 11–19% across these session averages, assuming the separate follow-up re-sends state and question. If the provider re-prefills the state for each question even in a batch, the extra per eligible turn is S+Q, and token break-even is 100%. This is a token-cost approximation, not a latency break-even: provider batching latency and Liquid's billing behavior are unmeasured. Images and question envelope overhead are excluded. Eligibility varies; rates above use all contexts, not just eligible turns.

Conditional framing may alter answer quality: the model selects a hypothetical pending action before it knows whether help was selected. This is a hypothesis, not an observed effect. Independent questions could also yield combinations that would differ from a sequential answer. Batching can increase latency on ordinary turns, and raises the call's token/cost bound; neither is measured here.

## Paid bench design, not run

Estimand: paired difference in per-turn latency and input tokens, and agreement/quality of pending choices on help turns, for the same frozen eligible contexts. Compare default sequential against batched d1-ultra, randomize arm order, use a frozen corpus of 100 eligible contexts with 5 independent repeats per arm. A pair is one context/repeat; score primary validity and exact primary agreement, secondary validity and exact agreement conditional on help, plus blinded human appropriateness ratings; report help and non-help strata and uncertainty clustered by context. Compare end-to-end latency including sequential calls, provider-reported tokens and missing usage separately; don't interpret agreement as correctness. Maximum 1,500 calls (500 batched, 500 primary, up to 500 follow-ups), fewer if help is not selected. Estimated input cost at $0.042 per million is approximately $0.95–$1.46 at the above state/question ranges and maximum calls, excluding images and output charges; actual provider billing and usage are unknown. Obtain explicit budget approval before running, freeze corpus and scoring beforehand, and report noise and invalid responses without repair.

Ownership audit: ordinary primary -> execute primary; help primary + valid secondary -> apply secondary; help + invalid/missing secondary -> absent and loop policy default; invalid primary -> reject. Conditional answers on non-help turns are recorded as condition not met. Runtime trace replay and production provider behavior for this new mode remain unverified; no model call was made.
