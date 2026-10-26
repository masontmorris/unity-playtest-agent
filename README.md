# unity-playtest-agent

An autonomous AI playtester for Unity games. Point it at a Unity project with an objective and a budget. It learns the game's controls and state by reading the game's own code, plays it in real time, investigates anything that looks wrong, reproduces what it finds, and exports a bug report with evidence.

Built for the [Meta Global AI Developer Hackathon](https://cerebralvalley.ai/e/meta-ai-developer-hackathon) (26 October – 4 November 2026), indie track.

> **Prior work.** In the five days before kickoff (22–26 October) we built a research prototype in a separate repo. A fast decision model played a Unity game turn by turn and could escalate to a stronger model. A bridge read each game's code to learn its controls and state. It could observe and control three open-source games, but it never completed a level or a lap, and it never found a bug. Its best run passed 2 of 32 checkpoints in a racing game. Reviewing the traces showed two more problems: our harness sometimes overrode the decision model, and we couldn't tell whether escalating to the stronger model ever helped. This project starts from an empty repo and keeps only the lessons. Anything reused from the prototype is listed in [`PRIOR_WORK.md`](PRIOR_WORK.md).

## Status

Day 1. Nothing plays a game yet. This section will say what works, and what doesn't, as it changes.

What exists so far:
- **Contracts** (`src/playtest/contracts/`): typed models for the bridge protocol, choices, decisions, turn records, directives, findings and run records. JSON Schema is exported to `schemas/`, with shared fixtures in `fixtures/contracts/`.
- **S1 decision parsing**: rejects an invalid answer and never repairs it.
- **Adherence checker**: fails a run if anything other than S1's choice reached the game.
- **CI**: tests, a name-blind lint over the core, and a gitleaks secret scan.

The roadmap is tracked in [issues](https://github.com/masontmorris/unity-playtest-agent/issues), with one milestone per hackathon day.

### Development

```sh
uv sync                      # Python 3.11+
uv run pytest
uv run playtest-export-schemas   # after changing a contract
uvx pre-commit install       # local secret scan on commit
```

## How it works

- **Unity bridge** (C# package): streams game state and frames out of the Editor and applies bounded, verified inputs. It has five commands: `hello`, `observe`, `act`, `stop` and `clock`.
- **Preflight**: reads the game's scripts, input bindings and live state, measures what each control actually does, and writes a manifest of readers and actions. A model chooses from what code discovered; it never writes code.
- **S1**: a fast decision model picks every action, and also decides when to ask for help. Candidates are Baseten mammoth-1 and Liquid's decision models. The harness supplies facts and valid choices. It never picks for S1.
- **Frontier** (Meta Muse Spark): when S1 escalates, it reviews a short clip and the investigation record. It returns a directive with a checkable success condition, revised choices and hypotheses.
- **Findings**: suspicions become candidate findings. Each is reproduced from a reset k of n times and exported as a report with steps, clips, cost and remaining uncertainty.

Planned stack: Python 3.11+ (asyncio) core, C# Unity 6 bridge, SQLite plus artifact files, and a small FastAPI live view.

## Test games

The agent is tested on third-party open-source Unity games. Their code is not part of this repository. Bugs are seeded into separate forks, and the answer key is kept where the agent cannot read it.

## License

[GNU Affero General Public License v3.0](LICENSE).
