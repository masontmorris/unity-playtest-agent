"""System Zero witness offline check: windows from recorded sessions (docs/design/system-zero-witness-check.md).

`select` writes the window list by a fixed, seeded rule from trace measurements. `run` cuts each clip, reserves
a ledger row, sends one decide and records the decoded answers. `score` reports flags against the labels. The
labels are from measurement: b04-build windows where b04 never showed are negatives; the session 7 fall and the
Day 6 frozen-pickup windows are exploratory, reported but outside the bar. Never retries; an in-flight row stops.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from playtest.bench import replay, system_zero
from playtest.bench.record import digest
from playtest.core.context import canonical
from playtest.runner import witness

SEED = 3111
WINDOW_S = 9.5  # stays under System Zero's 10 s video cap once encoded
BOUND = Decimal("0.021")
NEGATIVE_SESSIONS = (1, 2, 3, 4, 5, 6, 8, 9, 10)  # b04 build; no session before 11 reached a 0.25 riser
RANDOM_NEGATIVES = 17
PADS = ((2.50, -13.04), (4.53, -13.04))  # the two weapon pads (dev key b02)


def _utc(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


class Session:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        recording = json.loads((directory / "recording" / "recording.json").read_text(encoding="utf-8"))
        self.video = directory / "recording" / recording["video"]["file"]
        self.video_sha256 = recording["video"]["sha256"]
        self.first = _utc(recording["first_frame_utc"])
        self.duration = float(recording["video"]["encoded_duration_s"])
        self.observations: list[dict[str, Any]] = []
        self.executions: list[dict[str, Any]] = []
        for line in (directory / "trace.jsonl").read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if entry["kind"] == "observation":
                self.observations.append(entry)
            elif entry["kind"] == "execution":
                self.executions.append(entry)

    def offset(self, wall_utc: str) -> float:
        """Seconds into the video; approximate, as the recorder's timestamps are JPEG completion times."""
        return _utc(wall_utc) - self.first

    def turn_offset(self, turn: int) -> float:
        """The video offset of the turn's observation; every turn has one, not every turn executes."""
        entry = next(e for e in self.observations if e["turn"] == turn)
        return self.offset(entry["wall_utc"])

    @staticmethod
    def start_of(execution: dict[str, Any]) -> str:
        records = execution["body"].get("records") or []
        times = [r.get("wall_utc") for r in records if isinstance(r, dict) and r.get("wall_utc")]
        return min(times, key=_utc) if times else execution["wall_utc"]

    def inputs(self, start: float, duration: float, speed: float) -> list[dict[str, Any]]:
        out = []
        for entry in self.executions:
            at = self.offset(self.start_of(entry)) - start
            if 0 <= at < duration:
                request = entry["body"].get("request") or {}
                lease = request.get("lease_s")
                out.append({"t_s": round(at / speed, 2), "action": request.get("action"),
                            "held_s": round(lease / speed, 2) if isinstance(lease, (int, float)) else None})
        return out


def select(args: argparse.Namespace) -> dict[str, Any]:
    root = args.sessions
    b04 = {n: Session(root / "b04-hunt" / f"session-{n:02d}") for n in (*NEGATIVE_SESSIONS, 7)}
    windows: list[dict[str, Any]] = []

    def add(wid: str, session: str, start: float, label: str, note: str) -> None:
        windows.append({"id": wid, "session": session, "start_s": round(max(0.0, start), 3), "duration_s": WINDOW_S,
                        "speed": 1.0, "label": label, "note": note})

    add("s09-climb", "b04-hunt/session-09", b04[9].turn_offset(170) - 1.0, "negative",
        "hard: climbs the main flight (turns 170-179)")
    add("s09-walk-off", "b04-hunt/session-09", b04[9].turn_offset(180) - 4.0, "negative",
        "hard: walks off the flight's top and falls to the floor (turn 180)")
    add("s10-press", "b04-hunt/session-10", b04[10].turn_offset(100), "negative",
        "hard: pushes a 1.1-unit wall face (turns 62-160)")
    rng = random.Random(SEED)
    taken = {(w["session"], w["start_s"]) for w in windows}
    while sum(w["label"] == "negative" for w in windows) < 3 + RANDOM_NEGATIVES:
        n = rng.choice(NEGATIVE_SESSIONS)
        name = f"b04-hunt/session-{n:02d}"
        start = round(rng.uniform(5.0, b04[n].duration - 15.0), 3)
        if any(s == name and abs(start - t) < WINDOW_S for s, t in taken):
            continue
        taken.add((name, start))
        add(f"s{n:02d}-r{start:.0f}", name, start, "negative", "random b04-build window")
    fall = next(o for o in b04[7].observations
                if (((o["body"].get("public") or {}).get("camera") or {}).get("position") or [0, 1])[1] < 0)
    add("s07-fall", "b04-hunt/session-07", b04[7].offset(fall["wall_utc"]) - 5.0, "exploratory",
        "falls below the floor (camera y < 0 at turn %d)" % fall["turn"])
    for n in range(1, 6):
        session = Session(root / "day6-gate" / f"session-{n:02d}")
        for o in session.observations:
            public = o["body"].get("public") or {}
            timers = [public.get("dispenser_a_timer"), public.get("dispenser_b_timer")]
            position = (public.get("camera") or {}).get("position")
            if not position or not any(isinstance(t, (int, float)) and t < -1.0 for t in timers):
                continue
            near = min(((position[0] - x) ** 2 + (position[2] - z) ** 2) ** 0.5 for x, z in PADS)
            if near <= 8.0:
                add(f"d6-s{n:02d}-pad", f"day6-gate/session-{n:02d}", session.offset(o["wall_utc"]) - 2.0,
                    "exploratory", f"b02 build: a pad timer below -1 (item returned frozen), camera {near:.1f} from a pad")
                break
    return {"version": 1, "seed": SEED, "window_s": WINDOW_S, "windows": windows}


def _rows(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((directory / "calls").glob("*.json"))]


def run(args: argparse.Namespace) -> int:
    plan = json.loads(args.windows.read_text(encoding="utf-8"))
    approval, _ = replay._load(args.approval)
    replay.validate_approval(approval, [system_zero.ARM])
    (args.directory / "calls").mkdir(parents=True, exist_ok=True)
    (args.directory / "clips").mkdir(exist_ok=True)
    rows = _rows(args.directory) + (_rows(args.shared) if args.shared else [])
    if any(row.get("status") == "in_flight" for row in rows):
        raise ValueError("a prior call has an unknown outcome; reconcile it before resuming")
    done = {row["window_id"] for row in _rows(args.directory)}
    chosen = [w for w in plan["windows"] if not args.only or w["id"] in args.only]
    made = 0
    for window in chosen:
        if window["id"] in done:
            continue
        reserved = sum((Decimal(row["reserved_usd"]) for row in rows), Decimal(0))
        if len(rows) >= approval["max_calls"] or reserved + BOUND > Decimal(str(approval["max_cost_usd"])):
            print(f"envelope reached after {made} new calls", file=sys.stderr)
            break
        session = Session(args.sessions / window["session"])
        clip_path = args.directory / "clips" / f"{window['id']}.mp4"
        clip = witness.cut_clip(session.video, window["start_s"], window["duration_s"], clip_path, speed=window["speed"])
        inputs = session.inputs(window["start_s"], window["duration_s"], window["speed"])
        body = witness.request(clip_path.read_bytes(), duration_s=clip["duration_s"], speed=window["speed"], inputs=inputs)
        row = {"window_id": window["id"], "label": window["label"], "note": window["note"], "clip": clip,
               "source_video_sha256": session.video_sha256, "inputs": inputs, "prompt_sha256": witness.prompt_sha256(),
               "request_state_sha256": digest(canonical(body["state"])), "reserved_usd": str(BOUND), "status": "in_flight"}
        path = args.directory / "calls" / f"{window['id']}.json"
        path.write_text(json.dumps(row, indent=1), encoding="utf-8")
        try:
            status, data, latency_ms, problem = system_zero.send(system_zero.ARM, body)
        except Exception as exc:  # noqa: BLE001 - the outcome is unknown; keep the row and stop
            row.update(status="finished", http_status=None, problem=f"adapter error: {type(exc).__name__}")
            path.write_text(json.dumps(row, indent=1), encoding="utf-8")
            print(f"stopped: {row['problem']}", file=sys.stderr)
            return 1
        row.update(status="finished", http_status=status, latency_ms=latency_ms, problem=problem,
                   resolved_model=(data or {}).get("model"), usage=(data or {}).get("usage"),
                   answers=witness.decode(data if status == 200 else None))
        path.write_text(json.dumps(row, indent=1), encoding="utf-8")
        rows.append(row)
        made += 1
        print(f"{window['id']}: HTTP {status} {latency_ms:.0f} ms problem_visible={row['answers']['problem_visible']}")
        if status != 200:
            print(f"stopped: HTTP {status} {problem}", file=sys.stderr)
            return 1
    print(f"new calls: {made}")
    return 0


def score(args: argparse.Namespace) -> dict[str, Any]:
    rows = [r for r in _rows(args.directory) if r.get("status") == "finished" and r.get("answers")]
    report: dict[str, Any] = {"negatives": {"flagged": 0, "scored": 0, "unknown": 0, "windows": []}, "exploratory": []}
    for row in rows:
        visible = row["answers"]["problem_visible"]
        item = {"id": row["window_id"], "note": row["note"],
                "problem_visible": visible.get("decision", "unknown"), "p_true": visible.get("probability"),
                "kind": row["answers"]["kind"].get("decision", "unknown"),
                "when": row["answers"]["when"].get("decision", "unknown"), "latency_ms": row.get("latency_ms"),
                "input_tokens": (row.get("usage") or {}).get("input_tokens"),
                "media_tokens": (row.get("usage") or {}).get("media_tokens")}
        if row["label"] == "negative":
            group = report["negatives"]
            if "decision" not in visible:
                group["unknown"] += 1
            else:
                group["scored"] += 1
                group["flagged"] += int(visible["decision"] is True)
            group["windows"].append(item)
        else:
            report["exploratory"].append(item)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("select", "run", "score"))
    parser.add_argument("--sessions", type=Path, help="the runs/ folder holding b04-hunt/ and day6-gate/")
    parser.add_argument("--windows", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--shared", type=Path, help="another ledger directory spending from the same approval")
    parser.add_argument("--only", nargs="*", help="window ids to run (default: all)")
    args = parser.parse_args(argv)
    if args.command == "select":
        args.windows.write_text(json.dumps(select(args), indent=1), encoding="utf-8")
        print(f"wrote {args.windows}")
        return 0
    if args.command == "run":
        return run(args)
    print(json.dumps(score(args), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
