# FPSKit S1 bench: first replay

The first replay completed all 270 requests on 27 October 2026: 50 noise repeats and 40
scored frames for each of three models. These results are **preliminary** because Codex drafted
the accept-lists and human review is pending. They measure a control-check task in one FPSKit
recording, not live play or bug discovery.

## Result against the assistant draft

| Model | Acceptable top choice | Adjacent repeat flips | Median / p95 sweep latency |
|---|---:|---:|---:|
| Baseten mammoth-1 | 40/40 (100%) | 0/40 | 250 / 266 ms |
| Baseten elephant-1 | 30/40 (75%) | 0/40 | 172 / 203 ms |
| Liquid d1.1 | 21/40 (52.5%) | 14/40 (35%) | 203 / 250 ms |

The noise figures compare consecutive top choices across five repeats of each of ten frames; they
are not a percentage of the 50 calls. All 40 sweep responses per arm were valid and scored. Against
mammoth-1 on the same 40 frames, elephant-1 had 0 wins, 10 losses and 30 ties; d1.1 had 0 wins,
19 losses and 21 ties. Among elephant's ten unacceptable selections, nine reselected `c1` and one
reselected `c2`. Among d1.1's 19, 16 reselected `c1` and three selected `finish` before the
checklist was complete, according to the draft rubric.

The five passes are correlated checklists within one recording; no independent-sampling confidence
interval is claimed. The d1.1 repeat variation is substantial. The results should be revisited after
human label review and do not establish model quality on other tasks, games or live play. The API
returned the requested model aliases; an immutable hosted model revision was not established.

## Cost and run conditions

Liquid returned a cost for all 90 of its requests, totalling **$0.00323478** including noise and
sweep. Its largest reported single-call cost was $0.00003952. Baseten returned no cost field for
its 180 requests, so their billed cost is unknown in the local ledger; the provider advertised a
[free trial week](https://www.baseten.co/blog/the-best-decision-models-in-the-world-mammoth-1-and-elephant-1/)
for both models. All 270 calls returned HTTP 200 with usable choices, without retries.

The user approved up to 320 calls and $0.05, then explicitly directed this run after being told
provider balances and the specific Liquid d1.1 price were unverified. The ledger reserved $0.00016
per call and recorded those conditions in its approval and plan. The estimate was based on previous
d1.1 API-reported usage; a reservation is not a bill or a provider-enforced spending cap.

## Inputs and provenance

- Corpus: 40 pre-hold frames from FPSKit commit `25e43fb`, Unity `6000.5.4f1`, one recording with
  five separate control-check passes. Requested holds were 1.2 seconds; measured holds ranged
  from 1.199311 to 1.366949 seconds, median 1.200553 seconds.
- Scoring: an unchecked offered control was acceptable; `wait` and `finish` were unacceptable while
  the checklist remained incomplete. Labels are assistant-drafted with `human_review: pending`.
- The raw corpus, per-call ledger, approval, plan and report remain local under
  `runs/bench/issue24-fpskit-03/` and `runs/bench/issue24-replay-01/`. The human review guide is
  `runs/bench/issue24-fpskit-03-review/draft-review.md`.
- Corpus SHA-256: `3bdd5ff6ac026a739e40d56369ab7c199e702192fdd613d335ccaf7c89a698f6`.
- Draft labels SHA-256: `9a43e01cff6214e891b2ea3d96f440799af5becdacf802cf7a05b3feedb74c45`.
- Replay source SHA-256: `95f34cba71aba935f79be37872239faf474f68be28593b7cc4cdb52bc846b99f`.
- Plan SHA-256: `447710eccdd3429858dbe51b7f6ee9aafe98525d8e08c0f50d6e54a552332339`.
- Report SHA-256: `e4e736ea03ffb956dddb3d4c2ae61955b200d86df1b6dde4f70c9fd781b8616a`.

A later human-reviewed revision can be scored offline against the original calls, with both label
hashes and the reviewer recorded. A post-hoc review remains distinct from this frozen-draft result.
