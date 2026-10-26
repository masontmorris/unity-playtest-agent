---
name: playtest-evidence-review
description: Review unity-playtest-agent run, probe, capture, or report evidence and decide what it supports. Use when checking a trace, run record, finding promotion, reproduction k/n, directive outcome, image or clip provenance, especially before a commit or report claim; this is a read-only finding-state review, not a live trial.
---

# Evidence review

Paths are relative to the target repository root, the checkout containing `src/playtest/contracts/`. Do not infer the root from where this skill is installed.

Implementation notes describe `879f9129`; verify component availability in the target checkout before applying them.

## Scope

Read-only review of evidence. It does not run a trial or probe, open Unity, or create a report. At this snapshot there is no runtime turn loop, bridge, report CLI or live-trial evidence; probe artifacts and contract-level records exist. Say which is reviewed. The deliverable is a note; do not edit records.

## Binding rules

- A claim needs evidence of the right kind and scope. Missing metadata limits that claim, not every claim about the artifact.
- Missing evidence is `unknown`/`null`, never zero, success or failure; an observation is not a hypothesis.
- `fix_verified` is not "any rerun passed": retest the same reproduced defect under comparable reset and clock conditions, failures retained.
- Keep full traces and media local. Compact public records omit secrets, local absolute paths and third-party assets. A perceptual judge must not see issued actions, directions, S1's account or intended outcome.

## Procedure

1. Inventory by type: static probe — commit, model/effort, calls, cost, schema; runtime turn — `RunRecord`, sequence, clock/reset, adherence; image or clip — source, consumer, time, size/orientation, hash. A two-call static probe without Unity, reset or `RunRecord` still supports static cost/schema claims; missing scene/reset limits runtime claims, not those. Record producer, commits, manifest fingerprint, input mode, models/resolved, prompt hashes, objective, budget, reset.

2. Check the sequence: stable and append-only; gaps, duplicates or reordering are defects; clock consistent; artifacts exist; compaction retains unresolved hypotheses, negatives, contradictions, conditions and failures. Matrix: raw/effective/executed agree except rejected-invalid (`effective_choice` null, no input) and help `while_pending` continue; wait sends none; stale rejection authorises nothing different. Run `check_adherence()` on runtime turns, else report not applicable.

3. Unknown vs absent: a missing reader is `null` plus `reader_errors`, not zero; no attempt is not 0/n. Current unknowns: wire and effect values are `null`; `RunRecord` uses `UNKNOWN`, currently the lowercase string `"unknown"`; no universal uppercase convention.

4. Directives: target, expected, tolerance, game-time deadline; `expected` null means `unknown`; `error_class` only for `not_met`; every `evidence_seq` exists; no setpoint is unscorable.

5. Findings: states are suspicion, candidate, repro_attempted, reproduced, unconfirmed, fix_proposed, fix_verified. Promotion past suspicion cites sequence numbers. `reproduced` needs `Repro` attempts/successes/clock/reset, `reproduced <= attempts`, at least one success, tolerance if used. `fix_verified` retests the same reproduced defect under comparable reset and clock, retains failures; another rerun or "fixed" is not verification. An explanation is not a cause.

6. Media: record source and consumer before pixels: adapter/preflight, S1 frame, frontier or judge clip, report; camera versus composite. Record sequence, game and wall time, size, orientation, hash. A render-defect claim needs an independent capture path. A judge must not see issued actions, directions, S1's account or intended outcome; check echo bias with a known clip.

7. Note: claim, sequence numbers, counterevidence, unknowns, failures, k/n, cost, uncertainty; label underpowered inconclusive. A record change is a separate, validated change.

## Outcome and stop

Verdict is `supported`, `partially supported`, `unsupported` or `not applicable`; "looks fine" is not a review. Include inventory, matrix or NA, directive outcomes, finding state, k/n and unknowns. If unsupported or unknown, name the missing evidence and the smallest check that would settle it.

Limit the affected claim if its required record or cited sequence numbers are missing; a reproduction has no reset or zero successes; `met`/`not_met` lacks `expected`; `fix_verified` uses a different rerun; or compaction erases failures. Scrub secrets and local absolute paths and exclude third-party assets before publishing the note. Do not claim runtime evidence that the reviewed checkout and artifacts cannot produce. Scale the note.
