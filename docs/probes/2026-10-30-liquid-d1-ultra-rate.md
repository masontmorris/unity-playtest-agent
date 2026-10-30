# Liquid d1-ultra rate probe (30 October 2026)

Issue #164 (related #36). Operational throughput only: what one sequential,
unpaced replay stream achieved. No choice is scored, no game action is executed,
and no provider limit is claimed. Checkout `73218045`, arm `liquid-d1-ultra`
(resolved model `d1-ultra` on every call).

## Design (predeclared before spend)

- **Corpus.** The 108 S1 calls recorded in one earlier 90-turn session (90
  `next_action`, 18 `while_pending`), replayed in chronological recorded order
  and cycling from the start within the caps. Each body was rebuilt from its
  recorded context and exact recorded frame bytes; all 108 rebuilt hashes match
  the stored request hashes, all 90 context hashes match, and all 90 frames
  hash to the recorded view hashes. Bodies are byte-identical replays,
  including their original timestamps. Frozen hashes (trace / ordered
  requests / frames): `482665a1…`, `860ab6e2…`, `5f9530e2…`.
- **Caps, earliest wins.** First 429, auth/billing error, other failed or
  invalid reply, or missing usage/cost; 300 authorized calls; 180 s wall from
  first dispatch including any in-flight call (each call's timeout was the
  remaining wall time); $0.10 ceiling. No retries, no concurrency.
- **Budget.** $0.0006 reserved before every call through the shared ledger, so
  at most 166 calls fit under $0.10. The bound is empirical for this frozen
  corpus: its historical maximum was 9,677 input tokens / $0.000406434, about
  48% headroom. Estimated pricing under the recorded unverified-balance
  override; balances stayed unverified and no balance call was made. Approval
  was recorded before spend (user selection, 2026-10-30 15:11 UTC).
- **Transport.** Same canonical bytes and headers over one persistent `httpx`
  connection. Live S1 uses `urllib` per call. This persistent `httpx` transport
  is not the live `urllib` path, so probe latency and throughput are **not
  equal to** live-run throughput.

## Result

| Measure | Value |
|---|---:|
| Attempted / succeeded | 166 / 166 (one full 108-call cycle + 58) |
| HTTP statuses | 200 × 166; no 429 |
| Ended by | Effective call/budget cap (166 × $0.0006 = $0.0996 reserved) |
| Elapsed, first dispatch to last completion | 56.97 s |
| Input tokens (output 0 throughout) | 1,216,660 total; min 4,676, median 7,273, max 9,677 |
| Reported cost | $0.05109972 total; max $0.000406434 per call |
| Latency | median 297 ms, p95 453 ms, max 719 ms |
| Calls per minute, per-minute-normalized over 56.97 s | 174.8 |
| Tokens per minute, per-minute-normalized over 56.97 s | 1,281,392 |
| Full 60 s rolling tokens per minute | **none — no complete window** |

The probe hit its budget-derived call cap 3 s before a full minute elapsed.
The per-minute figures above are normalized burst rates: totals scaled to a
per-minute basis over 57 s. They are not directly observed counts in a
complete 60 s window and are not a sustained rate.

## Reading

- **Observed, not a limit.** 166 sequential calls completed without a 429.
  That shows at least this throughput was achieved in this window; with no
  rate-limit response, the provider's limit remains unknown.
- **No sustained-supported pacing value.** A pacing default needs a complete
  60 s window with headroom (the brief's rule: about 80% of the sustained
  window, rounded down). This run has 56.97 s, so it supports **no**
  sustained-rate number and, on its own, no measurement-backed lift of the
  current 85,000 tokens/minute. Any lift chosen now is a judgment call from a
  sub-minute observation (whose 57 s average is far above 85,000), not a
  sustained measurement; the smallest settling test is a separately approved
  probe that runs past 60 s.
- **Limits.** One sequential stream, one frozen corpus, one short window, a
  different HTTP transport from live S1, and static replay timestamps. Nothing
  here speaks to choice quality, concurrent load, or longer-run stability.

Raw per-call records, frames and traces stay local; this note is the compact
public record. No session was run as part of this probe.

## Follow-up: paced 100-call stream (30 October 2026)

Added 30 October 2026 by the follow-up implementation run, under a separate
approval recorded before spend in the authorizing conversation on
2026-10-30 15:32 UTC (user answered "Approve follow-up probe"; balances
remained unverified under the prior override, and no balance call was made).
This section does not change the first probe's record above.

- **Design (predeclared before spend).** Operational test of this one
  sequential paced stream only: not a provider limit and not gameplay or
  choice quality. The first 100 calls of the same frozen 108-call corpus, in
  chronological recorded order with no cycling; every rebuilt request, context
  and frame hash matched the original trace before sending. Frozen caps were
  100 calls, $0.06, and 180 s from first dispatch through final completion,
  with $0.0006 reserved per call before each send and start-to-start spacing
  of at least 1.0 s (no catch-up bursts; 100 dispatches therefore span at
  least 99 s). No retries. Primary metric: fully elapsed rolling 60 s tokens
  from completed successful calls, reporting the minimum and maximum eligible
  windows. Predeclared rule: any 429, non-200, invalid choice, missing usage
  or cost, cost over the reserved bound, or deadline error means report with
  no recommendation; only if all 100 succeed spanning at least 90 s is a value
  recommended, at 80% of the *minimum* full 60 s window rounded down to the
  nearest 10,000 tokens/minute. The parent makes the final operating-setting
  decision.
- **Transport.** Same persistent `httpx` transport as the first probe. It is
  not the live `urllib` path, so this throughput is not equal to live-run
  throughput.

| Measure | Value |
|---|---:|
| Attempted / succeeded | 100 / 100 |
| HTTP statuses | 200 × 100; no 429 |
| Resolved model | `d1-ultra` × 100 |
| Ended by | Call cap (100 × $0.0006 = $0.0600 reserved) |
| Elapsed, first dispatch to last completion | 111.67 s (dispatch span 111.09 s; minimum start-to-start gap 1.01 s) |
| Input tokens (output 0 throughout) | 761,017 total; min 4,676, median 7,801.5, max 9,474 |
| Reported cost | $0.031962714 total ($0.0600 reserved) |
| Latency | median 266 ms, p95 1,610 ms, max 5,422 ms |
| Full 60 s window, minimum eligible | 353,640 tokens/min |
| Full 60 s window, maximum eligible | 478,767 tokens/min |
| **Probe recommendation** | **280,000 tokens/min** (80% of the minimum window, rounded down to the nearest 10,000) |

- **Reading.** All 100 paced calls succeeded over more than 90 s, so the
  predeclared rule produces 280,000 tokens/min as the probe's recommendation
  for the parent's decision. It is headroom against every observed complete
  window in this one stream, not 80% of the best window. No rate-limit
  response was observed, so the provider's limit remains unknown. One
  sequential paced stream, one frozen corpus, a persistent `httpx` transport
  that is not the live path, and the latency tail above (p95 1,610 ms) limit
  this result. Nothing here speaks to choice quality, concurrent load, or
  longer-run stability, and no quality or improvement claim is made.

Raw per-call records, frames and traces for the follow-up also stay local.
No session was run as part of this follow-up.

## Operating decision

The orchestrator independently checked all 100 ledger rows, costs, dispatch spacing and
completion-aligned windows, reproducing the 280,000 recommendation. Adopt 280,000 tokens/minute
as the default for `liquid-d1-ultra` only. Other arms keep 85,000; explicit overrides still win.
This applies the predeclared rule to the measured workload. It does not establish the provider's
limit, live-session throughput or long-run stability. The next approved live session is a
development observation, not a controlled comparison: its objective also changes.
