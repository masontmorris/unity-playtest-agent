"""Does System Zero perceive motion in a game clip? Narrow questions whose answers the trace measured.

Added to the witness check after Part 2 (24 windows, P(problem) 0.41-0.56 on all, `kind` and `when` "none"
throughout) and before Part 1 is run. Bar, written before the calls: >= 90% of judgments right means video
perception works and the broad "is there a bug?" question was the problem; <= 70% means the video channel isn't
usable for the witness; in between is mixed. Truth comes from the trace's camera readings inside each window
(alignment is approximate: the recorder's timestamps are JPEG completion times). Reuses Part 2's clips.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from playtest.bench import replay, system_zero

_spec = importlib.util.spec_from_file_location("system_zero_witness", Path(__file__).with_name("system_zero_witness.py"))
wit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wit)

QUESTIONS = {
    "moves": {"type": "boolean", "instructions": "Does the camera travel through the level during the clip, so the "
              "viewpoint ends somewhere clearly different from where it started?",
              "criteria": {"true": "the viewpoint travels", "false": "the viewpoint stays in about the same place"}},
    "rises": {"type": "boolean", "instructions": "Does the camera end the clip clearly higher than it started, as when "
              "climbing?", "criteria": {"true": "it ends clearly higher", "false": "it does not end clearly higher"}},
    "drops": {"type": "boolean", "instructions": "Does the camera end the clip clearly lower than it started, as when "
              "falling?", "criteria": {"true": "it ends clearly lower", "false": "it does not end clearly lower"}},
    "turns": {"type": "boolean", "instructions": "Does the view turn left or right by a large angle, more than about "
              "30 degrees, at some point in the clip?",
              "criteria": {"true": "the view turns by a large angle", "false": "the view never turns that much"}},
    "outside": {"type": "boolean", "instructions": "Does the clip show the view outside the playable level, for "
                "example below the floor or in empty space?",
                "criteria": {"true": "the view is outside the level", "false": "the view stays inside the level"}},
}
CLIPS = ("s09-climb", "s07-fall", "s10-press", "s09-walk-off")
BOUND = Decimal("0.021")


def truth(session: Any, start: float, duration: float) -> dict[str, Any]:
    readings = []
    for o in session.observations:
        at = session.offset(o["wall_utc"])
        camera = ((o["body"].get("public") or {}).get("camera") or {}).get("position")
        yaw = (o["body"].get("public") or {}).get("yaw")
        if start <= at <= start + duration and camera:
            readings.append((camera, yaw))
    if len(readings) < 2:
        return {"readings": len(readings)}
    (first, _), (last, _) = readings[0], readings[-1]
    travel = sum((a - b) ** 2 for a, b in zip(first, last)) ** 0.5
    yaws, unwrapped = [y for _, y in readings if isinstance(y, (int, float))], []
    for y in yaws:
        if unwrapped:
            step = (y - unwrapped[-1] + 180) % 360 - 180
            unwrapped.append(unwrapped[-1] + step)
        else:
            unwrapped.append(y)
    return {"readings": len(readings), "travel": round(travel, 2), "dy": round(last[1] - first[1], 2),
            "yaw_range": round(max(unwrapped) - min(unwrapped), 1) if unwrapped else None,
            "min_y": round(min(c[1] for c, _ in readings), 2),
            "answers": _answers(travel, last[1] - first[1], (max(unwrapped) - min(unwrapped)) if unwrapped else None,
                                min(c[1] for c, _ in readings))}


def _band(value: float | None, low: float, high: float) -> bool | None:
    """True above the band, False below it, None (unscored) inside it: a borderline reading isn't a fair truth."""
    if value is None or low <= value <= high:
        return None
    return value > high


def _answers(travel: float, dy: float, yaw_range: float | None, min_y: float) -> dict[str, bool | None]:
    # Bands fixed before any call: travel 0.5-2.0 units, |dy| 0.5-1.5, turn 20-45 degrees.
    return {"moves": _band(travel, 0.5, 2.0), "rises": _band(dy, 0.5, 1.5), "drops": _band(-dy, 0.5, 1.5),
            "turns": _band(yaw_range, 20.0, 45.0), "outside": min_y < 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run", "score"))
    for name in ("sessions", "windows", "witness", "directory", "approval"):
        parser.add_argument(f"--{name}", type=Path)
    args = parser.parse_args(argv)
    windows = {w["id"]: w for w in json.loads(args.windows.read_text(encoding="utf-8"))["windows"]}
    plan_path = args.directory / "plan.json"
    if args.command == "plan":
        # The four named clips plus the random window with the most travel and the one with the widest turn.
        rows = {}
        for wid, w in windows.items():
            if w["session"].startswith("b04-hunt"):
                rows[wid] = truth(wit.Session(args.sessions / w["session"]), w["start_s"], w["duration_s"])
        randoms = [wid for wid in rows if "-r" in wid and rows[wid].get("answers")]
        pick = list(CLIPS) + [max(randoms, key=lambda i: rows[i]["travel"]),
                              max(randoms, key=lambda i: rows[i]["yaw_range"] or 0)]
        args.directory.mkdir(parents=True, exist_ok=True)
        plan = {"version": 1, "questions": QUESTIONS, "bar": {"works": 0.9, "unusable": 0.7},
                "clips": {wid: rows[wid] for wid in dict.fromkeys(pick)}}
        plan_path.write_text(json.dumps(plan, indent=1), encoding="utf-8")
        print(json.dumps(plan["clips"], indent=1))
        return 0
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    calls = args.directory / "calls"
    calls.mkdir(exist_ok=True)
    if args.command == "run":
        approval, _ = replay._load(args.approval)
        replay.validate_approval(approval, [system_zero.ARM])
        spent = [json.loads(p.read_text(encoding="utf-8")) for p in (args.witness / "calls").glob("*.json")]
        mine = [json.loads(p.read_text(encoding="utf-8")) for p in calls.glob("*.json")]
        for wid in plan["clips"]:
            if any(r["window_id"] == wid for r in mine):
                continue
            reserved = sum((Decimal(r["reserved_usd"]) for r in spent + mine), Decimal(0))
            if reserved + BOUND > Decimal(str(approval["max_cost_usd"])):
                print("envelope reached", file=sys.stderr)
                break
            witness_row = json.loads((args.witness / "calls" / f"{wid}.json").read_text(encoding="utf-8"))
            clip = (args.witness / "clips" / f"{wid}.mp4").read_bytes()
            body = wit.witness.request(clip, duration_s=witness_row["clip"]["duration_s"], speed=1.0,
                                       inputs=witness_row["inputs"])
            body["questions"] = QUESTIONS
            row = {"window_id": wid, "reserved_usd": str(BOUND), "status": "in_flight"}
            path = calls / f"{wid}.json"
            path.write_text(json.dumps(row), encoding="utf-8")
            status, data, latency_ms, problem = system_zero.send(system_zero.ARM, body)
            answers = (data or {}).get("answers") if status == 200 else None
            row.update(status="finished", http_status=status, latency_ms=latency_ms, problem=problem,
                       usage=(data or {}).get("usage"), answers=answers)
            path.write_text(json.dumps(row, indent=1), encoding="utf-8")
            mine.append(row)
            print(f"{wid}: HTTP {status} {latency_ms:.0f} ms")
            if status != 200:
                return 1
        return 0
    right = judged = 0
    for wid, info in plan["clips"].items():
        row = json.loads((calls / f"{wid}.json").read_text(encoding="utf-8"))
        line = []
        for name, expected in info["answers"].items():
            answer = (row.get("answers") or {}).get(name) or {}
            got, p = answer.get("decision"), answer.get("probability")
            if expected is None or not isinstance(got, bool):
                line.append(f"{name}=unknown")
                continue
            judged += 1
            right += int(got is expected)
            line.append(f"{name}:{'ok' if got is expected else 'WRONG'}(truth {expected}, p {p:.2f})")
        print(f"{wid:14} " + "  ".join(line))
    print(f"accuracy {right}/{judged}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
