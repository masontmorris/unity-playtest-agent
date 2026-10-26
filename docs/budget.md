# Budget

Measured on 26 October 2026 ([probe write-up](probes/2026-10-26-day1.md)). Update this file when a measurement replaces an estimate.

At measured rates, with frontier calls at `reasoning_effort: minimal`, a 10-minute real-time run costs about **$0.15–0.35**, mostly frontier calls. The $150 of Meta credits covers roughly **400 such runs**. Frontier latency, not credits, limits how many escalations a run can make.

## Per-call cost

| Role | Model | Measured cost per call | Basis |
|---|---|---|---|
| S1 turn | Liquid d1.1 | $0.00003 small, $0.00006 with a frame, $0.00026 at 13k tokens | `usage.cost` returned by the API |
| S1 turn | mammoth-1 / elephant-1 | $0 during the Baseten free week (until about 28 Oct); unknown after | No published post-trial price; Baseten credits requested (#8) |
| Frontier call | Muse Spark 1.3, `minimal` effort (default) | $0.006–0.016 (median about $0.007) | Token usage × $1.25 / $0.15 cached / $4.25 per 1M |
| Frontier call | Muse Spark 1.3, `low` or default effort | $0.018–0.029 (median about $0.024) | Same |
| Clip in a frontier call | Muse Spark 1.3 | about $0.0013 per 4 s at 640×360, 10 fps | 1,056 extra input tokens |
| Preflight call, full project | Muse Spark 1.3 (Standard) | $0.037–0.043 for 26k input tokens | Measured tokens × list price, no cache |
| Preflight call, full project | `muse-spark-1.3-contributor` | $0.0028–0.0032 for the same tokens | Fixtures only: Meta may train on the prompts |

## Preflight per project

Stages 4–6 are three calls, and each re-sends the whole project: 26,401 input tokens for 26 scripts and 224 fields.

| Case | Three stages | Basis |
|---|---|---|
| Contributor, `minimal` | $0.0090, 33.5 s | Measured |
| Contributor, `low` | $0.0093, 50.3 s | Measured |
| Standard, `minimal`, no cache | $0.119 | Projected from measured tokens |
| Standard, `minimal`, full prefix cache | $0.029 | Projected; not observed at this size |

No call in the probe hit the prefix cache, so the no-cache case is the planning figure. Preflight runs once per project; the manifest cache (#28) is meant to make later runs free, and does not exist yet.

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
| Baseten | $100 | Requested, no response |
| Modal | $100 | Requested, no response |
| Spent so far | $0.276 | Day 1 probes: Meta $0.275, including $0.055 for the full-load preflight; Liquid $0.0011 |

## Rules

- Every paid sweep states its call count and cost before it starts, and the user approves it.
- Results are written per call as they arrive.
- Check the balance before a sweep instead of discovering it from an HTTP 402.
