"""One live smoke trial of S1 playing a three.js game in a real browser.

It starts, in order: the local d1-2B S1 shim, the game's static server, the bridge relay serving every
declared action, and Chrome on the game page with the relay URL. It measures each declaration's
execution evidence with the input probe, exports the run catalog from the updated manifest (reopening
the page if that catalog differs from the probe's), and runs `playtest-run` with the `d1-2b-local` arm. It tears everything
down and writes `report.json` beside the run's own records.

The manifest is a smoke stand-in until the three.js bridge has a preflight. It is built from the
game's hand-written declarations (`playtest/smoke.json` in the game repo) with the runner tests'
manifest helpers. No action is supported until the probe has measured its release-verified evidence.
A trial is a smoke run, not a finding.

Run it inside the GPU lease, because the model and the page both render on the shared GPU:

    python C:/Users/mason/.gpu/gpu_lease.py run --holder upa-three --purpose "three.js live trial" -- \
        uv run python scripts/three/live_trial.py --game C:/Users/mason/source/repos/three-playtest-sandbox --approved-by "<who asked>"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))

from runner_manifest import action, manifest  # noqa: E402  (the shared manifest helpers)

from playtest.contracts.action_catalog import ActionCatalog  # noqa: E402
from playtest.contracts.manifest import PreflightManifest  # noqa: E402
from playtest.preflight import catalog as catalog_export  # noqa: E402
from playtest.preflight import manifest as manifest_checks  # noqa: E402

CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
DECISION_LAB = Path("C:/Users/mason/decision-lab")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Process:
    """One child process whose stdout lines are kept and copied to a log file."""

    def __init__(self, name: str, command: list[str], log_dir: Path, *, env: dict[str, str] | None = None,
                 cwd: Path | None = None) -> None:
        self.name = name
        self.lines: list[str] = []
        self._log = (log_dir / f"{name}.log").open("w", encoding="utf-8")
        self._log.write("$ " + " ".join(command) + "\n")
        self.process = subprocess.Popen(
            command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.append(line.rstrip("\n"))
            self._log.write(line)
            self._log.flush()

    def json_line(self, key: str, timeout_s: float) -> dict[str, Any]:
        """The first stdout line that is a JSON object with `key`."""
        deadline = time.monotonic() + timeout_s
        seen = 0
        while time.monotonic() < deadline:
            while seen < len(self.lines):
                line = self.lines[seen]
                seen += 1
                try:
                    body = json.loads(line)
                except ValueError:
                    continue
                if isinstance(body, dict) and key in body:
                    return body
            if self.process.poll() is not None:
                raise RuntimeError(f"{self.name} exited with {self.process.returncode} before printing {key}")
            time.sleep(0.05)
        raise RuntimeError(f"{self.name} printed no {key} line within {timeout_s:g} s")

    def stop(self) -> None:
        if self.process.poll() is None:
            # The whole tree: Chrome and the shim spawn children.
            subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self._log.close()


def build_manifest(spec: dict[str, Any]) -> dict[str, Any]:
    """A valid smoke manifest for the spec's entity and declarations, with nothing yet supported."""
    actions = [action(item["name"], item["control"], hold_s=item["hold_s"], supported=False) for item in spec["actions"]]
    data = manifest(*actions).model_dump(mode="json")
    entity = spec["entity_id"]
    scene = spec["scene_id"]
    data["readers"] = []
    data["selection"]["offered_fields"] = []
    data["entity_id"] = entity
    data["entity"] = {"id": entity, "path": entity.rsplit("::", 1)[-1], "scene_id": f"scene:{scene}", "scene_path": scene}
    for row in data["actions"]:
        for calibration in row["calibrations"]:
            for binding in calibration["bindings"]:
                binding["source_object_id"] = entity
    for control in data["selection"]["offered_controls"]:
        for binding in control["bindings"]:
            binding["source_object_id"] = entity
    problems = manifest_checks.validate(PreflightManifest.model_validate(data))
    if problems:
        raise ValueError("the smoke manifest does not validate: " + "; ".join(problems))
    return data


def declared_catalog(manifest_path: Path) -> ActionCatalog:
    """Every declaration in the manifest, supported or not: what the probe needs the bridge to execute."""
    value = PreflightManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"), strict=True)
    actions = {action.name: catalog_export._action(action) for action in value.actions}
    actions.update({skill.name: catalog_export._skill(skill) for skill in value.skills})
    return ActionCatalog(actions=actions)


def write_catalog(path: Path, catalog: ActionCatalog) -> None:
    """Written byte for byte as `playtest-export-catalog` writes, so equal catalogs have equal pins."""
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(catalog_export.serialize(catalog), stream, indent=2, allow_nan=False, sort_keys=True)
        stream.write("\n")


def _write(path: Path, value: Any) -> None:
    path.write_bytes((json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _run(command: list[str], log: Path, env: dict[str, str], timeout_s: float) -> int:
    with log.open("w", encoding="utf-8") as stream:
        stream.write("$ " + " ".join(command) + "\n")
        stream.flush()
        try:
            return subprocess.run(command, cwd=REPO, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                  timeout=timeout_s).returncode
        except subprocess.TimeoutExpired:
            stream.write(f"timed out after {timeout_s:g} s\n")
            return -1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--game", type=Path, required=True, help="the game repo: serve.mjs and playtest/smoke.json")
    parser.add_argument("--out", type=Path, default=None, help="default runs/three-live/<stamp>")
    parser.add_argument("--chrome", type=Path, default=CHROME)
    parser.add_argument("--headless", action="store_true", help="Chrome's new headless mode instead of a window")
    parser.add_argument("--decision-lab", type=Path, default=DECISION_LAB)
    parser.add_argument("--port", type=int, default=8173)
    parser.add_argument("--max-calls", type=int, default=20)
    parser.add_argument("--wall-limit-s", type=float, default=240.0)
    parser.add_argument("--max-age-s", type=float, default=8.0)
    parser.add_argument("--probe-repeats", type=int, default=2)
    parser.add_argument("--no-game-view", action="store_true")
    parser.add_argument("--approved-by", required=True,
                        help="who asked for this trial, recorded in the approval file (the arm is local and costs $0)")
    args = parser.parse_args(argv)

    game = args.game.resolve()
    spec = json.loads((game / "playtest" / "smoke.json").read_text(encoding="utf-8"))
    out = (args.out or REPO / "runs" / "three-live" / _stamp()).resolve()
    out.mkdir(parents=True, exist_ok=False)
    logs = out / "logs"
    logs.mkdir()
    report: dict[str, Any] = {"started_utc": datetime.now(timezone.utc).isoformat(), "game": str(game),
                              "status": "error", "steps": {}}
    processes: list[Process] = []
    profile = Path(tempfile.mkdtemp(prefix="upa-three-chrome-"))
    token = secrets.token_urlsafe(24)
    env = {**os.environ, "UPA_REPO_ROOT": str(REPO), "D1_LOCAL_TOKEN": token}

    try:
        started = time.monotonic()
        shim = Process("d1-shim", [str(args.decision_lab / ".venv" / "Scripts" / "python.exe"),
                                   str(REPO / "scripts" / "s1" / "d1_local_server.py"),
                                   "--decision-lab", str(args.decision_lab)], logs, env=env)
        processes.append(shim)
        server = Process("game-server", ["node", str(game / "serve.mjs"), "--bridge", str(REPO / "bridge-three" / "src"),
                                         "--port", str(args.port)], logs, cwd=game)
        processes.append(server)
        server.json_line("serving", 20)

        manifest_path = out / "manifest.json"
        _write(manifest_path, build_manifest(spec))
        probe_catalog = out / "probe-catalog.json"
        write_catalog(probe_catalog, declared_catalog(manifest_path))

        lock = game / "Temp" / "playtest-bridge.lock"
        page: dict[str, Any] = {}

        def open_page(catalog: Path, tag: str) -> None:
            """Start the relay serving `catalog` and Chrome on the game, and wait until the page attaches."""
            for name in ("relay", "chrome"):
                if name in page:
                    page.pop(name).stop()
            previous = lock.read_text(encoding="utf-8") if lock.exists() else None
            relay = Process(f"relay-{tag}", ["node", str(REPO / "bridge-three" / "relay" / "relay.mjs"),
                                             "--project", str(game), "--catalog", str(catalog)], logs)
            page["relay"] = relay
            processes.append(relay)
            page_url = relay.json_line("playtest", 20)["playtest"]
            url = f"http://127.0.0.1:{args.port}/?playtest={urllib.parse.quote(page_url, safe='')}"
            chrome_args = [str(args.chrome), f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                           "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                           "--disable-backgrounding-occluded-windows", "--autoplay-policy=no-user-gesture-required",
                           "--window-size=1280,720"]
            chrome_args += ["--headless=new", "--enable-gpu", "--use-angle=d3d11"] if args.headless else []
            chrome = Process(f"chrome-{tag}", chrome_args + [url if args.headless else f"--app={url}"], logs)
            page["chrome"] = chrome
            processes.append(chrome)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if lock.exists() and lock.read_text(encoding="utf-8") != previous:
                    return
                time.sleep(0.1)
            raise RuntimeError(f"the page never attached ({tag}): no new bridge lock")

        open_page(probe_catalog, "probe")
        renderer = server.json_line("page_log", 60)
        report["renderer"] = json.loads(renderer["page_log"])

        ready = shim.json_line("ready", 600)
        report["s1_shim"] = {key: ready.get(key) for key in ("kernels", "port")}
        report["steps"]["startup_s"] = round(time.monotonic() - started, 2)
        env["D1_LOCAL_URL"] = f"http://127.0.0.1:{ready['port']}"

        plan = {"schema_version": 1, "catalog": str(probe_catalog), "manifest": str(manifest_path),
                "update_manifest": str(manifest_path), "entity_id": spec["entity_id"],
                "actions": [{"name": item["name"], "repeats": args.probe_repeats} for item in spec["actions"]],
                "max_age_s": 5.0}
        plan_path = out / "probe-plan.json"
        _write(plan_path, plan)
        probe_started = time.monotonic()
        code = _run(["uv", "run", "playtest-input-probe", "--project", str(game), "--plan", str(plan_path),
                     "--output", str(out / "probe.json")], logs / "input-probe.log", env, 600)
        report["steps"]["probe"] = {"exit": code, "seconds": round(time.monotonic() - probe_started, 2)}
        updated = json.loads(manifest_path.read_text(encoding="utf-8"))
        supported = [row["name"] for row in updated["actions"] if row["execution_supported"]]
        report["supported_actions"] = supported
        if not supported:
            raise RuntimeError("the probe measured no supported action, so the run would offer S1 nothing")

        # The run serves exactly what the manifest now exports: only the declarations the probe supported.
        catalog_path = out / "catalog.json"
        code = _run(["uv", "run", "playtest-export-catalog", "--manifest", str(manifest_path), "--output",
                     str(catalog_path)], logs / "export-catalog.log", env, 120)
        if code != 0:
            raise RuntimeError(f"catalog export exited with {code}")
        catalog_sha = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
        if catalog_path.read_bytes() != probe_catalog.read_bytes():
            open_page(catalog_path, "run")
            report["page_reopened_for_run_catalog"] = True

        _write(out / "entity.json", {"entity_id": spec["entity_id"]})
        _write(out / "calibration.json", {"entity_id": spec["entity_id"]})
        code = _run(["uv", "run", "playtest-approval", "--arm", f"d1-2b-local={args.max_calls}", "--approved-by",
                     args.approved_by, "--output", str(out / "approval.json")], logs / "approval.log", env, 120)
        if code != 0:
            raise RuntimeError(f"playtest-approval exited with {code}")
        run_dir = out / "run"
        run_dir.mkdir()
        command = ["uv", "run", "playtest-run", "--project", str(game), "--entity", str(out / "entity.json"),
                   "--calibration", str(out / "calibration.json"), "--output", str(run_dir / "summary.json"),
                   "--manifest", str(manifest_path), "--manifest-origin",
                   "hand-written smoke declarations with probe-measured evidence; no three.js preflight yet",
                   "--catalog", str(catalog_path), "--catalog-sha256", catalog_sha, "--approval",
                   str(out / "approval.json"), "--max-calls", str(args.max_calls), "--s1-arm", "d1-2b-local",
                   "--no-finish", "--wall-limit-s", str(args.wall_limit_s), "--max-age-s", str(args.max_age_s),
                   "--objective", spec["objective"]]
        if not args.no_game_view:
            command.append("--s1-game-view")
        run_started = time.monotonic()
        code = _run(command, logs / "playtest-run.log", env, args.wall_limit_s + 300)
        report["steps"]["run"] = {"exit": code, "seconds": round(time.monotonic() - run_started, 2)}
        summary_path = run_dir / "summary.json"
        report["summary"] = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else None
        frames = sorted((game / "Temp" / "playtest-bridge-frames").glob("f-*.jpg"))
        report["frames_in_window"] = len(frames)
        kept = out / "frames"
        kept.mkdir()
        for frame in frames[::max(1, len(frames) // 24)]:
            shutil.copy2(frame, kept / frame.name)
        # playtest-run exits non-zero for any stop short of a finish, a reached call cap included; its own
        # summary says how it ended, so the trial completed whenever that summary exists.
        report["status"] = "completed" if report["summary"] is not None else "run_failed"
    except Exception as exc:  # noqa: BLE001 - the report records any failure
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for process in reversed(processes):
            process.stop()
        shutil.rmtree(profile, ignore_errors=True)
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        _write(out / "report.json", report)
    print(json.dumps({"report": str(out / "report.json"), "status": report["status"], "error": report.get("error")}))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
