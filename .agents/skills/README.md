# Project skills

Five conditional skills for `unity-playtest-agent`, under `.agents/skills/`. They are project-scoped instructions, not a set to preload. Read one `SKILL.md` when its trigger matches.

All paths in a skill resolve from the target repository root, the checkout containing `src/playtest/` and this directory. Do not infer the root from where the skill is installed.

## Directory

| Skill | Use when | Unique value |
|---|---|---|
| `playtest-s1-ownership` | A diff, design or trace touches decisions, adherence, pending help, monitors, provider adapters, turn records, or context assembly, windowing or compaction. | Builds the selected → effective → executed path table; catches repair, pending suppression and fact-to-escalation while allowing diagnostic thresholds; adds a context-fidelity mode for the real payload, directive persistence and executability. |
| `playtest-contract-change` | A change touches `src/playtest/contracts/**`, `schemas/**`, `fixtures/contracts/**`, or a consumer. | Enforces one Pydantic authority, cross-field checks absent from the export, valid and invalid fixtures, export drift, current unknown conventions, and C# parity only when a consumer exists. |
| `playtest-preflight-probe` | Running, extending or interpreting the current paid preflight probes, or reviewing a future manifest and cache design. | Uses the real flags and a current-project budget; separates schema, reference and semantic validity; preserves invalid rows and failed probes; covers sign, exact zero, analog availability, rule writer/polarity/vacuity and cache identity. |
| `playtest-eval-design` | Before a paid pilot, noise check, sweep or "better" claim. | Puts approval and call accounting first; predeclares the estimand, scoring and adjudication; freezes inputs; treats missing game outcomes as unknown; pairs conditions and forbids randomizing S1's right to escalate. |
| `playtest-evidence-review` | Reviewing run, probe, capture or report evidence before a commit or claim. | Checks provenance by artifact type, trace sequence, selected/effective/executed applicability, observation versus hypothesis, directive setpoints, capture source and consumer, and same-defect fix verification. |

In each skill, `Binding rules` are mandatory. The procedure is default practice and may be adapted with a stated reason.

## Invocation

Read the file explicitly:

```text
.agents/skills/<name>/SKILL.md
```

Any provider with file access can follow that path, whether or not it has a native skill loader. Providers that support `$skill-name` or project-scope auto-discovery may load it automatically; do not assume that for every provider. A brief should name the path and quote the trigger and operative law, because a fresh worktree does not have the gitignored `docs/build-bible.md`.

Keep the repo files canonical. Local directory links may expose them to provider skill loaders; avoid copied bodies that drift. `scripts/install-skills.ps1` reads a committed tree, so it cannot ship a drifted copy.

Local installation uses `scripts/install-skills.ps1`. It exports one commit's `.agents/skills` tree to `~/.local/share/unity-playtest-agent/skills/<full-commit>/` and points a junction per skill, from both `~/.codex/skills/<name>` and `~/.claude/skills/<name>`, at that snapshot. It verifies every file by Git blob hash, stages each link before swapping it, restores the previous junctions if any step fails, and refuses a link it did not create. This single installed snapshot serves agents in other checkouts and survives removal of the authoring worktree. Start a fresh session if its catalog is stale. An explicit path to an installed skill also works when a target checkout lacks it.

Discovery checked on 26 October 2026:

- **Codex:** all five in the live catalog and fresh prompt-input inspection from the main checkout, once per name.
- **OpenCode:** all five in the local skill API from the main checkout and this worktree, once per name; user discovery follows the `.claude/skills` links.
- **Muse:** all five in trusted-project and user CLI discovery, and all five loaded through native `read_skill` in a fresh T3 session.
- **Claude:** personal `.claude/skills` links installed and their contents verified against the canonical files. A fresh native-load test was blocked by the Claude API rate limit; runtime loading remains unverified. Binary import-scanner strings do not establish runtime `.agents/skills` support.

## Source snapshot and prototype provenance

Current code is at `879f9129cc8b9bdc6d0437b2edbf235271578a8f` on `chore/playtest-skills`. Sources include `AGENTS.md`, `docs/budget.md`, `docs/probes/2026-10-26-day1.md`, `src/playtest/{contracts,core}/`, `fixtures/contracts/`, `schemas/`, `scripts/probes/` and `tests/`.

Prototype provenance is `s1-loop` at `fe3f0a44`. Source docs, relative to that repo, include `docs/validation-2026-10-25-exact-analog-effects.md`, `docs/validation-2026-10-25-analog-steering.md`, `docs/validation-2026-10-25-speed-management.md`, `docs/validation-2026-10-25-waiting-recovery.md`, `docs/validation-2026-10-25-recovery-holds.md`, `docs/validation-2026-10-25-generic-trial-fixes.md`, `docs/validation-2026-10-25-p2-continuous-input.md`, `docs/validation-2026-10-25-ui-and-capture.md`, `docs/validation-2026-10-25-s1-decision-quality.md`, `docs/checkpoint-2026-10-25-bridge-shim-capture.md`, `docs/preflight-rules.md`, `docs/typesafe.md`, `docs/broadcast.md` and `docs/research/cybernetics-20261025-escalation.md`. Lessons are paraphrased design evidence, with current-contract names checked separately. No prototype code, prompts, fixtures, labels, recorded data or assets are copied.

## Merge and reject decisions

- Kept five skills. `s1-context-fidelity` is merged into `playtest-s1-ownership` as `references/context-fidelity.md`; `capture-provenance` is integrated into `playtest-evidence-review`; `manifest-semantics-review` is folded into `playtest-preflight-probe` and `references/manifest-review.md`. This is not a sixth skill.
- Deferred bridge, input-lease and scene-hygiene procedures until a bridge and a Unity session exist. `AGENTS.md` already owns serialization, lock and one-Editor rules; writing commands now would invent them.
- Rejected standalone `finding-states` (folded into evidence review), `sweep-budget` (folded into preflight and eval as a spending gate), `trial-gate` and `bridge-smoke` (no runtime or bridge command exists).
- Rejected generic code-review, git, documentation, orchestration, security, prompt-tuning and one-off bake-off skills. They duplicate `AGENTS.md` or installed tools, or do not contain a recurring decision.
- No scripts or UI metadata. Two focused references were added because context fidelity and future manifest/cache review are distinct conditional modes; the other three entrypoints remain self-contained.

## Limitations

- Git carries these skills into worktrees based on a commit containing them. The installed user links also serve older checkouts on this machine; other machines need the committed collection or an accessible explicit path.
- The skills describe the code at the snapshot. If commands, flags, contract names or status change, update the skill and re-run the validator; stale instructions are worse than no skill.
- The production preflight, manifest cache, context builder, bridge, turn loop and report CLI do not exist. Checks that depend on them are design review or `not applicable`, not passed.
- The installed snapshot is pinned to a commit and does not follow uncommitted edits. To update it, commit the skill change, run `scripts/install-skills.ps1 -Repo <repo> -Commit <full-commit> -WhatIf` to preview, then drop `-WhatIf`. The script verifies the new snapshot against Git and repoints only these five links, leaving unrelated skills and the previous version in place. Links do not travel with Git; Claude on another machine needs its own personal/project link or explicit-path invocation.
- These skills do not replace `AGENTS.md`, the roadmap, paid-run approval or the one-Editor-per-project rule.
- Scale the artifact to the change. A trivial edit needs the relevant check, not a full matrix or a new record.

## Validation

Run the installed skill-creator `quick_validate.py` against each skill directory, then `git diff --check` including new files. Frontmatter validity does not prove the guidance is correct; exercise a skill on a real task and revise it from observed failures.

All five passed format validation. A fresh Muse worker applied them to six original synthetic cases: control-path review, directive/context persistence, static preflight semantics, a misleading invalid fixture, comparison/budget design, and finding/capture evidence. The orchestrator reviewed the actual outputs: all six made the required distinctions without live trials or paid probe calls. This validates those cases, not future runtime behavior or Claude loading.
