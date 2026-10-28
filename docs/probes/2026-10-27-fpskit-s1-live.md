# FPSKit S1 bench: live loop smoke test

On 27 October 2026 `mammoth-1` drove FPSKit through the production `TurnLoop` in real time for three
short episodes. Every episode finished with all eight controls checked once and then `finish`. This is a
smoke test of expected use, not a benchmark, and it is **preliminary**: the facts are stand-ins, the
checklist objective is the replays', and the sample is 30 decisions. The design was predeclared in
`docs/design/s1-bench.md` before any call.

## Result

A miniature of the Day 4 gate in the build bible ("no crashes, 100% adherence, no code-selected actions,
every action with expected vs observed effect"):

| Check | Result |
|---|---|
| No crashes, inputs released | 3 of 3 episodes complete; the stop released and verified input; the game's files were unchanged |
| 100% adherence | 30 of 30 turns, 0 violations from `check_adherence` |
| No code-selected actions | every executed action was S1's raw choice; the harness chose nothing |
| Expected vs observed effect | 24 of 24 holds have both; the largest gap is 0.08 world units (6.04 calibrated, 6.12 measured) |

| | Episode 1 | Episode 2 | Episode 3 |
|---|---:|---:|---:|
| Turns | 10 | 10 | 10 |
| Controls checked, repeats | 8, 0 | 8, 0 | 8, 0 |
| Finished after all eight | yes | yes | yes |
| Stale rejections | 1 | 1 | 1 |
| Acceptable decisions | 10/10 | 10/10 | 10/10 |

Across the 30 decisions, acceptable mass averaged 0.984 (minimum 0.958) and no hit was under 0.6. There
was no `wait`, `escalate` or `delegate` turn and no invalid answer, with all 12 choices on offer.

## What the live loop showed that the replays could not

- **Staleness is real at this model latency.** 3 of 27 action attempts (11%) were rejected as
  `stale_age`, and the loop sent no input for them. They were the only action turns where the model took
  0.5 s or longer (1,109 ms, 500 ms and 609 ms); no call under 0.5 s went stale. Each time Mammoth re-picked
  the same control on the next turn, after the fact line reported the rejection. The 0.5 s window is the
  loop's default, so either the window, the model latency or per-action freshness has to give.
- **The cycle is dominated by the hold.** An executed turn took a median of 1.46 s: a 1.2 s hold plus a
  0.27 s model call. That is 0.68 decisions a second against the 0.7–1.6 in `docs/budget.md`, or about 410
  calls in a 10-minute run. Deciding during the hold, which the loop does not do yet, is what would raise it.
- **The replay result carried over.** The replays' mechanical rule scored 30/30, with no re-check and no
  premature `finish`, now with the executed input and the next observation in the loop.
- **Observation age is small.** Median 1 ms at decision time (maximum 36 ms), because the model call
  follows the observation directly. The age that matters is the one the bridge sees when the action arrives.

## What is real and what is a stand-in

- **Real:** `TurnLoop` with its defaults (0.5 s window, execution controller, answer validation that never
  repairs), real-time mode, the bridge, `build_context`, `check_adherence`, and a choice set with `wait`,
  `escalate`, `delegate` and `finish` beside the eight controls. Holds were 1.2 s leases. Each episode drew a
  fresh seeded id map.
- **Stand-ins:** the facts (no facts builder exists, so the harness wrote them from measured results in
  the replays' wording, plus one line for a stale, failed or invalid turn); the `escalate` and `delegate`
  wording; the S1 adapter (one blocking call a turn); and the checklist objective. No frontier model ran, so
  nothing could answer an escalation.

## Limits

- 30 decisions, one game, one machine, text only, and the checklist objective. It shows the integrated loop
  works with this model, not that Mammoth plays well or notices bugs.
- No `escalate`, `delegate` or `wait` was chosen, so the help path, `pending_help` and `while_pending` were
  not exercised against a live model.
- The three episodes share one session and one game state, so they are not independent.
- The action catalog declares no dependent fields, so only age could make an action stale. Per-action
  freshness by field (`stale_fields`) was not tested.
- Latency comes from a clock that steps in about 15.6 ms, which is why two values read as 500 ms.

## Cost and accounting

30 requests, all HTTP 200, no retries, against a cap of 48. Baseten returns no cost field, so the billed cost
is unknown; the free week was advertised to about 28 October. The reserved bound is $0.00016 a request,
$0.0048 for 30, which is a reservation and not a bill. The balance was not verified. The API key appears in
none of the retained files.

## Inputs and provenance

- FPSKit `25e43fb`, Unity `6000.5.4f1`; the entity and calibration hashes match the earlier corpora.
- Approval SHA-256 `f41e5573e14a9f23e7818acd03ac0d48fefb0f0a9168d964419ef6b8b261ad62`; plan
  `45ad3aea4c842af89d1600eeeefd96bb8b59debb5dab8f6282e9749a872aa146`; trace
  `43cdf80e3b27ab8e39bfb65c34275bfa2731b7772039f2464c50f4d33125db19`.
- Source: `live.py` `f45c3a68553d1d6e841d7205631e62b2caba98bc92a210f04365c5299b0fb36c`, `record.py`
  `c4e863281b32291a97725419f18ba168f918a34b7a4e987b4b7e212cf2b7a7a5`, and `replay.py`
  `95f34cba71aba935f79be37872239faf474f68be28593b7cc4cdb52bc846b99f`, unchanged from the earlier runs.

The trace, plan and per-call ledger stay local under `runs/bench/issue24-live-01/`.
