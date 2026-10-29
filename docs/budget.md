# Budget

Measured on 26 October 2026 ([probe write-up](probes/2026-10-26-day1.md)). Update this file when a measurement replaces an estimate.

At measured rates, with frontier calls at `reasoning_effort: minimal`, a 10-minute real-time run costs about **$0.15–0.35**, mostly frontier calls. The $150 of Meta credits covers roughly **400 such runs**. Frontier latency, not credits, limits how many escalations a run can make.

## Per-call cost

| Role | Model | Measured cost per call | Basis |
|---|---|---|---|
| S1 turn | Liquid d1.1 | $0.00003 small, $0.00006 with a frame, $0.00026 at 13k tokens | `usage.cost` returned by the API |
| S1 turn | mammoth-1 / elephant-1 | $0 during the Baseten free week (until about 28 Oct); unknown after | No published post-trial price; no notice of Baseten credits (#8) |
| Frontier call | Muse Spark 1.3, `minimal` effort (default) | $0.006–0.016 (median about $0.007); $0.0066 and $0.0087 with the #73 candidate schema | Token usage × $1.25 / $0.15 cached / $4.25 per 1M |
| Frontier call | Muse Spark 1.3, `low` or default effort | $0.018–0.029 (median about $0.024) | Same |
| Clip in a frontier call | Muse Spark 1.3 | about $0.0013 per 4 s at 640×360, 10 fps | 1,056 extra input tokens |
| Preflight call, full project | Muse Spark 1.3 (Standard) | $0.037–0.043 for 26k input tokens; $0.011–0.013 for a later stage that reuses the cached prefix | Measured tokens × list price (#70); one schema per chain (#73) |
| Preflight call, full project | `muse-spark-1.3-contributor` | $0.0028–0.0032 for the same tokens | Fixtures only: Meta may train on the prompts |

## Preflight per project

Stages 4–6 are three calls, and each re-sends the whole project. The early probes below sent 26,401 input tokens (26 scripts, 224 fields). A full FPSKit packet with discovery and calibration is far larger: 232,662 prompt tokens per call in #28 and about 243k after #115, mostly calibration evidence. Plan with the measured row for the current packet, not the probe rows.

| Case | Three stages | Basis |
|---|---|---|
| Contributor, `minimal` | $0.0090, 33.5 s | Measured |
| Contributor, `low` | $0.0093, 50.3 s | Measured |
| Standard, `minimal`, no cache | $0.119 | Projected from measured tokens; the planning figure |
| Standard, `minimal`, one schema for the chain (#73) | **$0.063**, 29.4 s | Measured once: stages 5 and 6 reused the 28k prefix. $0.124 if neither transition hits |
| Standard, `minimal`, all stages in one call (#73) | $0.047 | Projected from two contributor calls; needs no cache |
| Standard, `minimal`, all stage input cached | $0.029 | Projected; not measured |
| Standard, `minimal`, unified schema, full FPSKit packet after #115 (about 243k tokens per call) | **$0.394**, 34 s | Measured once (#116): 3 calls, 66% of prompt tokens cached (stages 5 and 6 hit). About $0.92 if no call hits the cache |

With one byte-identical schema for every stage (`roles.preflight.StageAnswer`), every stage transition and retry reused the prefix: 8 of 8 calls across two contributor chains and the first Standard chain (#73). Per-stage schemas lost it again in the same session. Misses still happen, though: the Standard stage-4 exact repeat reported 0. For the probe-sized packet the planning figure stays at **$0.12** as the bound, and **about $0.06** is the expected cost with the unified schema. For a full FPSKit packet after #115, plan **about $0.39** per chain, and about $0.92 as the bound with no cache hit. The manifest cache (#28) is a design proposal for avoiding a repeat preflight, not implemented or measured.

## A 10-minute real-time run

| Item | Count | Cost |
|---|---|---|
| S1 turns (0.7–1.6 decisions/s) | 400–1,000 | $0.02–0.06 on d1.1 with frames; $0 on Baseten during the trial |
| Frontier calls at `minimal` | ≤ 30 | ≤ $0.30 (at most $0.48 if every call carries a large packet) |
| **Total** | | **about $0.15–0.35** |

The first run on a new project also pays the one-off preflight above.

The frontier count is set by latency, not policy. With one pending request at a time and about 6 s per `minimal` call, a 10-minute run could make up to about 100 calls back to back. 30 is a planning figure, because S1 decides when to escalate. Escalation frequency stays S1's choice (law 2). Spend is watched, never capped by suppressing escalation.

## Credits

| Source | Amount | Status (26 Oct) |
|---|---|---|
| Meta Model API | $150 | Received |
| Baseten | $100 | Requested (#8). No refusal: a representative said the final wave was being sent on 27 Oct, and no notice has arrived |
| Modal | $100 | Received (27 Oct) |
| Spent so far | $0.508 | Day 1 probes: Meta $0.507, including $0.055 for the full-load preflight, $0.078 for #70 (99 calls) and $0.154 for #73 (98 calls); Liquid $0.0011 |

## Rules

- Every paid sweep states its call count and cost before it starts, and the user approves it.
- Results are written per call as they arrive.
- Check the balance before a sweep instead of discovering it from an HTTP 402.
