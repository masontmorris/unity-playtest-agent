# FPSKit S1 bench: Mammoth shuffled re-test

On 27 October 2026 `mammoth-1` was replayed over a new FPSKit recording that removes three
regularities of the [first corpus](2026-10-27-fpskit-s1-bench.md): controls held in id order, `c1`
always listed first, and no decision after the last check. It scored **45/45** acceptable. This is a
robustness check of one arm, not a comparison. The labels are an assistant draft that Mason Morris
reviewed the same day without changing any (see the [first replay's label
review](2026-10-27-fpskit-s1-bench.md#label-review)). The design and the reading rule were
predeclared in `docs/design/s1-bench.md` before any call on the new frames.

## Result

| | First replay | Shuffled re-test |
|---|---:|---:|
| Frames | 40 (5 passes × 8 checks) | 45 (5 passes × 8 checks, plus a terminal decision each) |
| Acceptable top choice | 40/40 | **45/45** (checks 40/40, terminal 5/5) |
| Acceptable mass, mean / minimum | 0.981 / 0.927 | 0.986 / 0.961 |
| Same request sent twice | identical on 10/10 frames | identical on 5/5 frames |
| Median latency | 250 ms | 266 ms (slowest 1,188 ms) |

The columns use different frames and are not paired. There were no misses and no unknowns, and no pick
had under 0.6 acceptable mass. All five terminal frames chose `finish`. Mammoth never repeated a
completed control, and 37 of its 40 check picks were the lowest-numbered unchecked id; the other three
were also unchecked.

## What the new corpus can tell apart

Shortcuts that never look at which controls are already done, scored against the same rule:

| Shortcut | First corpus | This corpus |
|---|---:|---:|
| Take the id after the last completed one | 40/40 | 22/40 |
| Count the completed facts and take the next id | 40/40 | 27/40 |
| Always `c1` | 5/40 | 17/40 |
| Take the lowest unchecked id (needs the set) | 40/40 | 40/40 |
| Never say `finish` | not tested | misses all 5 terminal frames |

So 40/40 in the first replay was consistent with a counting shortcut, and 40/40 on the check frames
here is not. The shuffle is gentler on an `always c1` habit (17/40 against 5/40), so it is a weaker
probe of that one.

## What ran

- **Recording:** FPSKit `25e43fb`, Unity `6000.5.4f1`, one scene load, 45 frames, input release
  verified. The 40 holds measured 1.1994 to 1.2116 s. The entity, calibration, instructions and
  objective hashes match the first corpus, as do the changed-file hashes of the game's manifest,
  lockfile and `TimeManager.asset`.
- **Shuffle:** seed 2410. Each pass has its own id map and its own hold order (`protocol.passes` in
  the corpus), so the control behind `c1` changes between passes. Only 1 of the 35 check frames with a
  completed check has a completed set that is a prefix of the id order; in the first corpus all 35 did.
- **Labels:** `benchmarks/fpskit/labels-shuffled.json` accepts any unchecked control on a check frame
  and `finish` on a terminal frame. It is derived from the recorded facts and cross-checked against the
  recorded executions. No pick sits on the contested ruling that repeating a completed check is a
  miss, or on `wait`, so a reviewer would have to reject "choose an unchecked control" or "finish when
  every control is checked" to change a score. The review approved both, and no hold here needed a
  separate ruling (every hold was within 0.07 units of calibration). The reviewed copy is
  `benchmarks/fpskit/labels-shuffled-reviewed.json`. `retest.py` scores only the labels its run pins,
  so the copy was compared with the draft instead: all 45 accept-lists are identical, and re-scoring
  the run gives the same result, apart from the 16th significant digit of some acceptable-mass sums.
- **Calls:** `decision-models/baseten-mammoth-27b-latest` (the alias came back unchanged; no immutable
  revision), one request per frame and a second request on five frames drawn by seed.

## An interrupted run

The first run, `runs/bench/issue24-retest-01`, stopped at request 41 of 50. That call (`pass-02-s2`)
ended in a transport error after 21.4 s with no HTTP response, so whether Baseten processed or billed
it is unknown. The runner stops on any failed call and never retries. The user was asked and chose to
resume the nine unattempted requests and retry the failed frame. The continuation,
`runs/bench/issue24-retest-02`, holds the 40 answered rows copied byte for byte plus 10 new ones, with a
`reconciliation.json` recording the error and the copy. The failed row stays in the first ledger.
`retest.py` was not edited between the runs.

In total **51 requests were sent, 50 were answered and 1 failed with an unknown outcome**, against 50
predeclared. Baseten returns no cost field, so the billed cost is unknown; the free week was advertised
to about 28 October. The reserved bound is $0.00016 a request, $0.00816 for 51, which is a reservation
and not a bill. The balance was not verified; the user directed the run.

## Limits

- It is still a bookkeeping task on one game, one recording and one machine, text only. 45/45 shows
  Mammoth tracks which controls are done and ends the checklist. It does not show it plays well,
  notices bugs, or asks for help when it should.
- The five passes share a recording, so no interval is claimed. One seed tests one set of permutations.
- Only Mammoth ran, so nothing here ranks it against `elephant-1` or `liquid-d1.1`.
- Latency comes from a clock that steps in about 15.6 ms. Calls were slowest at the start of each
  batch (9 of 50 over 500 ms, up to 1,188 ms), which looks like warm-up but was not tested. That tail
  matters against the 0.7–1.6 decisions a second in `docs/budget.md`.

## Inputs and provenance

- Corpus `runs/bench/issue24-fpskit-04/frames.json`, SHA-256
  `b84524b51a8aed22dc55c680734f136be05be9e03ef8e3b24e44ee27680d4bf1`.
- Draft labels SHA-256 `b067d24eb4dd02bd5850b40af475fc92fa124725993a40d1d6c557a20112c0d6`; reviewed
  copy `4ecb214602c2224aae4cccf9105199d82304641fbfccf1b301b28858b19df296`.
- Approvals: first `3664f83350f97a9be63433f57e74ba8a0ed27fd295d9bcfd21c4b8238b8b92fe`, continuation
  `db0abd3c37317ed24d90e1f59c0722d1a0397618e4fdd3eacd10f4f0df6ffdbb`.
- Plans: first `381bc693c7f605ed165cab1f010a67a3a66223e974af82bb4911fc96673f5bdb`, continuation
  `03478070bbb373348dd0e2e6231ce3bb04125b1fb88f2f53a1836f093318e40d`.
- Reconciliation `e011efcda0cc976346273a9b89997056d10c4833db9ea2a4c50557aba1dcee9c`; report
  `347741d5d90eeeb064d7f15eeb101c2a790b8319ee4d1f3dce1455f44b503db5`.
- Source: `retest.py` `a812fa23f2c98591c9db2803533875f432507520d7500c7e4da2b09ccc1f9e37`, `record.py`
  `c4e863281b32291a97725419f18ba168f918a34b7a4e987b4b7e212cf2b7a7a5`, and `replay.py`
  `95f34cba71aba935f79be37872239faf474f68be28593b7cc4cdb52bc846b99f`, unchanged from the first replay.

The corpus, both ledgers and the report stay local under `runs/bench/`.
