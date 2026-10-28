# FPSKit preflight cache and stage 7

Issue #28. Two complete FPSKit preflights ran on FPSKit `25e43fb` with this checkout's bridge wired
in. The second hit the cache that the first sealed and made no model call. Stage 7 then ran twice
on the cache-hit manifest: the reset half verified both times, and the action-effect half stayed
inconclusive because the bridge did not reproduce the calibrated hold. Full records stay local
under `runs/preflight/issue28-fpskit-01/`, `issue28-fpskit-02/` and `runs/preflight/cache/`.

## Source preflight

The authored snapshots taken before the first inspection and after the manifest are identical:
1,027 files including the 189-file bridge package, SHA-256 `8061742f…a3a43e`. Discovery reported
312 fields and 62 objects with no error and no truncated cap. The IL scan recorded 55 issues
(51 indirect field addresses, 4 missing method bodies), so its write and issue caps are uncertain;
under the revised rules that evidence is hashed into the key rather than refused. Calibration
finished `measured_with_gaps`, and all 3 scene-reload trials restored the entity position.

The user approved up to 12 `muse-spark-1.3-contributor` calls with a $1.28 planning bound;
Contributor permits Meta to train on the FPSKit source excerpts and the screenshot. The first
chain accepted readers and actions on their first attempts, then rejected all four progress
attempts. The retry message "a implies rule leaves unless null" was read as a complaint that
`unless` was null: the model added `unless` to its other `implies` invariant too. That ledger used
6 calls, estimated at $0.0273, and is preserved. The message now reads "an implies rule does not
use unless; set it to null". The second chain accepted all three stages on the first attempt in
3 calls, estimated at $0.0025 because the provider served the repeated prompt from its cache.
In total 9 calls, an estimated **$0.0298**; the provider reported no cost.

The manifest has 16 readers, 10 actions and 2 skills, with no progress field and 3 invariants. Seven
actions are executable; three taps have no measured minimum hold, and neither skill can run on the
current bridge. The previous admission rules would have refused this selection. The model reported
5 unobservable, 5 uncontrollable and 4 unjudgeable gaps. The packet offered 256 fields and 8
controls, with 56 unreadable fields and 15 unsupported bindings recorded. The store command
revalidated the manifest against the accepted chain's ordered ledger and an exact replay against
the source packet, then sealed fingerprint `8bed9b9e…a96ef`.

## Second preflight

A new before snapshot matched the source's authored hash. New inventory, discovery, entity and
calibration (session `968b72b9…`) produced the same fingerprint, and the manifest command reported
a hit with **zero model calls**; it was given no budget and no API key. `playtest-validate-manifest`
reconstructed the source ledger and the replay and passed. Every declared reader, action, skill,
invariant and model gap is identical to the source; execution support, hold effects and
calibrations come from the new packet.

## Stage 7

The operator chose `walk_forward` (`w`, 1.2 s): the manifest's first action, with the most repeated
calibration trials (4, at 5.999 units) and so the tightest effect tolerance, 0.054 units.

| Run | Applied hold | Effect | Reload | Status |
|---|---|---|---|---|
| 1 | 1.2206 s | 6.087 units, cosine 1.000 | restored; 211 of 211 reload-stable fields matched | `different` |
| 2 | 1.2203 s | 6.089 units, cosine 1.000 | restored; 211 of 211 reload-stable fields matched | `inconclusive` |

Run 1 was judged `different` because 6.087 is 0.088 from the calibrated mean. The four matching
calibration holds were applied for 1.2000–1.2018 s, so the bridge held the key about 20 ms longer;
at the calibrated 5 units per second that accounts for the excess. After this observation the
comparison gained a predeclared input-reproduction gate: a hold more than 10 ms outside the matching
calibration trials' applied holds is a different input, and its effect is inconclusive. Under the
gate run 1 is also inconclusive. Run 2, the one rerun, overshot again by the same amount. Across all
96 valid calibration trials the median overshoot was 0.4 ms, so the stage 7 harness holds longer for
a reason not yet identified; the lease's measured duration spans the release call.

Both reloads restored the position exactly after about 6.09 units of movement. Six comparable
fields differed and were reported but not counted, because calibration's own reloads did not restore
them: CinemachineBrain `m_LastFrameUpdated`, the virtual camera's `ActivationId`, and the
controller's `m_ForceMag`, `m_Forces`, `m_JumpTTL` and `m_Move`. A reload sample is not full
game-state restoration; save/load and checkpoints were not exercised.

## Records

| Record | SHA-256 |
|---|---|
| Source manifest | `7ac37e6161253ec238baaeaabde376081d246d271f5377662e58146d3613ab3b` |
| Source ledger, rejected chain | `870d97881bd56af58edf1d8ae7b13be0bfa7b2ccd2f9011242c63150ab559d53` |
| Source ledger, accepted chain | `d371a4983d2523bca86a8d5c33dbd1b133c469536b03098ea741a9b558bc125a` |
| Cache entry | `32d9d6aa5ac7202203dbac72f2488d6d29168ed40e3246527c2fb3c87e853145` |
| Cache-hit manifest | `e7694bed9274ebb0bef52a2dc26cf6a58f4b728d011735427ee97387b2692fa7` |
| Cache-hit ledger | `4b0c18d05820b32efc50823b2d5b2283a7c475e19d3d475efb7886b2b98fea5f` |
| Stage 7 run 1 | `752e8d7b5bcb3c1b5747346da3e5456d55371f8af7128dc87218830f98fff846` |
| Stage 7 run 2 | `99f63c2f5bd8e95dbccc3e2282a249a98a4660d27ab3777ea56903fd77a8a000` |

These are local consistency records, not attestations from Unity or the provider. The screenshot is
the hash-checked FPSKit frame from a separate session that #27 used. No S1 turn ran, so adherence
and finding reproduction are not applicable.
