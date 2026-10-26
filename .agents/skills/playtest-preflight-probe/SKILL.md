---
name: playtest-preflight-probe
description: Run, extend, or interpret the current paid preflight probes under scripts/probes, or review a future manifest and cache design. Use for probe budget, discovery and selection analysis, invariant and semantic checks, invalid rows, or gaps; this is not the production preflight and a probe never produces a manifest.
---

# Preflight probe and manifest review

Paths are relative to the target repository root, the checkout containing `scripts/probes/`. Do not infer the root or the game project from where this skill is installed.

Implementation notes describe `879f9129`; verify component availability in the target checkout before applying them.

## Scope and non-claims

These are Day-1 probes; `scripts/probes/common.py` says they are not part of the product. No production preflight package, CLI, manifest writer, cache or bridge exists. A probe is not stage 0–3 measurement, a validated manifest, or product evidence. Static results — schema, reference, semantics, choices, gaps, latency, cost — are analysable now; runtime effects are `unknown` without measurement, which does not block that analysis.

## Commands and budget

```sh
uv run --extra probe python scripts/probes/probe_preflight.py <unity-project> <script.cs>...
uv run --extra probe python scripts/probes/probe_preflight_full.py <exported-project-dir> [--model M]
```

The first sends two calls, `minimal` then `low`, with `muse-spark-1.3` and a regex field stand-in. The second defaults to `muse-spark-1.3-contributor`: readers, actions and progress at `minimal` and `low`, then one Standard readers call unless `--model muse-spark-1.3` — seven calls by default, six with Standard. Both need `META_API_KEY` from the environment or repo `.env`; never print its contents or headers.

Historical baselines, not approval: the two-call probe measured $0.021 and $0.025; the full default chain about $0.055 (`docs/probes/2026-10-26-day1.md`; prices in `docs/budget.md`). For the current project, estimate scripts/fields, tokens, stages, calls, retries/repeats, model tier, effort and cache; state the envelope and get approval before any paid call. Check the balance first.

## Procedure

1. Choose the smallest sufficient probe: two calls for a few scripts, the full chain only for whole-project discovery. They are file-only: an unrelated trial or open Editor does not block them, and there is no new Unity gate. Block only if project files are being edited or Play mode is writing them, making provenance ambiguous.
2. Gate every `calls.jsonl` row: status, non-null `problem`, schema validation, resolved model, usage, `est_cost_usd`, latency, and every chosen id in discovery. An invalid row is a finding: preserve it and finish analysing the rest. Stop dependent spend, use or claim, not the whole investigation. Never repair output, retry to make a row pass, or switch models.
3. Separate validity layers: schema (closed schema accepted), reference (every id exists in the discovered set) and semantic (source supports the meaning). The first two are code checks; the third requires reading source and is never implied by schema acceptance.
4. The small probe regexes fields and marks `runtime_writes: unknown`; the full probe reads `InputAction` blocks and `.inputactions` files but has no reflection, inventory, entity measurement, calibration or freshness classification. Expect the reader cap to bind and near-duplicates to split choices; report the ambiguity, do not merge ids or add a missing action.
5. For a future manifest or cache, review the design: sign/polarity, exact zero, analog availability, rule writer/polarity/vacuity, cache identity over authored structure, never cache incomplete. See `references/manifest-review.md`; not a measured probe result.
6. Write a compact `docs/probes/YYYY-MM-DD-*.md`: project commit, licence, model and effort, calls, tokens, cost, latency, choices, invalid rows, false and vacuous invariants, gaps and coverage. Raw calls and discovery dumps stay local under `runs/probes/`. Check `git status` for tracked game files.

## Outcome and stop

The local `calls.jsonl` and `discovery.json` (full probe only), plus the write-up, are the artifact. Account for every attempted call, including invalid and failed rows, and state what they cannot support. Completion is not "all rows valid": a preserved invalid row, a completed analysis of the rest, and an explicit claim limit can be complete. If unsupported or unknown, name the missing measurement and the smallest probe that would settle it.

Stop without approval, a key or a verified balance; if the licence is unclear or the run would copy game code or assets; if output invents an id or the schema is open; if asked to fix an answer, synthesise an action or call it a manifest. Never use a probe as a runtime manifest without stages 0–3. Scale the write-up: a rerun needs the row gate and the delta.
