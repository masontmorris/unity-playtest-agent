# Agent orchestration (T3 Code)

The goal is to spread work across separate usage pools: OpenCode Go, Codex and Claude. Grunt work goes to the cheap pool, and harder work goes to the mid-tier workers. The orchestrator's own pool is for its own judgment.

## When the orchestrator is GPT-6 Astra (`codex` / `gpt-6-astra`)

The orchestrator is whichever model is driving T3 Code in this thread. This section applies **only when that model is GPT-6 Astra**. For any other orchestrator, the normal rules in "Doing it yourself vs. delegating" apply unchanged.

Astra is the most expensive pool here, so when it is orchestrating, treat its tokens as the scarce resource and **delegate by default**. Astra decides, briefs, reviews and verifies. Grunt workers do the rest.

- **Default to delegating.** If a task is well specified, mechanical, high-volume, or just needs reading or running things, write a brief and delegate it. Do not start by exploring the code yourself.
- **Keep Astra's context small.** Don't read large files, grep through unfamiliar code, tail logs, or run test suites in the orchestrator thread. Ask a worker a concrete question and read only its short summary.
- **Astra does only this directly:** design and escalation judgment (S1-first rules), reading worker diffs and rerunning acceptance checks, deciding what to do next, and edits small enough that the brief would cost more than the change (a few lines, one tool call).
- **Fan out.** Split work into independent pieces and launch them in parallel with `delegate_task` (`mode:"async"`, one `clientRequestId` per task). Keep Astra's turns for dispatching and reviewing, not waiting. Use `task_status` to check on workers.
- **Don't re-do a worker's job.** Review the diff and run the acceptance check. If it fails, send a corrective brief to a worker. Fix it yourself only for trivial slips.
- **Worker tiers don't change.** Astra orchestrating doesn't change the worker tiers or escalation order. Astra itself (including Astra Pro) is never a worker unless the user asks.

## Worker tiers

| Tier | When | `delegate_task` target |
|---|---|---|
| Grunt | Default for delegated work | `{"providerInstanceId":"opencode","model":"opencode-go/deepseek-v4.1-flash","options":{"variant":"max"}}` |
| Mid | Grunt workers fail, are rate-limited or busy, or the task is too hard for them. No need to ask the user | `{"providerInstanceId":"claudeAgent","model":"claude-sonnet-5-5","options":{"effort":"max"}}` |
| Mid | Same as above; use as the second lane or when the Claude pool is busy | `{"providerInstanceId":"codex","model":"gpt-6-luna","options":{"reasoningEffort":"xhigh"}}` |

**Same-setting lane.** An orchestrator that is itself on the cheap pool — OpenCode, for example DeepSeek
`deepseek-v4.1-flash-ultrafast` at high effort — may also delegate to a native subagent on its own provider, model
and settings, using the harness's own subagent tool. That lane is grunt-tier work only: it buys parallelism and
keeps the orchestrator's context small; it does not buy a better model. Keep each child inside one turn, give it
the same brief a grunt worker would get, and review its diff and rerun its acceptance check yourself. Use
`delegate_task` instead when the child needs another provider or another model, settings the orchestrator does not
have, a separate checkout, or background execution.

- Always use `delegate_task`, including for Claude workers, except on the same-setting lane above. Do not use native subagent tools to reach another provider's or another model's pool (Claude's Agent/Task, Codex sub-agents): those bill the orchestrator's own pool and ignore these options.
- Check that the returned provider, model and options match the table. If a model id is missing from `orchestrator_capabilities`, report the mismatch. Do not substitute another model silently.
- Escalate in order: grunt, then mid. If grunt workers fail twice on a task, move it to a mid worker (Sonnet 5.5 or Luna) without asking. If a mid worker also fails, either do it yourself or ask the user how to proceed. Never use a model outside this table as a worker on your own.

## Doing it yourself vs. delegating

The orchestrator may work directly. Delegate when the brief is cheaper than the work. (When the orchestrator is GPT-6 Astra, the stricter rules in its section above override this.)

- **Do it yourself:** anything needing judgment you already hold from this conversation, such as design decisions, interpreting trial results, reviewing worker output, or deciding what S1 should see. Also do small edits and lookups that take only a few tool calls, where writing a self-contained brief would cost about as much as doing it.
- **Delegate to grunt workers:** well-specified, mechanical, or high-volume work. Examples: applying a known change across files, writing or extending tests from a clear spec, renames and refactors with an exact target, log/trace digging with a concrete question, running suites and summarizing failures, drafting validation write-ups from given numbers, and searching unfamiliar code.
- Run independent grunt tasks in parallel. When the OpenCode pool alone would bottleneck, use a mid worker as the second lane.

## Writing briefs

Workers start cold. Every brief must include:

1. The goal, and why it matters, in one or two sentences. Link the GitHub issue it closes.
2. Exact paths and repos, plus the branch or commit to start from.
3. Constraints, quoting the project rules below that apply. The build bible (`docs/build-bible.md`) is gitignored, so worktrees don't have it: quote the laws and contracts the task depends on in the brief itself.
4. An acceptance check the worker must run, such as a test command, expected output, or diff scope.
5. What to return: a short summary, changed files, and check results. No full file dumps.

Use `isolation:"worktree"` when a worker edits files while you or another worker edit the same repo. Note that uncommitted edits are not copied into the worktree.

## After a worker finishes

- Read the diff and rerun the acceptance check yourself before reporting. Never relay a worker's claims as verified.
- Workers do not commit or push. The orchestrator commits after verifying, and only when the user has asked for commits.

## GitHub workflow

- Work is tracked as issues under the day milestones (`Day 1 – Foundations` … `Day 10 – Submit`) and on the project roadmap. Each day has one `gate` issue holding that day's acceptance check.
- The roadmap updates itself (`.github/workflows/roadmap.yml`). Every issue is added to the project, and its Day and dates follow its milestone. It moves to In Progress when an open PR says `Closes #N`, and to Done when it closes. To move an issue between days, change its milestone, not the project fields.
- One branch per issue or tight group of issues, named `<type>/<short-slug>` (`feat/`, `fix/`, `docs/`, `chore/`, `eval/`), cut from an up-to-date `main`. Never commit directly to `main`.
- Changes land through a pull request whose description says `Closes #N` and what was verified. CI must be green before merge. Squash-merge, then delete the branch.

## Project rules every brief must carry when relevant

- **S1, not code, decides actions and escalation.** Only the choice S1 selected is executed; no code path substitutes, ranks, suppresses or reinterprets it. No score, threshold, stagnation detector or timer may turn into a help request. Code exposes facts and valid choices; it never chooses.
- **The core is name-blind.** No game field, object, control or genre word appears in `src/playtest/core/`. Game knowledge lives only in the preflight-generated manifest. `tests/test_name_blind.py` enforces this.
- **Measured, not inferred.** Missing evidence is `unknown`/`null`, never zero, success or failure.
- **Prior work.** Do not copy code, prompts, fixtures, labels or recorded data from the s1-loop prototype unless the brief says to. If it does, the same commit adds a row to `PRIOR_WORK.md`.
- **This repo is public.** Never print, paste or commit `.env` contents or API keys. No third-party game code or assets in this repo; test games stay in their own repos.
- Never push the test games (FPSKit, DragonBlaze, street-racing) to their `origin` (third-party upstream). Push only to `backup`.
- Only one Unity Editor per game project. Never run two trials or Editor-driven validations on the same game at once; serialize them.
- Full run traces, frames and clips stay local under `runs/`. Commit only compact records.
- **Budget before spend.** Any paid model sweep states its call count and estimated cost and gets the user's OK before it starts.

## Project skills

Five conditional skills live under `.agents/skills/`. They are not an always-load mandate: when a task matches a skill's trigger, read that `SKILL.md`; otherwise leave the directory alone. If a provider cannot discover project skills, cite the path and quote the trigger and law in the brief. The index, invocation notes and current limitations are in `.agents/skills/README.md`.
