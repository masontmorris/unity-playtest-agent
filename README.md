# unity-playtest-agent

An autonomous AI playtester for Unity games. Point it at a Unity project with an objective and a budget. It learns the game's controls and state by reading the game's own code, plays it in real time, investigates anything that looks wrong, reproduces what it finds, and exports a bug report with evidence.

Built for the [Meta Global AI Developer Hackathon](https://cerebralvalley.ai/e/meta-ai-developer-hackathon) (26 October – 4 November 2026), indie track.

> **Prior work.** In the five days before kickoff (22–26 October) we built a research prototype in a separate repo. A fast decision model played a Unity game turn by turn and could escalate to a stronger model. A bridge read each game's code to learn its controls and state. It could observe and control three open-source games, but it never completed a level or a lap, and it never found a bug. Its best run passed 2 of 32 checkpoints in a racing game. Reviewing the traces showed two more problems: our harness sometimes overrode the decision model, and we couldn't tell whether escalating to the stronger model ever helped. This project starts from an empty repo and keeps only the lessons. Anything reused from the prototype is listed in [`PRIOR_WORK.md`](PRIOR_WORK.md).

## Status

Submitted on 4 November 2026. It plays two third-party Unity games end to end and finds some seeded bugs, but not most of them yet.

**What works.** One command, `playtest-hunt --config <game>.json`, runs the whole loop with no human in it:
- **Preflight** reads the game's scripts and input bindings, then measures what each control does.
- **Expectations** are written down before play: what each press should change, and the sizes the game's own authored values fix.
- **Live sessions**: S1 plays and decides when to ask the frontier for help. The frontier files findings, each with a check it can test.
- **Replays**: every finding is replayed from a reset k of n times, with no model in the loop.
- **Report**: a report with the evidence, the replay results and the cost.

A local dashboard (below) starts a hunt from a prompt and follows it live.

**Results.** Each hunt is three 10-minute sessions and costs about $0.50–2.30.

| Build | Seeded bugs | Found and reproduced |
|---|---|---|
| FPSKit, held out (never tuned on) | 3 | 1: aim toggle does nothing, reproduced 5 of 5 in both held-out hunts |
| DragonBlaze, held out | 2 | 0 |
| FPSKit, development set (tuned on) | 4 | 2: both reproduced 5 of 5, and one also checked 0 of 5 on the unseeded build |

The held-out builds were scored blind: a separate scorer read their answer keys and returned only ids and counts, and no tuning used them. The records are in [`docs/probes/2026-11-04-final-tuning-hunts.md`](docs/probes/2026-11-04-final-tuning-hunts.md), [`docs/probes/2026-11-04-heldout-hunts.md`](docs/probes/2026-11-04-heldout-hunts.md) and [`docs/probes/2026-10-31-night-play-and-find.md`](docs/probes/2026-10-31-night-play-and-find.md).

The agent also found a real defect in DragonBlaze's base game: dialogue never opens, because its controller registers on a canvas the scene saves inactive. It reproduces 5 of 5 on the clean build.

**Honest limitations.**
- **Most seeded bugs are still missed** (1 of 5 held out). A bug is found only when a press's measured effect can be compared with a stated expectation. Bugs that need a long route, a precise spot or a judgment about feel are mostly out of reach.
- **False findings remain.** On the clean FPSKit build, the frontier still files "interact at the dispenser changes no timer", and those replays mostly come back unknown.
- **It plays through the same inputs a player has, but it sees more than a player does.** Every action is a keyboard, mouse or gamepad event in Unity's Input System, and nothing teleports or edits the game during play. Alongside the screen, though, it reads the game's internal values (health, flags, cooldowns) to measure what each press did. It can also restart the level.
- **Editor only, and Windows-tested.** It drives the Unity Editor in batch mode, one Editor per project. The game runs uncapped at about 1,700 fps there, which shows up as weapon jitter in FPSKit (#249).
- **It needs the game's source.** Preflight reads the C# scripts and compiled assemblies, so it can't test a shipped build.

**New vs. prior vs. third-party.**
- Everything in this repo was written during the hackathon (26 October – 4 November), except the items listed in [`PRIOR_WORK.md`](PRIOR_WORK.md).
- The test games are third-party projects. Their code, assets and seeded forks live in separate repos, not here.
- The models are called through their providers' APIs: S1 is Liquid `d1-ultra` by default (or open-weights d1-2B on this PC, through a loopback shim), and the frontier is Meta Muse Spark.

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
- **S1**: a fast decision model picks every action, and also decides when to ask for help. The default is Liquid `d1-ultra`; Baseten mammoth-1 (also on our own Modal server) is an option, and so is the open-weights d1-2B on this PC, free (`--s1-arm d1-2b-local`; dev runs only, under the LFM Open License). The harness supplies facts and valid choices. It never picks for S1.
  - Start the loopback shim with the Python of a decision-lab venv (CUDA, transformers 5.19 or newer), under the machine's GPU lease: `$env:D1_LOCAL_TOKEN = [guid]::NewGuid().ToString('N'); python <gpu_lease.py> run --holder unity-playtest-agent --purpose "d1-2b-local S1 shim" -- <decision-lab>\.venv\Scripts\python.exe scripts\s1\d1_local_server.py --port 8765`. It prints a JSON ready line once the model is loaded; `python scripts/s1/d1_local_server.py --help` loads nothing.
  - In the run's environment set `D1_LOCAL_URL=http://127.0.0.1:8765` and the same `D1_LOCAL_TOKEN`. Write the approval with `playtest-approval --arm d1-2b-local=<calls> --approved-by <you> --output <file>` (the arm's bounds are 0, and the call count still caps the run), then pass `--s1-arm d1-2b-local`.
- **Frontier** (Meta Muse Spark): when S1 escalates, it reviews a short clip and the investigation record. It returns a directive with a checkable success condition, revised choices and hypotheses.
- **Findings**: suspicions become candidate findings. Each is reproduced from a reset k of n times and exported as a report with steps, clips, cost and remaining uncertainty.

Stack: Python 3.11+ (asyncio) core, C# Unity 6 bridge, artifact files under `runs/`, and a local web dashboard.

## Test games

The agent is tested on two third-party open-source Unity games, FPSKit and DragonBlaze. Their code is not part of this repository. Bugs are seeded into separate forks, and the answer key is kept where the agent cannot read it.

## License

The Unity bridge package in [`bridge/`](bridge/) is licensed under the
[Apache License 2.0](bridge/LICENSE). The three.js bridge package in
[`bridge-three/`](bridge-three/) is licensed under [MIT](bridge-three/LICENSE-MIT) or
[Apache-2.0](bridge-three/LICENSE-APACHE), at your option. The rest of this repository, including
`tools/ilscan`, is licensed under the [GNU Affero General Public License v3.0 only](LICENSE).
Other engine bridges will be licensed individually, case by case.

Earlier versions remain available under their original license.
