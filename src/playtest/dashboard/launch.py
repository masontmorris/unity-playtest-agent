"""Start a hunt from the dashboard with a prompt and a few clicks.

A launch is a game config (`runs/hunts/*.json`), the developer's prompt, and a size. The user's confirmation of
one dollar amount is the approval: the dashboard writes it as this launch's own approval file, built by
`playtest-approval`'s own `build` and `check_runner_accepts` from the per-call bounds measured in the ledgers the
config's approval already cites (largest measured cost times the margin). The amount shown is every call the
settings allow at those bounds, so it is a ceiling, not a forecast; the runner enforces it call by call.

Start also does the preparation a hunt would otherwise refuse to run without, after listing it on the page: it
checks out the config's branch in the game project and points the project's bridge dependency at this checkout
(`wire-fpskit.ps1`). It does neither while a Unity Editor has the project open, and a checkout git refuses is
reported, never forced. One hunt runs at a time: one Unity Editor per project.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from playtest.runner import approval as approval_cli
from playtest.runner import hunt as hunt_cli

FRONTIER_MODELS = ("muse-spark-1.3", "muse-spark-1.3-contributor", "muse-wonder-1.0")
PREDICT_CALLS = len(("all", "actions", "magnitudes"))
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
LIMITS = {"sessions": (1, 5), "wall_limit_s": (60, 1500), "max_frontier_calls": (1, 20), "attempts": (1, 10),
          "max_calls": (50, 1000)}
PRESETS = {
    "quick": {"label": "Quick", "sessions": 1, "wall_limit_s": 300, "max_frontier_calls": 6, "attempts": 3,
              "max_calls": 300},
    "standard": {"label": "Standard", "sessions": 3, "wall_limit_s": 600, "max_frontier_calls": 12, "attempts": 5,
                 "max_calls": 600},
    "thorough": {"label": "Thorough", "sessions": 5, "wall_limit_s": 900, "max_frontier_calls": 12, "attempts": 5,
                 "max_calls": 900},
}
MARGIN = Decimal("1.5")
MIN_SAMPLES = 10
APPROVAL_HOURS = 24
BOUND_CACHE_S = 300.0
S1_ARM_DEFAULT = re.compile(r"\[string\]\$S1Arm\s*=\s*'([^']+)'")
INPUT_KEYS = ("entity", "calibration", "manifest", "discovery", "approval")


class LaunchError(ValueError):
    pass


def session_s1_arm() -> str | None:
    """The S1 arm a hunt session runs: `run-session.ps1`'s default, since the hunt passes none."""
    try:
        text = (hunt_cli.REPO / "scripts" / "unity" / "run-session.ps1").read_text(encoding="utf-8")
    except OSError:
        return None
    match = S1_ARM_DEFAULT.search(text)
    return match.group(1) if match else None


def _resolve(value: Any, base: Path) -> Any:
    if not isinstance(value, str) or not value:
        return value
    path = Path(value)
    return str(path if path.is_absolute() else (base / path).resolve())


def hunt_configs(runs_root: Path) -> list[dict[str, Any]]:
    """Every `runs/hunts/*.json` a hunt can load. Relative paths in it are relative to the repository that owns
    `runs/`, the same rule `playtest-hunt` applies from its own checkout."""
    base = runs_root.resolve().parent
    configs = []
    for path in sorted((runs_root / "hunts").glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict) or not all(key in raw for key in hunt_cli.REQUIRED):
            continue
        resolved = {key: _resolve(value, base) if key in INPUT_KEYS else value for key, value in raw.items()}
        missing = [key for key in INPUT_KEYS if not Path(resolved[key]).is_file()]
        configs.append({"id": path.stem, "path": str(path), "game": raw.get("game") or path.stem,
                        "project": raw["project"], "ref": raw["ref"], "raw": resolved, "missing_files": missing,
                        "frontier_model": raw.get("frontier_model", hunt_cli.DEFAULTS["frontier_model"])})
    return configs


# --- what a launch may spend ---------------------------------------------------------------------------------


def evidence_ledgers(approval_path: Path) -> dict[str, list[Path]]:
    """The ledger folders the config's approval measured each arm from: the evidence a new bound is derived from."""
    try:
        doc = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found = {}
    for arm, limit in (doc.get("arms") or {}).items():
        ledgers = [Path(p) for p in ((limit or {}).get("evidence") or {}).get("ledgers") or [] if isinstance(p, str)]
        if ledgers:
            found[arm] = ledgers
    return found


_BOUNDS: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_BOUNDS_LOCK = threading.Lock()


def arm_bound(arm: str, ledgers: list[Path]) -> dict[str, Any]:
    """One arm's per-call bound, derived exactly as `playtest-approval` derives it; or why it cannot be."""
    key = (arm, "|".join(map(str, ledgers)))
    with _BOUNDS_LOCK:
        cached = _BOUNDS.get(key)
        if cached is not None and time.monotonic() - cached[0] < BOUND_CACHE_S:
            return cached[1]
    now = dt.datetime.now(dt.timezone.utc)
    try:
        built = approval_cli.build([(arm, 1)], {}, {arm: tuple(ledgers)}, margin=MARGIN, min_samples=MIN_SAMPLES,
                                   max_cost_usd=Decimal("1000"), use_by_utc=now + dt.timedelta(hours=1),
                                   approved_by="quote", balance_reason=None, now=now)
        limit = built["arms"][arm]
        result = {"per_call_max_usd": limit["per_call_max_usd"],
                  "samples": limit["evidence"]["samples"],
                  "observed_median_usd": limit["evidence"]["observed_median_usd"]}
    except (OSError, ValueError) as exc:
        result = {"error": str(exc)}
    with _BOUNDS_LOCK:
        _BOUNDS[key] = (time.monotonic(), result)
    return result


def _cents_up(amount: float) -> float:
    return math.ceil(round(amount * 100, 6)) / 100


def quote(settings: dict[str, Any], ledgers: dict[str, list[Path]], s1_arm: str | None) -> dict[str, Any]:
    """The most a hunt with these settings can spend: every allowed call at its arm's bound, plus the predictions.

    Each session and each prediction is its own run with its own ledger and approval, so the per-run cap is one
    session's ceiling and the hunt's ceiling is their sum. Replays and the guard session make no model call.
    """
    model = settings["frontier_model"]
    problems = []
    bounds = {}
    for role, arm in (("S1", s1_arm), ("frontier", model)):
        if not arm or arm not in ledgers:
            problems.append(f"no measured {role} calls for {arm or 'an unknown arm'} in this game's ledgers, "
                            "so no bound can be derived")
            continue
        bound = arm_bound(arm, ledgers[arm])
        if "error" in bound:
            problems.append(f"{arm}: {bound['error']}")
        else:
            bounds[role] = bound["per_call_max_usd"]
    if problems:
        return {"usd": None, "problems": problems}
    per_session = settings["max_calls"] * bounds["S1"] + settings["max_frontier_calls"] * bounds["frontier"]
    session_cap = _cents_up(per_session)
    predict = PREDICT_CALLS * bounds["frontier"]
    total = _cents_up(predict + settings["sessions"] * session_cap)
    return {"usd": total, "problems": [], "session_cap_usd": session_cap, "bounds": bounds, "lines": [
        {"what": f"{PREDICT_CALLS} prediction calls · {model}", "usd": round(predict, 4)},
        {"what": f"{settings['sessions']} × S1 · up to {settings['max_calls']} calls each · {s1_arm}",
         "usd": round(settings["sessions"] * settings["max_calls"] * bounds["S1"], 4)},
        {"what": f"{settings['sessions']} × frontier · up to {settings['max_frontier_calls']} calls each · {model}",
         "usd": round(settings["sessions"] * settings["max_frontier_calls"] * bounds["frontier"], 4)},
    ]}


def write_approval(path: Path, settings: dict[str, Any], ledgers: dict[str, list[Path]], s1_arm: str,
                   total_usd: float, name: str) -> dict[str, Any]:
    """The approval every run of this launch reads, recording the user's confirmation of `total_usd`."""
    now = dt.datetime.now(dt.timezone.utc)
    model = settings["frontier_model"]
    arms = [(s1_arm, settings["max_calls"]), (model, settings["max_frontier_calls"])]
    tier = "Contributor" if model.endswith("-contributor") else "Standard"
    reason = (f"User approved up to ${total_usd:.2f} for dashboard hunt {name} on {now:%Y-%m-%d} by confirming that "
              f"amount in the playtest dashboard; frontier {model} (Meta {tier}); balance not verified.")
    built = approval_cli.build(arms, {}, {arm: tuple(ledgers[arm]) for arm, _ in arms}, margin=MARGIN,
                               min_samples=MIN_SAMPLES,
                               max_cost_usd=Decimal(str(quote(settings, ledgers, s1_arm)["session_cap_usd"])),
                               use_by_utc=now + dt.timedelta(hours=APPROVAL_HOURS), approved_by="user",
                               balance_reason=reason, now=now)
    approval_cli.check_runner_accepts(built, dict(arms))
    approval_cli._write(path, built)
    return built


# --- preparation and checks ------------------------------------------------------------------------------------


def bridge_dependency() -> str:
    """What `run-session.ps1` requires the game's manifest to name: this checkout's bridge package."""
    return "file:" + str(hunt_cli.REPO / "bridge").replace("\\", "/")


def wired_to(project: Path) -> str | None:
    try:
        manifest = json.loads((project / "Packages" / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = (manifest.get("dependencies") or {}).get("com.playtest.bridge")
    return value if isinstance(value, str) else None


def credential_check() -> list[str]:
    """The credential names `playtest-hunt` needs that its checkout's `.env` lacks. Values are never returned."""
    env_file = hunt_cli.REPO / ".env"
    if not env_file.is_file():
        return list(hunt_cli.CREDENTIALS)
    try:
        from dotenv import dotenv_values
    except ImportError:
        return ["python-dotenv is not installed (uv sync --extra probe)"]
    values = dotenv_values(env_file)
    return [name for name in hunt_cli.CREDENTIALS if not values.get(name)]


def readiness(config: dict[str, Any]) -> dict[str, Any]:
    """Blocking problems, the preparation Start would do, and facts worth showing. Each item is plain text."""
    project = Path(config["project"])
    blockers, prepare = [], []
    if not project.is_dir():
        blockers.append(f"The game project is missing: {project}")
        return {"blockers": blockers, "prepare": prepare, "branch": None, "wired": None}
    running = hunt_cli.unity_projects()
    if running is None:
        blockers.append("Unity processes could not be listed, so an open Editor cannot be ruled out")
    elif any(hunt_cli._norm(str(project)) in line for line in running):
        blockers.append("A Unity Editor has the game project open; close it first")
    if config["missing_files"]:
        blockers.append("Missing inputs: " + ", ".join(config["missing_files"]))
    missing = credential_check()
    if missing:
        blockers.append(f"{hunt_cli.REPO / '.env'} lacks " + ", ".join(missing)
                        + "; run the dashboard from the checkout that holds the keys")
    branch = hunt_cli.project_ref(project)
    if branch is None:
        blockers.append("The game project's branch could not be read")
    elif branch != config["ref"]:
        prepare.append({"step": "checkout", "text": f"Switch the game project from {branch} to {config['ref']}"})
    wired = wired_to(project)
    if wired is None or wired.lower() != bridge_dependency().lower():
        prepare.append({"step": "wire", "text": "Point the game's bridge package at this checkout"
                        + (f" (now {wired.removeprefix('file:')})" if wired else "")})
    return {"blockers": blockers, "prepare": prepare, "branch": branch, "wired": wired}


def _run(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=120,
                            creationflags=flags)
    return result.returncode, (result.stdout + result.stderr).strip()


def prepare_project(config: dict[str, Any], steps: list[dict[str, str]]) -> None:
    project = Path(config["project"])
    for step in steps:
        if step["step"] == "checkout":
            code, text = _run(["git", "-C", str(project), "checkout", config["ref"]])
            if code != 0:
                raise LaunchError(f"git would not switch the project to {config['ref']}: {text[-400:]}")
        elif step["step"] == "wire":
            code, text = _run(["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               str(hunt_cli.REPO / "scripts" / "unity" / "wire-fpskit.ps1"), "-ProjectPath",
                               str(project)])
            if code != 0 or (wired_to(project) or "").lower() != bridge_dependency().lower():
                raise LaunchError(f"wiring the bridge failed: {text[-400:]}")


# --- launch ------------------------------------------------------------------------------------------------------


def settings_from(config: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    preset = PRESETS.get(str(request.get("preset") or "quick"))
    if preset is None:
        raise LaunchError(f"preset must be one of {', '.join(PRESETS)}")
    overrides = request.get("settings") or {}
    settings = {}
    for key, (low, high) in LIMITS.items():
        value = overrides.get(key, preset[key])
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise LaunchError(f"{key} must be a whole number from {low} to {high}")
        settings[key] = value
    model = overrides.get("frontier_model", config["frontier_model"])
    if model not in FRONTIER_MODELS:
        raise LaunchError(f"frontier_model must be one of {', '.join(FRONTIER_MODELS)}")
    settings["frontier_model"] = model
    return settings


def prompt_from(request: dict[str, Any]) -> str | None:
    prompt = request.get("prompt")
    if prompt is None:
        return None
    if not isinstance(prompt, str) or len(prompt) > hunt_cli.PROMPT_LIMIT:
        raise LaunchError(f"the prompt must be text of at most {hunt_cli.PROMPT_LIMIT} characters")
    return prompt.strip() or None


@dataclass
class Job:
    name: str
    config_id: str
    output: Path
    log: Path
    project: str
    settings: dict[str, Any]
    prompt: str | None
    bound_usd: float
    started_utc: str
    process: subprocess.Popen | None = field(default=None, repr=False)

    def view(self) -> dict[str, Any]:
        code = None if self.process is None else self.process.poll()
        return {"name": self.name, "config": self.config_id, "output": str(self.output), "log": str(self.log),
                "settings": self.settings, "prompt": self.prompt, "bound_usd": self.bound_usd,
                "started_utc": self.started_utc, "running": self.process is not None and code is None,
                "exit_code": code}


class Launcher:
    def __init__(self, runs_root: Path, spawn=None):
        self.runs_root = runs_root
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._spawn = spawn or _spawn

    def running(self) -> Job | None:
        return next((job for job in self.jobs.values() if job.view()["running"]), None)

    def output_for(self, name: str) -> Path:
        return self.runs_root / "hunts" / name

    def suggest_name(self, config_id: str) -> str:
        for number in range(1, 100):
            name = f"{config_id}-{number:02d}"
            if not self.output_for(name).exists() and not (self.runs_root / "hunts" / f"{name}.log").exists():
                return name
        return f"{config_id}-{dt.datetime.now():%Y%m%d-%H%M%S}"

    def options(self) -> dict[str, Any]:
        configs = []
        for config in hunt_configs(self.runs_root):
            ledgers = evidence_ledgers(Path(config["raw"]["approval"]))
            configs.append({**{k: v for k, v in config.items() if k != "raw"},
                            "suggested_name": self.suggest_name(config["id"]),
                            "measured_models": [m for m in FRONTIER_MODELS if m in ledgers]})
        running = self.running()
        return {"configs": configs, "s1_arm": session_s1_arm(), "frontier_models": list(FRONTIER_MODELS),
                "presets": PRESETS, "limits": LIMITS, "prompt_limit": hunt_cli.PROMPT_LIMIT,
                "running": running.view() if running else None}

    def warm(self) -> None:
        """Derive every config's bounds once, in the background, so the first quote on the page is quick."""
        def run() -> None:
            for config in hunt_configs(self.runs_root):
                try:
                    self.quote({"config": config["id"]})
                except (LaunchError, OSError, ValueError):
                    pass
        threading.Thread(target=run, daemon=True).start()

    def _config(self, config_id: str) -> dict[str, Any]:
        config = next((c for c in hunt_configs(self.runs_root) if c["id"] == config_id), None)
        if config is None:
            raise LaunchError(f"no hunt config {config_id!r} under {self.runs_root / 'hunts'}")
        return config

    def check(self, config_id: str) -> dict[str, Any]:
        return readiness(self._config(config_id))

    def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        config = self._config(str(request.get("config")))
        settings = settings_from(config, request)
        return {"settings": settings, "quote": quote(settings, evidence_ledgers(Path(config["raw"]["approval"])),
                                                     session_s1_arm())}

    def start(self, request: dict[str, Any]) -> Job:
        with self._lock:
            running = self.running()
            if running is not None:
                raise LaunchError(f"hunt {running.name} is still running; one hunt at a time (one Editor per project)")
            config = self._config(str(request.get("config")))
            settings = settings_from(config, request)
            prompt = prompt_from(request)
            s1_arm = session_s1_arm()
            ledgers = evidence_ledgers(Path(config["raw"]["approval"]))
            ceiling = quote(settings, ledgers, s1_arm)
            if ceiling["problems"]:
                raise LaunchError("; ".join(ceiling["problems"]))
            if request.get("approved_usd") != ceiling["usd"]:
                raise LaunchError(f"confirm the amount shown, ${ceiling['usd']:.2f}, to start")
            name = str(request.get("name") or self.suggest_name(config["id"]))
            if NAME.fullmatch(name) is None:
                raise LaunchError("name: lower-case letters, digits and dashes, starting with a letter or digit")
            output = self.output_for(name)
            log = self.runs_root / "hunts" / f"{name}.log"
            launch_dir = self.runs_root / "dashboard" / "launches"
            approval_path = launch_dir / f"{name}-approval.json"
            if output.exists() or log.exists() or approval_path.exists():
                raise LaunchError(f"{output} already exists; pick a new name")
            ready = readiness(config)
            if ready["blockers"]:
                raise LaunchError("; ".join(ready["blockers"]))
            prepare_project(config, ready["prepare"])
            launch_dir.mkdir(parents=True, exist_ok=True)
            try:
                write_approval(approval_path, settings, ledgers, s1_arm, ceiling["usd"], name)
            except (OSError, ValueError) as exc:
                raise LaunchError(f"the approval could not be written: {exc}") from exc
            derived = {**config["raw"], **settings, "approval": str(approval_path), "prompt": prompt}
            config_path = launch_dir / f"{name}.json"
            config_path.write_text(json.dumps(derived, indent=2) + "\n", encoding="utf-8", newline="\n")
            job = Job(name, config["id"], output, log, config["project"], settings, prompt, ceiling["usd"],
                      dt.datetime.now(dt.timezone.utc).isoformat())
            job.process = self._spawn(config_path, output, log)
            self.jobs[name] = job
            return job


def _spawn(config_path: Path, output: Path, log: Path) -> subprocess.Popen:
    log.parent.mkdir(parents=True, exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    with log.open("ab") as handle:
        return subprocess.Popen([sys.executable, "-m", "playtest.runner.hunt", "--config", str(config_path),
                                 "--output", str(output)], cwd=str(hunt_cli.REPO), stdout=handle,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags)
