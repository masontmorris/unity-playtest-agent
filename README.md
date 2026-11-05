# unity-playtest-agent

An autonomous AI playtester for Unity games. Point it at a Unity project with an objective and a budget. It learns the game's controls and state by reading the game's own code, plays it in real time, investigates anything that looks wrong, reproduces what it finds, and exports a bug report with evidence.

Built for the [Meta Global AI Developer Hackathon](https://cerebralvalley.ai/e/meta-ai-developer-hackathon) (26 October – 4 November 2026), indie track.

> **Prior work.** In the five days before kickoff (22–26 October) we built a research prototype in a separate repo. A fast decision model played a Unity game turn by turn and could escalate to a stronger model. A bridge read each game's code to learn its controls and state. It could observe and control three open-source games, but it never completed a level or a lap, and it never found a bug. Its best run passed 2 of 32 checkpoints in a racing game. Reviewing the traces showed two more problems: our harness sometimes overrode the decision model, and we couldn't tell whether escalating to the stronger model ever helped. This project starts from an empty repo and keeps only the lessons. Anything reused from the prototype is listed in [`PRIOR_WORK.md`](PRIOR_WORK.md).

## Status

Day 2. The bridge transport exists; nothing plays a game yet. This section will say what works, and what doesn't, as it changes.

What exists so far:
- **Contracts** (`src/playtest/contracts/`): typed models for the bridge protocol, choices, decisions, turn records, directives, findings and run records. JSON Schema is exported to `schemas/`, with shared fixtures in `fixtures/contracts/`.
- **Bridge v0** (`bridge/`, Unity package): a generic loopback JSONL transport with `upa/1` protocol checks, token auth, a one-Editor project lock, `hello`/`status`, a 20 Hz observation stream, bounded actions with atomic freshness checks and input leases, stop/clock control with verified input release, and offscreen JPEG capture with a startup self-test. It loads by reference in the clean FPSKit fork.
- **IL write scan** (`tools/ilscan/`, .NET 10): reads a project's compiled script assemblies for inspection only and records which methods store which fields. Preflight merges the result into discovery. It needs no Unity.
- **S1 decision parsing**: rejects an invalid answer and never repairs it.
- **Adherence checker**: fails a run if anything other than S1's choice reached the game.
- **CI**: tests, a name-blind lint over the core, a generic-bridge lint, the IL scan tool's .NET tests, and a gitleaks secret scan.

The roadmap is tracked in [issues](https://github.com/masontmorris/unity-playtest-agent/issues), with one milestone per hackathon day.

### Development

```sh
uv sync                      # Python 3.11+
uv run pytest
uv run playtest-export-schemas   # after changing a contract
uv run playtest-export-catalog --manifest <manifest.json> --output <catalog.json>  # supported declarations only
uvx pre-commit install       # local secret scan on commit
dotnet test tools/ilscan/tests/Playtest.IlScan.Tests   # .NET 10 SDK; no Unity
```

### Dashboard

```sh
uv sync --extra probe
uv run playtest-dashboard --runs runs      # then open http://127.0.0.1:8765/
```

A local page for the demo, served on loopback only, with no extra dependencies:

- **New playtest**: type what to test, pick a game (or name it in the prompt), choose Quick, Standard or Thorough, tick the approval and start. The prompt is quoted first in every session's objective, so S1 and the frontier both read it. The amount you approve is every call the settings allow at the per-call bounds measured from the game's past ledgers. The launch writes it as its own approval file (as `playtest-approval` builds one), so there is no approval file to renew by hand. Before starting, the page lists what Start will do first: switch the game project to the config's branch, and point its bridge package at this checkout. It refuses while a Unity Editor has the project open, and only one hunt runs at a time. Run the dashboard from the checkout whose `.env` holds the credentials.
- **Live** follows a hunt's steps and its current session: the game view (the bridge's ~10 Hz frames), every S1 decision with its outcome and latency, suspicions as they are raised, and frontier help, directives and findings. Raw, effective and executed choices that differ are flagged.
- **Findings** lists what was filed, each with its replay result (k of n), expected and actual, the repro check, and a button that opens the session recording at the cited moment (approximate) or the clip the frontier was shown.

The dashboard reads what runs write. It has no endpoint that chooses, pauses, steers or stops play, or asks for help.

## How it works

- **Unity bridge** (C# package): streams game state and frames out of the Editor and applies bounded, verified inputs. It has five commands: `hello`, `observe`, `act`, `stop` and `clock`.
- **Preflight**: reads the game's scripts, input bindings and live state, measures what each control actually does, and writes a manifest of readers and actions. A model chooses from what code discovered; it never writes code.
- **S1**: a fast decision model picks every action, and also decides when to ask for help. Candidates are Baseten mammoth-1 and Liquid's decision models. The harness supplies facts and valid choices. It never picks for S1.
- **Frontier** (Meta Muse Spark): when S1 escalates, it reviews a short clip and the investigation record. It returns a directive with a checkable success condition, revised choices and hypotheses.
- **Findings**: suspicions become candidate findings. Each is reproduced from a reset k of n times and exported as a report with steps, clips, cost and remaining uncertainty.

Stack: Python 3.11+ (asyncio) core, C# Unity 6 bridge, artifact files under `runs/`, and a local web dashboard.

## Test games

The agent is tested on third-party open-source Unity games. Their code is not part of this repository. Bugs are seeded into separate forks, and the answer key is kept where the agent cannot read it.

## License

The Unity bridge package in [`bridge/`](bridge/) is licensed under the
[Apache License 2.0](bridge/LICENSE). The rest of this repository, including
`tools/ilscan`, is licensed under the [GNU Affero General Public License v3.0 only](LICENSE).
Other engine bridges will be licensed individually, case by case.

Earlier versions remain available under their original license.
