# d1-ultra: does conditional finish wording stop premature `finish`? (31 October 2026)

**Verdict, by the rule declared before any call: works.** The finish-relevant units where `finish` won a
majority of repeats fell from 4 of 11 to 1 of 11. The share of calls choosing `finish` fell from 32.7% to
20.0%. Normal units kept their choices: per-call agreement was 0.88, against the current wording's 0.92
leave-one-out self-agreement, within the 10-point limit. The wording reduces premature finishing but does
not remove it.

## Design (`runs/s1-finish-wording/plan.json`, written before the first call)

- **Units:** 16 frozen `next_action` turns from today's FPSKit trials, each with its exact JPEG.
  - 11 finish-relevant: turns where `finish` won or came close.
  - 5 normal turns.
- **Gate:** every rebuilt `current` request matched the trial's recorded `request_sha256` (16/16).
- **Arms:**
  - `current`: the request the trial sent.
  - `conditional`: the same payload with the instructions variant `policy/2` and the finish choice labelled
    `FINISH_LABELS["conditional"]`, encoded the way that trial encoded it.

  `policy/2` drops "or you judge no useful attempt remains". The new sentence reads "Finish only when the
  objective's completion conditions are met; otherwise keep playing until the session ends by itself."
- **Repeats:** 5 per arm and unit, in a seeded order; 160 calls.
- **Rule:**
  - A unit finishes when most of its repeats choose `finish`.
  - The result is inconclusive if `current` finishes on fewer than 3 units.
  - "Disturbs other choices" if normal-unit agreement moves more than 10 points.
  - "Works" if `conditional` finishes on at most 2 units.
  - Otherwise "no effect".

## Results (160 calls, 160 OK)

| Arm | `finish` share on finish-relevant calls | Units finishing (majority) | Mean P(finish) | Normal units: agreement with the current modal choice | Cost | p50 latency |
|---|---:|---:|---:|---:|---:|---:|
| current | 32.7% | 4 / 11 | 0.152 | 0.92 (self, leave-one-out) | $0.0352 | 312 ms |
| conditional | **20.0%** | **1 / 11** | 0.130 | 0.88 | $0.0353 | 297 ms |

Total $0.0705. The script is `scripts/probes/probe_s1_finish_wording.py` (Terra wrote it; the
orchestrator fixed the reading so a unit counts as finishing by majority, and so that `current` must
reproduce enough finishes before "works" can be read). These latencies ran over the new pooled HTTPS
connection. They are lower than the morning bench's 450–530 ms, on somewhat smaller requests.

## Limits

- 16 units from one game and one afternoon.
- The wording lowers the probability of `finish` only a little (0.152 → 0.130). It works by tipping
  near-ties, not by removing the option.
- In a live session, finishing stays S1's choice. Nothing here blocks it.
