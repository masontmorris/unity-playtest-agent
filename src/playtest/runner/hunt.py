"""One hunt on one build: predict, play, reproduce and report, from a game config and nothing else.

Every step is an existing command, run in order, each into its own folder under the output; a step whose result is
already there is not run again, so an interrupted hunt resumes where it stopped. Nothing here decides what is a bug:
S1 decides when to escalate, the frontier decides what to file, and code only measures and replays.

1. **guards**: one guards-only session (`run-session.ps1 -MeasureGuards -GuardsOnly`), no model call: the verbs the
   predictions are written about, as the build serves them.
2. **predict**: the frontier writes, from the build's declarations only, what each verb must do (`--part all`) and
   what each press must make happen, including situations near scene objects (`--part actions`).
3. **objective**: code turns the action rules into a test plan (`expectations test-plan`), followed by the
   standing playtest objective.
4. **sessions**: N live sessions with S1 and the frontier, each with the guard table measured first, press
   readings, the situations placed, and the expectations as facts. They run one after another (one Editor per
   project) and share one outcome memory, so a later session can reuse where an earlier one went.
5. **replays**: every candidate finding the frontier filed with a repro_check is replayed k of n from reloads
   (`reproduce.ps1`), with no model call. A finding with no repro_check is listed as not replayable.
6. **scores**: the pre-registered expectations are scored by code on every session's guard table and trace
   (`expectations score`, `score-play`).
7. **report**: `hunt.md` and `hunt.json` say what was filed, what reproduced, what the rules measured, and the cost.

The config is a JSON object: project, ref (the branch the project must be on), entity, calibration, manifest,
discovery, approval, and optionally scene, max_age_s, point_moves, wall_limit_s, max_calls, max_frontier_calls,
frontier_model, sessions, attempts. Relative paths are relative to the repository. Credentials are read from the
repository's `.env` by name and passed to the child processes only; they are never printed or written.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
CREDENTIALS = ("LIQUID_API_KEY", "LIQUID_D1.1_URL", "META_API_KEY")
PLAYTEST = (
    "Then playtest the rest of the session as usual: Playtest the level for the whole session using only the offered "
    "choices, and look for behaviour that is wrong. First collect what you can reach near the start. Then try every "
    "control, alone and in the combinations offered, in different situations, and try each one again after anything "
    "changes what you hold or how you move: standing still, moving forward, backward and sideways, on flat ground, on "
    "slopes and on steps, and while in the air. Walk up the structures you can see, walking into each step rather "
    "than jumping, and later come back to places you changed to see whether they behave the same way again. Watch "
    "what each choice changes; every action you take is measured and reviewed after the session. Ask the frontier "
    "model for help when something stays unexplained after you have tried it yourself; it can answer only a few "
    "times. Keep playing while its answer is pending. The session ends by itself; choose finish only if nothing is "
    "left to try.")
# The live loop's settings, the same on every game: only the config's paths and measured setup differ.
SESSION_ENV = {"UPA_RUN_DETECTORS": "1", "UPA_RUN_KEEP_HELP_ACROSS_RESTART": "1", "UPA_RUN_FRONTIER_CLIP_S": "30",
               "UPA_RUN_HOLD_MET_DIRECTIVE": "1", "UPA_RUN_DECIDE_DURING_OPTIONS": "1",
               "UPA_RUN_MEASURE_AIR_RISE": "1"}
SESSION_FLAGS = ("-RecordFrames", "-S1GameView", "-CoverageFacts", "-PatternFacts", "-S1Instructions", "policy/2",
                 "-FinishLabel", "conditional", "-S1ContinuousInput", "-Options", "-S1Context", "compact-menu",
                 "-Contingent", "-PacingBurst", "0.5", "-MeasureGuards", "-PressReadings", "-NoFinish")
DEFAULTS = {"scene": None, "max_age_s": 1.0, "point_moves": False, "wall_limit_s": 600, "max_calls": 600,
            "max_frontier_calls": 12, "frontier_model": "muse-spark-1.3", "sessions": 3, "attempts": 5}
REQUIRED = ("project", "ref", "entity", "calibration", "manifest", "discovery", "approval")


class HuntError(RuntimeError):
    pass


def load_config(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    missing = [key for key in REQUIRED if not raw.get(key)]
    if missing:
        raise HuntError(f"the config is missing {', '.join(missing)}")
    unknown = sorted(set(raw) - set(REQUIRED) - set(DEFAULTS) - {"game"})
    if unknown:
        raise HuntError(f"the config has unknown keys {', '.join(unknown)}")
    config = {**DEFAULTS, **raw}
    for key in ("entity", "calibration", "manifest", "discovery", "approval"):
        resolved = _path(config[key])
        if not resolved.is_file():
            raise HuntError(f"{key} {resolved} does not exist")
        config[key] = str(resolved)
    config["project"] = str(Path(config["project"]))
    return config


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO / path


def project_ref(project: Path) -> str | None:
    """The branch the project's checkout is on, or None when it cannot be read."""
    result = subprocess.run(["git", "-C", str(project), "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def unity_projects() -> list[str] | None:
    """The project paths of every running Unity process, normalised; None when they cannot be listed."""
    query = ("Get-CimInstance Win32_Process -Filter \"Name='Unity.exe'\" | ForEach-Object { $_.CommandLine }")
    result = subprocess.run(["pwsh", "-NoProfile", "-Command", query], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return [_norm(line) for line in result.stdout.splitlines() if line.strip()]


def _norm(text: str) -> str:
    return text.replace("\\", "/").lower()


def clear_stale_lock(project: Path, say) -> None:
    """A Unity lock file no running Unity process holds is left by a host that was stopped: removed, and said so.
    One that a running process may hold, or when processes cannot be listed, is left alone."""
    lock = project / "Temp" / "UnityLockfile"
    if not lock.is_file():
        return
    running = unity_projects()
    if running is None or any(_norm(str(project)) in line for line in running):
        return
    lock.unlink()
    say(f"removed a stale Unity lock no running process held: {lock}")


def credentials() -> dict[str, str]:
    from dotenv import dotenv_values

    values = dotenv_values(REPO / ".env")
    missing = [name for name in CREDENTIALS if not values.get(name)]
    if missing:
        raise HuntError("missing required credential(s) in .env: " + ", ".join(missing))
    return {name: str(values[name]) for name in CREDENTIALS}


def _run(args: Sequence[str], log: Path, env: Mapping[str, str] | None = None) -> int:
    """One child process, its output to `log` only (it can carry nothing secret, but it is long)."""
    import os

    log.parent.mkdir(parents=True, exist_ok=True)
    child = {**os.environ, **(env or {})}
    with log.open("w", encoding="utf-8") as out:
        return subprocess.call(list(args), cwd=REPO, env=child, stdout=out, stderr=subprocess.STDOUT)


def _pwsh(script: str, *args: str) -> list[str]:
    return ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REPO / "scripts" / "unity" / script),
            *args]


def _base(config: Mapping[str, Any], output: Path) -> list[str]:
    args = ["-ProjectPath", config["project"], "-EntityPath", config["entity"],
            "-CalibrationPath", config["calibration"], "-ManifestPath", config["manifest"],
            "-ApprovalPath", config["approval"], "-OutputDirectory", str(output),
            "-DiscoveryPath", config["discovery"]]
    if config.get("scene"):
        args += ["-ScenePath", config["scene"]]
    if config.get("point_moves"):
        args.append("-PointMoves")
    return args


def guards_step(config: Mapping[str, Any], out: Path, env: Mapping[str, str], say) -> Path:
    folder = out / "guards"
    if (folder / "guards.json").is_file():
        return folder / "guards.json"
    if folder.exists():
        raise HuntError(f"{folder} exists without guards.json; move it aside to measure again")
    say("guards: one guards-only session (no model call)")
    clear_stale_lock(Path(config["project"]), say)
    code = _run(_pwsh("run-session.ps1", *_base(config, folder), "-MaxCalls", "1", "-WallLimitSeconds", "180",
                      "-Objective", "measure the guard table", "-MeasureGuards", "-GuardsOnly", "-NoFinish",
                      "-ManifestOrigin", f"hunt guards on {config['ref']}"), out / "guards.log", env)
    if not (folder / "guards.json").is_file():
        raise HuntError(f"the guards session measured nothing (exit {code}); see {out / 'guards.log'}")
    return folder / "guards.json"


def predict_step(config: Mapping[str, Any], out: Path, guards: Path, part: str, env: Mapping[str, str],
                 say) -> Path:
    folder = out / f"expect-{part}"
    result = folder / "expectations.json"
    if result.is_file():
        return result
    if folder.exists():
        raise HuntError(f"{folder} exists without expectations.json; move it aside to predict again")
    say(f"predict: {part} (one frontier call)")
    code = _run([sys.executable, "-m", "playtest.runner.expectations", "predict", "--manifest", config["manifest"],
                 "--discovery", config["discovery"], "--guards", str(guards), "--approval", config["approval"],
                 "--output", str(folder), "--part", part, "--model", config["frontier_model"],
                 "--project", config["project"], "--ref", config["ref"]],
                out / f"expect-{part}.log", env)
    if not result.is_file():
        raise HuntError(f"predict {part} wrote nothing (exit {code}); see {out / f'expect-{part}.log'}")
    return result


def objective_step(actions: Path, out: Path) -> Path:
    path = out / "objective.txt"
    if path.is_file():
        return path
    from .expectations import test_plan

    plan = test_plan(json.loads(actions.read_text(encoding="utf-8")))
    if plan.endswith("Then keep exploring."):
        plan = plan[: -len("Then keep exploring.")]
    text = (plan + PLAYTEST) if plan else PLAYTEST.removeprefix("Then playtest the rest of the session as usual: ")
    path.write_text(text + "\n", encoding="utf-8", newline="\n")
    return path


def session_step(config: Mapping[str, Any], out: Path, number: int, objective: Path, every: Path, actions: Path,
                 env: Mapping[str, str], say) -> Path | None:
    folder = out / f"session-{number:02d}"
    if (folder / "run.json").is_file():
        return folder
    if folder.exists():
        raise HuntError(f"{folder} exists without run.json; move it aside to play it again")
    say(f"session {number}: live, up to {config['wall_limit_s']} s")
    clear_stale_lock(Path(config["project"]), say)
    child = {**env, **SESSION_ENV, "UPA_RUN_MEMORY_DIR": str(out / "memory")}
    code = _run(_pwsh("run-session.ps1", *_base(config, folder), "-MaxCalls", str(config["max_calls"]),
                      "-WallLimitSeconds", str(config["wall_limit_s"]), "-MaxAgeSeconds", str(config["max_age_s"]),
                      "-TokensPerMinute", "100000000", "-ObjectivePath", str(objective),
                      "-Frontier", "-MaxFrontierCalls", str(config["max_frontier_calls"]),
                      "-FrontierModel", config["frontier_model"], "-ExpectationsPath", str(every),
                      "-SituationsPath", str(actions), *SESSION_FLAGS,
                      "-ManifestOrigin", f"hunt session {number} on {config['ref']}"),
                out / f"session-{number:02d}.log", child)
    if not (folder / "run.json").is_file():
        say(f"session {number}: no run record (exit {code}); see {out / f'session-{number:02d}.log'}")
        return None
    return folder


def findings(session: Path) -> list[dict[str, Any]]:
    """The latest state of each finding the session's trace recorded."""
    latest: dict[str, dict[str, Any]] = {}
    trace = session / "trace.jsonl"
    if not trace.is_file():
        return []
    for line in trace.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("kind") == "finding":
            body = entry.get("body") or {}
            latest[body.get("finding_id")] = {**body, "trace_seq": entry.get("trace_seq")}
    return list(latest.values())


def replay_step(config: Mapping[str, Any], out: Path, session: Path, finding: Mapping[str, Any], say) -> dict:
    name = f"{session.name}-{finding['finding_id']}"
    folder = out / "replays" / name
    report = folder / "reproduce.json"
    if not report.is_file():
        if folder.exists():
            raise HuntError(f"{folder} exists without reproduce.json; move it aside to replay again")
        say(f"replay: {finding['finding_id']} from {session.name}, {config['attempts']} attempts (no model call)")
        clear_stale_lock(Path(config["project"]), say)
        args = ["-ProjectPath", config["project"], "-TracePath", str(session / "trace.jsonl"),
                "-ManifestPath", config["manifest"], "-OutputDirectory", str(folder),
                "-FindingId", finding["finding_id"], "-Attempts", str(config["attempts"])]
        if config.get("scene"):
            args += ["-ScenePath", config["scene"]]
        _run(_pwsh("reproduce.ps1", *args), out / "replays" / f"{name}.log")
    if not report.is_file():
        return {"status": "failed", "log": str(out / "replays" / f"{name}.log")}
    data = json.loads(report.read_text(encoding="utf-8"))
    summary = data.get("summary") or {}
    from collections import Counter

    reasons = Counter(a.get("reason") for a in data.get("attempts") or () if a.get("result") != "reproduced")
    return {"status": data.get("status"), "k": summary.get("k"), "n": summary.get("n"),
            "unknown": summary.get("unknown"), "attempted": summary.get("attempted"), "folder": str(folder),
            "skipped_inputs": (data.get("plan") or {}).get("skipped_inputs"),
            "reason": reasons.most_common(1)[0][0] if reasons else None}


def scores_step(out: Path, every: Path, actions: Path, guards: Path, sessions: Sequence[Path]) -> dict[str, Any]:
    from .expectations import main as expectations

    runs = [guards.parent, *(s for s in sessions if (s / "guards.json").is_file())]
    result: dict[str, Any] = {}
    guard_score = out / "guard-score.json"
    if not guard_score.is_file():
        expectations(["score", "--expectations", str(every), "--runs", *map(str, runs), "--output", str(guard_score)])
    result["guards"] = json.loads(guard_score.read_text(encoding="utf-8"))
    played = [s for s in sessions if (s / "trace.jsonl").is_file()]
    for name, path in (("play", every), ("rules", actions)):
        target = out / f"{name}-score.json"
        if played and not target.is_file():
            expectations(["score-play", "--expectations", str(path), "--runs", *map(str, played),
                          "--output", str(target)])
        result[name] = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {"scored": []}
    return result


def unmet_rows(scores: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every mismatch the scores found, each once: a rule both expectation files hold is scored twice."""
    seen: dict[str, dict[str, Any]] = {}
    for name in ("guards", "play", "rules"):
        for row in scores.get(name, {}).get("scored", []):
            if row.get("verdict") == "mismatch":
                seen.setdefault(row["line"], row)
    return list(seen.values())


def _cost(folder: Path) -> float:
    """What a folder's ledgers recorded, in USD: every calls/*.json row's cost."""
    total = 0.0
    for row in folder.rglob("calls/*.json"):
        try:
            cost = json.loads(row.read_text(encoding="utf-8")).get("cost_usd")
        except (OSError, ValueError):
            continue
        if isinstance(cost, (int, float)):
            total += cost
    return total


def report(config: Mapping[str, Any], out: Path, sessions: Sequence[Path], filed: Sequence[dict[str, Any]],
           scores: Mapping[str, Any]) -> str:
    lines = [f"# Hunt: {config.get('game') or config['project']} at {config['ref']}", ""]
    lines.append(f"{len(sessions)} live session(s), {config['wall_limit_s']} s each; frontier "
                 f"{config['frontier_model']}; replays {config['attempts']} attempts each. Recorded cost "
                 f"${_cost(out):.2f}.")
    lines += ["", "## Findings the frontier filed", ""]
    if not filed:
        lines.append("None.")
    for item in filed:
        replay = item["replay"]
        kn = (f"reproduced {replay['k']} of {replay['n']} ({replay['unknown']} unknown)"
              if replay.get("k") is not None else replay.get("status") or "not replayed")
        if replay.get("reason") and not replay.get("n"):
            kn += f"; {replay['reason']}"
        lines.append(f"- **{item['finding_id']}** ({item['session']}): {item['title']}. Expected: {item['expected']} "
                     f"Actual: {item['actual']} Replay: {kn}.")
    lines += ["", "## Pre-registered expectations that code found unmet", ""]
    unmet = unmet_rows(scores)
    if not unmet:
        lines.append("None.")
    lines += [f"- {row['line']}" for row in unmet]
    return "\n".join(lines) + "\n"


def hunt(config: Mapping[str, Any], out: Path, say=print) -> dict[str, Any]:
    on = project_ref(Path(config["project"]))
    if on != config["ref"]:
        raise HuntError(f"the project is on {on!r}, not {config['ref']!r}; check out the build under test first")
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps(dict(config), indent=2) + "\n", encoding="utf-8", newline="\n")
    env = credentials()
    guards = guards_step(config, out, env, say)
    every = predict_step(config, out, guards, "all", env, say)
    actions = predict_step(config, out, guards, "actions", env, say)
    objective = objective_step(actions, out)
    sessions = [s for n in range(1, int(config["sessions"]) + 1)
                if (s := session_step(config, out, n, objective, every, actions, env, say)) is not None]
    filed = []
    for session in sessions:
        for finding in findings(session):
            if finding.get("state") != "candidate":
                continue
            replay = (replay_step(config, out, session, finding, say) if finding.get("repro_check")
                      else {"status": "no repro_check"})
            filed.append({**finding, "session": session.name, "replay": replay})
    scores = scores_step(out, every, actions, guards, sessions)
    text = report(config, out, sessions, filed, scores)
    (out / "hunt.md").write_text(text, encoding="utf-8", newline="\n")
    result = {"config": dict(config), "sessions": [s.name for s in sessions], "findings": filed,
              "unmet": [row["line"] for row in unmet_rows(scores)],
              "cost_usd": round(_cost(out), 4)}
    (out / "hunt.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="playtest-hunt", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="the hunt's folder; an existing one resumes")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        hunt(config, args.output.resolve(), say=lambda text: print(text, flush=True))
    except HuntError as exc:
        print(f"playtest-hunt: {exc}", file=sys.stderr)
        return 1
    print((args.output / "hunt.md").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
