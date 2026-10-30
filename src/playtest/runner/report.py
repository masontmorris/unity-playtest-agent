"""Export a run's findings as a report: JSON for tools, Markdown for people (#39).

Every input is read, never written:
- the run folder: `trace.jsonl` (the findings, the observations they cite and the help results), `run.json` (the
  run's configuration and counts), `calls/` (one record per paid call, with its measured cost or none),
  `clips/t<turn>/` (the clip each frontier request sent) and `recording/` (the whole-session video, if recorded);
- any number of `reproduce` output folders (#38). Each is matched to a finding by id and to this trace by its
  SHA-256. One that does not match is listed with the reason and never applied.

A finding is one claim: under these conditions, this state is wrong. The report keeps each finding as recorded
and adds what the replays measured. Nothing is filled in: a missing cost, clip, video frame or replay is reported
as missing with the reason, and unscored or invalid attempts stay in the report with theirs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from playtest.roles.checks import field_value
from playtest.runner.reproduce import (OVERRIDE_REASON, UNSCORED_REASON, latest_findings, overrides_finding,
                                       read_trace)

SCHEMA_VERSION = 1
STATE_RANK = {"repro_attempted": 1, "reproduced": 2}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _wall(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _usd(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def _money(amount: Decimal) -> float:
    return float(round(amount, 6))


def _display(path: Path, base: Path | None = None) -> str:
    """A path relative to `base` (or the working directory) when it is inside it, so reports carry no home folder."""
    resolved = path.resolve()
    for root in ([base] if base is not None else []) + [Path.cwd()]:
        try:
            return resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
    return str(path)


# --- run ---------------------------------------------------------------------------------------


def load_calls(run_dir: Path) -> list[dict[str, Any]]:
    return [row for row in (_read_json(p) for p in sorted((run_dir / "calls").glob("*.json"))) if isinstance(row, dict)]


def run_config(entries: Sequence[Mapping[str, Any]], run_json: Mapping[str, Any], trace_path: Path,
               trace_sha: str) -> dict[str, Any]:
    run = entries[0]["body"] if entries and entries[0].get("kind") == "run" else {}
    end = next((e["body"] for e in reversed(entries) if e.get("kind") == "end"), {})
    manifest = run_json.get("manifest") or {}
    return {
        "run_id": run.get("run_id"), "game_project": run.get("game_project"), "game_commit": run.get("game_commit"),
        "game_worktree": run.get("game_worktree"), "game_modified_paths": run.get("game_modified_paths"),
        "unity_version": run.get("unity_version"), "scene": run.get("scene"), "clock_mode": run.get("clock_mode"),
        "input_mode": run.get("input_mode"), "reset_method": run.get("reset_method"),
        "bridge_version": run.get("bridge_version"), "core_version": run.get("core_version"),
        "objective": run.get("objective"), "budget": run.get("budget"), "models": run.get("models"),
        "manifest": {"fingerprint": run.get("manifest_fingerprint"), "sha256": manifest.get("sha256"),
                     "catalog_sha256": manifest.get("catalog_sha256"), "origin": manifest.get("origin")},
        "s1": run_json.get("s1"), "frontier": run_json.get("frontier"),
        "status": run_json.get("status", end.get("status")), "ending": run_json.get("ending"),
        "error": run_json.get("error", end.get("error")), "turns": end.get("turns", run_json.get("turns")),
        "outcomes": run_json.get("outcomes"), "adherence": run_json.get("adherence"),
        "started_utc": entries[0].get("wall_utc") if entries else None,
        "ended_utc": entries[-1].get("wall_utc") if entries else None,
        "trace": {"path": _display(trace_path), "sha256": trace_sha, "entries": len(entries)},
        "run_json_found": bool(run_json),
    }


def cost_summary(calls: Sequence[Mapping[str, Any]], run_json: Mapping[str, Any],
                 reproductions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Measured cost per role, and the calls whose cost is unknown with what was reserved for them.

    An unknown cost is never counted as zero. `upper_bound_usd` adds the reservations of the unknown calls to the
    measured total, and is null when an unknown call has no reservation.
    """
    out: dict[str, Any] = {}
    measured_total, reserved_total, bounded = Decimal(0), Decimal(0), True
    for role in ("s1", "frontier"):
        rows = [c for c in calls if (c.get("role") == "frontier") == (role == "frontier")]
        costs = [_usd(c.get("cost_usd")) for c in rows]
        known = [cost for cost in costs if cost is not None]
        reserved = [_usd(c.get("reserved_usd")) for c, cost in zip(rows, costs) if cost is None]
        role_bounded = all(r is not None for r in reserved)
        out[role] = {
            "calls": len(rows), "by_status": dict(Counter(str(c.get("status")) for c in rows)),
            "models": sorted({str(c.get("resolved_model") or c.get("requested_model") or c.get("arm")) for c in rows}),
            "measured_calls": len(known), "measured_usd": _money(sum(known, Decimal(0))),
            "unknown_calls": len(reserved),
            "unknown_reserved_usd": _money(sum(reserved, Decimal(0))) if role_bounded else None,
            "counted_by_run": (run_json.get(role) or {}).get("calls"),
        }
        measured_total += sum(known, Decimal(0))
        if role_bounded:
            reserved_total += sum(reserved, Decimal(0))
        bounded = bounded and role_bounded
    unknown = out["s1"]["unknown_calls"] + out["frontier"]["unknown_calls"]
    out["replays"] = {"runs": len(reproductions),
                      "model_calls": sum(int(r["report"].get("model_calls") or 0) for r in reproductions)}
    out["total"] = {"calls": out["s1"]["calls"] + out["frontier"]["calls"], "measured_usd": _money(measured_total),
                    "unknown_calls": unknown,
                    "upper_bound_usd": _money(measured_total + reserved_total) if bounded else None}
    out["not_included"] = "the preflight that produced the manifest; it is not part of this run"
    return out


def recording_info(run_dir: Path) -> dict[str, Any] | None:
    rec = _read_json(run_dir / "recording" / "recording.json")
    if not isinstance(rec, dict):
        return None
    video = rec.get("video") or {}
    path = run_dir / "recording" / video["file"] if isinstance(video.get("file"), str) else None
    return {"path": _display(path, run_dir) if path is not None else None,
            "exists": bool(path is not None and path.exists()), "sha256": video.get("sha256"),
            "encoded_duration_s": video.get("encoded_duration_s"), "first_frame_utc": rec.get("first_frame_utc"),
            "last_frame_utc": rec.get("last_frame_utc"), "caveat": rec.get("caveat"),
            "reason": rec.get("reason")}


def video_offset(recording: Mapping[str, Any] | None, wall: Any) -> float | None:
    """Seconds into the session video at a bridge wall time, or None outside the recorded frames.

    The video's frame durations follow the frames' file times, so an offset from the first frame is approximate
    in the same way; the recording's caveat says so.
    """
    if recording is None or not recording.get("exists"):
        return None
    first, last, seen = _wall(recording.get("first_frame_utc")), _wall(recording.get("last_frame_utc")), _wall(wall)
    if first is None or last is None or seen is None or not first <= seen <= last:
        return None
    return round((seen - first).total_seconds(), 2)


# --- findings ----------------------------------------------------------------------------------


def proposals(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every adopted frontier answer that proposed a finding, recorded or not, with its request turn."""
    out = []
    for entry in entries:
        body = entry.get("body") or {}
        proposed = (body.get("result") or {}).get("candidate_finding") if entry.get("kind") == "help" else None
        if body.get("event") == "adopted" and isinstance(proposed, Mapping):
            out.append({"finding_id": proposed.get("finding_id"), "reason": proposed.get("reason"),
                        "request_turn": body.get("opened_turn"), "help_trace_seq": entry.get("trace_seq")})
    return out


def clip_for(run_dir: Path, calls: Sequence[Mapping[str, Any]], request_turn: Any) -> dict[str, Any]:
    """The clip the frontier saw when it proposed the finding, checked against the SHA-256 its call recorded."""
    call = next((c for c in calls if c.get("role") == "frontier" and c.get("request_turn") == request_turn), None)
    if call is None:
        return {"path": None, "reason": f"no frontier call record for request turn {request_turn}"}
    meta = call.get("clip")
    if not isinstance(meta, Mapping):
        return {"path": None, "reason": call.get("clip_unavailable_reason") or "the call recorded no clip"}
    path = run_dir / "clips" / f"t{request_turn}" / "clip.mp4"
    exists = path.exists()
    return {"path": _display(path, run_dir), "exists": exists, "sha256": meta.get("sha256"),
            "sha256_matches": (_sha(path.read_bytes()) == meta.get("sha256")) if exists else None,
            "encoded_duration_s": meta.get("encoded_duration_s"), "start_wall_utc": meta.get("start_wall_utc"),
            "end_wall_utc": meta.get("end_wall_utc"), "caveat": meta.get("caveat"),
            "reason": None if exists else "the clip file is not in the run folder"}


def evidence(entries_by_seq: Mapping[int, Mapping[str, Any]], finding: Mapping[str, Any],
             recording: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    check = finding.get("repro_check") or {}
    rows = []
    for seq in finding.get("evidence_seq") or []:
        entry = entries_by_seq.get(seq)
        if entry is None:
            rows.append({"trace_seq": seq, "kind": None, "reason": "not in the trace"})
            continue
        row: dict[str, Any] = {"trace_seq": seq, "kind": entry.get("kind"), "turn": entry.get("turn")}
        body = entry.get("body") or {}
        if entry.get("kind") == "observation":
            row.update(bridge_seq=body.get("seq"), game_s=body.get("game_s"), wall_utc=body.get("wall_utc"),
                       scene=body.get("scene"), video_s=video_offset(recording, body.get("wall_utc")))
            if isinstance(check.get("field"), str):
                row["check_field"] = check["field"]
                row["check_value"] = field_value(body.get("public") or {}, check["field"].removeprefix("public."))
        rows.append(row)
    return rows


def load_reproductions(directories: Sequence[Path]) -> list[dict[str, Any]]:
    out = []
    for directory in directories:
        report, plan = _read_json(directory / "reproduce.json"), _read_json(directory / "plan.json")
        problem = None if isinstance(report, dict) and isinstance(plan, dict) else "reproduce.json or plan.json is missing"
        out.append({"path": _display(directory), "report": report or {}, "plan": plan or {}, "problem": problem})
    return out


def replay(record: Mapping[str, Any]) -> dict[str, Any]:
    """One `reproduce` run as the report shows it: every attempt, including the unscored and invalid ones.

    The operator-override rule is applied here too, so a report written before `reproduce` applied it cannot
    move a finding on a check that was not the finding's own.
    """
    report, plan = record["report"], record["plan"]
    summary = report.get("summary") or {}
    overridden = overrides_finding(plan)
    update = None if overridden else report.get("finding_update")
    update_reason = (None if update is not None else OVERRIDE_REASON if overridden
                     else report.get("finding_update_reason") or UNSCORED_REASON)
    attempts = []
    for attempt in report.get("attempts") or []:
        deviations = [abs(row["deviation_s"]) for row in attempt.get("inputs") or [] if row.get("deviation_s") is not None]
        attempts.append({"number": attempt.get("number"),
                         "result": attempt.get("result") if attempt.get("valid") else "invalid",
                         "reason": attempt.get("reason"), "baseline": attempt.get("baseline"),
                         "baseline_satisfied": attempt.get("baseline_satisfied"),
                         "readings": len(attempt.get("readings") or []),
                         "inputs_replayed": len(attempt.get("inputs") or []),
                         "max_abs_deviation_s": round(max(deviations), 3) if deviations else None})
    return {"path": record["path"], "finished_at_utc": report.get("finished_at_utc"), "status": report.get("status"),
            "check": report.get("repro_check", plan.get("repro_check")),
            "check_source": report.get("repro_check_source", plan.get("repro_check_source")),
            "check_kind": plan.get("repro_check_kind"), "reset": report.get("reset"),
            "clock_mode": report.get("clock_mode"), "inputs": len(plan.get("inputs") or []),
            "skipped_inputs": plan.get("skipped_inputs"), "evidence_turn": plan.get("evidence_turn"),
            "planned": summary.get("planned"), "attempted": summary.get("attempted"), "valid": summary.get("valid"),
            "k": summary.get("k"), "n": summary.get("n"), "unknown": summary.get("unknown"),
            "invalid": summary.get("invalid"), "input_timing": summary.get("input_timing"),
            "basis": summary.get("basis"), "attempts": attempts,
            "changes_state": update is not None, "state_after": (update or {}).get("state"),
            "finding_update_reason": update_reason,
            "model_calls": report.get("model_calls")}


def uncertainty(finding: Mapping[str, Any], replays: Sequence[Mapping[str, Any]],
                clip: Mapping[str, Any] | None) -> list[str]:
    notes = []
    if finding.get("uncertainty"):
        notes.append(f"Frontier: {finding['uncertainty']}")
    if not replays:
        notes.append("No replay has been run for this finding, so it is not reproduced.")
    elif not any(r["changes_state"] for r in replays):
        reasons = sorted({r["finding_update_reason"] or "no reason recorded" for r in replays})
        notes.append(f"No replay changed the finding's state: {'; '.join(reasons)}.")
    for r in replays:
        label = f"Replay {r['path']}"
        unknown = [a for a in r["attempts"] if a["result"] == "unknown"]
        invalid = [a for a in r["attempts"] if a["result"] == "invalid"]
        if unknown:
            notes.append(f"{label}: {len(unknown)} of {len(r['attempts'])} attempts unknown "
                         f"({'; '.join(sorted({a['reason'] or '' for a in unknown}))}).")
        if invalid:
            notes.append(f"{label}: {len(invalid)} attempts invalid "
                         f"({'; '.join(sorted({a['reason'] or '' for a in invalid}))}).")
        timing = (r.get("input_timing") or {}).get("max_abs_deviation_s")
        if timing is not None:
            notes.append(f"{label}: inputs replayed in real time, within {timing} s of their recorded offsets.")
        if r["planned"] is not None and r["attempted"] is not None and r["attempted"] < r["planned"]:
            notes.append(f"{label}: {r['attempted']} of {r['planned']} planned attempts ran.")
    if clip is None or clip.get("path") is None:
        notes.append(f"No clip is linked to this finding: {clip.get('reason') if clip else 'its proposal is not in the trace'}.")
    elif clip.get("sha256_matches") is False:
        notes.append("The clip file differs from the one the frontier was sent (SHA-256 mismatch).")
    return notes


def build_report(run_dir: Path, *, trace_path: Path | None = None, reproduce_dirs: Sequence[Path] = (),
                 finding_ids: Sequence[str] | None = None) -> dict[str, Any]:
    trace_path = trace_path or run_dir / "trace.jsonl"
    raw = trace_path.read_bytes()
    trace_sha = _sha(raw)
    entries = read_trace(trace_path)
    by_seq = {entry["trace_seq"]: entry for entry in entries}
    run_json = _read_json(run_dir / "run.json") or {}
    calls = load_calls(run_dir)
    recording = recording_info(run_dir)
    proposed = proposals(entries)
    loaded = load_reproductions(reproduce_dirs)
    latest = latest_findings(entries)
    if finding_ids:
        missing = [fid for fid in finding_ids if fid not in latest]
        if missing:
            raise ValueError(f"the trace has no finding {', '.join(missing)}")
    chosen = [fid for fid in latest if not finding_ids or fid in finding_ids]

    matched, unmatched = [], []
    for record in loaded:
        fid = record["report"].get("finding_id") or record["plan"].get("finding_id")
        if record["problem"]:
            unmatched.append({"path": record["path"], "finding_id": fid, "reason": record["problem"]})
        elif record["plan"].get("trace_sha256") != trace_sha:
            unmatched.append({"path": record["path"], "finding_id": fid,
                              "reason": f"made from a different trace (SHA-256 {record['plan'].get('trace_sha256')})"})
        elif fid not in chosen:
            unmatched.append({"path": record["path"], "finding_id": fid, "reason": "its finding is not in this report"})
        else:
            matched.append(record)

    findings = []
    for fid in chosen:
        recorded = latest[fid]
        body = recorded["finding"]
        replays = [replay(r) for r in matched if (r["report"].get("finding_id") or r["plan"].get("finding_id")) == fid]
        state = body.get("state")
        changing = [r for r in replays if r["changes_state"]]
        for r in changing:
            if STATE_RANK.get(r["state_after"], 0) > STATE_RANK.get(state, 0):
                state = r["state_after"]
        same_check = len({json.dumps(r["check"], sort_keys=True) for r in changing}) == 1
        proposal = next((p for p in proposed if p["finding_id"] == fid and p["reason"] is None), None)
        clip = clip_for(run_dir, calls, proposal["request_turn"]) if proposal is not None else None
        findings.append({
            "finding_id": fid, "recorded_state": body.get("state"), "state": state,
            "title": body.get("title"), "severity": body.get("severity"), "severity_reason": body.get("severity_reason"),
            "expected_source": body.get("expected_source"), "expected": body.get("expected"),
            "actual": body.get("actual"), "conditions": body.get("conditions"), "steps": body.get("steps") or [],
            "repro_check": body.get("repro_check"), "uncertainty": body.get("uncertainty"),
            "recorded_at_trace_seq": recorded["trace_seq"],
            "author": by_seq.get(recorded["trace_seq"], {}).get("author"),
            "proposed_at_request_turn": proposal["request_turn"] if proposal else None,
            "evidence": evidence(by_seq, body, recording), "clip": clip,
            "replays": replays,
            "reproduced": {"k": sum(r["k"] or 0 for r in changing), "n": sum(r["n"] or 0 for r in changing)}
                          if changing and same_check else None,
            "remaining_uncertainty": uncertainty(body, replays, clip),
        })
    return {
        "schema_version": SCHEMA_VERSION, "kind": "playtest_report", "generated_at_utc": _utcnow(),
        "run": run_config(entries, run_json, trace_path, trace_sha),
        "findings": findings,
        "proposed_not_recorded": [p for p in proposed if p["reason"] is not None],
        "unmatched_replays": unmatched,
        "session_video": recording,
        "cost": cost_summary(calls, run_json, matched),
    }


# --- markdown ----------------------------------------------------------------------------------


def _cell(value: Any) -> str:
    text = "unknown" if value is None else str(value)
    return text.replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def _check(check: Mapping[str, Any] | None) -> str:
    if not check:
        return "none"
    value = "" if check.get("value") is None else " " + json.dumps(check["value"])
    tolerance = f" (tolerance {check['tolerance']})" if check.get("tolerance") is not None else ""
    return f"`{check.get('field')} {check.get('op')}{value}`{tolerance}".replace("|", "\\|")


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def _usd_text(value: float | None) -> str:
    return "unknown" if value is None else f"${value:.4f}"


def render_markdown(report: Mapping[str, Any]) -> str:
    run, cost = report["run"], report["cost"]
    commit = (run.get("game_commit") or "unknown")[:7]
    lines = [f"# Playtest report: {run.get('game_project') or 'unknown game'} at {commit}", ""]
    s1, frontier = run.get("s1") or {}, run.get("frontier") or {}
    lines += [f"Run `{run.get('run_id')}`, started {run.get('started_utc')}. {run.get('turns')} turns, status "
              f"{run.get('status')} ({run.get('ending')}). S1: {s1.get('provider')} {s1.get('model')}, "
              f"{s1.get('calls')} calls. Frontier: {frontier.get('model') if frontier.get('enabled') else 'off'}"
              f"{', ' + str(frontier.get('calls')) + ' calls' if frontier.get('enabled') else ''}.", ""]
    total = cost["total"]
    lines += [f"{len(report['findings'])} finding(s). Measured cost {_usd_text(total['measured_usd'])}; "
              f"{total['unknown_calls']} call(s) of unknown cost. Replays made {cost['replays']['model_calls']} model "
              "calls.", ""]
    if not report["findings"]:
        lines += ["No findings were recorded in this run.", ""]
    for finding in report["findings"]:
        lines += _finding_markdown(finding)
    if report["proposed_not_recorded"]:
        lines += ["## Proposed but not recorded", ""]
        lines += [f"- {p['finding_id']} (request turn {p['request_turn']}): {p['reason']}"
                  for p in report["proposed_not_recorded"]]
        lines.append("")
    if report["unmatched_replays"]:
        lines += ["## Replays not applied", ""]
        lines += [f"- {u['path']} ({u['finding_id']}): {u['reason']}" for u in report["unmatched_replays"]]
        lines.append("")
    lines += _run_markdown(run, report.get("session_video"))
    lines += _cost_markdown(cost)
    return "\n".join(lines).rstrip() + "\n"


def _finding_markdown(f: Mapping[str, Any]) -> list[str]:
    state = f["state"]
    if f["reproduced"] is not None:
        state += f" ({f['reproduced']['k']} of {f['reproduced']['n']})"
    if f["state"] != f["recorded_state"]:
        state += f"; recorded as {f['recorded_state']}"
    lines = [f"## {f['finding_id']}: {_cell(f['title'])}", "", "| | |", "|---|---|",
             f"| State | {_cell(state)} |",
             f"| Severity | {_cell(f['severity'])}: {_cell(f['severity_reason'])} |",
             f"| Expected ({_cell(f['expected_source'])}) | {_cell(f['expected'])} |",
             f"| Actual | {_cell(f['actual'])} |",
             f"| Conditions | {_cell(f['conditions'])} |",
             f"| What a replay must show | {_check(f['repro_check'])} |", ""]
    if f["steps"]:
        lines += ["**Steps**", ""] + [f"{i}. {step}" for i, step in enumerate(f["steps"], 1)] + [""]
    lines += ["**Reproduction**", ""]
    if not f["replays"]:
        lines += ["No replay has been run.", ""]
    for r in f["replays"]:
        source = "the finding's check" if r["check_source"] == "finding" else f"{r['check_source']} check"
        effect = (f"Moved the finding to {r['state_after']}." if r["changes_state"]
                  else f"Did not change the finding's state: {r['finding_update_reason']}.")
        lines += [f"Replay `{r['path']}`, {source} {_check(r['check'])}: {r['status']}, {r['k']} of {r['n']} "
                  f"reproduced, {r['unknown']} unknown, {len(r['invalid'] or [])} invalid; reset {r['reset']}, "
                  f"{r['inputs']} inputs. {effect}", "",
                  "| Attempt | Result | Baseline | Reason |", "|---|---|---|---|"]
        lines += [f"| {a['number']} | {a['result']} | {_cell(a['baseline'])} | {_cell(a['reason'])} |"
                  for a in r["attempts"]]
        lines.append("")
    lines += ["**Evidence**", ""]
    for row in f["evidence"]:
        if row.get("kind") == "observation":
            value = (f", `{row['check_field']}` = {_cell(json.dumps(row.get('check_value')))}"
                     if "check_field" in row else "")
            video = f", {_clock(row['video_s'])} into the session video" if row.get("video_s") is not None else ""
            game_s = row.get("game_s")
            game_s = f"{game_s:.2f}" if isinstance(game_s, (int, float)) and not isinstance(game_s, bool) else "unknown"
            lines.append(f"- trace_seq {row['trace_seq']}: observation at turn {row['turn']}, game_s {game_s}, "
                         f"scene {_cell(row.get('scene'))}{value}{video}")
        else:
            lines.append(f"- trace_seq {row['trace_seq']}: {row.get('kind') or row.get('reason')}")
    clip = f["clip"]
    if clip and clip.get("path"):
        match = {True: "SHA-256 matches", False: "SHA-256 does not match", None: "file missing"}[clip.get("sha256_matches")]
        lines.append(f"- Clip the frontier saw: `{clip['path']}` ({_cell(clip.get('encoded_duration_s'))} s, {match})")
    else:
        lines.append(f"- No clip linked: {clip.get('reason') if clip else 'the proposal is not in the trace'}")
    lines += ["", "**Remaining uncertainty**", ""] + [f"- {note}" for note in f["remaining_uncertainty"]] + [""]
    return lines


def _run_markdown(run: Mapping[str, Any], video: Mapping[str, Any] | None) -> list[str]:
    s1, frontier, budget = run.get("s1") or {}, run.get("frontier") or {}, run.get("budget") or {}
    modified = run.get("game_modified_paths") or []
    manifest = run.get("manifest") or {}
    adherence = run.get("adherence") or {}
    rows = [
        ("Game", f"{run.get('game_project')} at {run.get('game_commit')}, worktree {run.get('game_worktree')} "
                 f"({len(modified)} modified paths)"),
        ("Unity", f"{run.get('unity_version')}, scene {run.get('scene')}, input {run.get('input_mode')}, clock "
                  f"{run.get('clock_mode')}"),
        ("Objective", run.get("objective")),
        ("S1", f"{s1.get('provider')} {s1.get('model')}, {s1.get('calls')} of {s1.get('call_cap')} calls"),
        ("Frontier", f"{frontier.get('model')}, {frontier.get('calls')} of {frontier.get('call_cap')} calls"
                     if frontier.get("enabled") else "off"),
        ("Manifest", f"{(manifest.get('sha256') or manifest.get('fingerprint') or 'unknown')[:12]}: "
                     f"{manifest.get('origin') or 'origin not recorded'}"),
        ("Budget", f"${budget.get('max_usd')}, {budget.get('max_wall_s')} s wall, "
                   f"{budget.get('max_frontier_calls')} frontier calls"),
        ("Bridge, core", f"{run.get('bridge_version')}, {run.get('core_version')}"),
        ("Adherence", f"{adherence.get('rate')}, {len(adherence.get('violations') or [])} violations"),
        ("Trace", f"`{run['trace']['path']}`, {run['trace']['entries']} entries, SHA-256 {run['trace']['sha256'][:12]}"),
        ("Session video", f"`{video['path']}` ({_clock(video.get('encoded_duration_s'))})"
                          if video and video.get("exists") else "not recorded"),
    ]
    return ["## Run configuration", "", "| | |", "|---|---|"] + [f"| {k} | {_cell(v)} |" for k, v in rows] + [""]


def _cost_markdown(cost: Mapping[str, Any]) -> list[str]:
    lines = ["## Cost", "", "| Role | Calls | Measured | Unknown cost |", "|---|---|---|---|"]
    for role, label in (("s1", "S1"), ("frontier", "Frontier")):
        c = cost[role]
        unknown = (f"{c['unknown_calls']} (reserved {_usd_text(c['unknown_reserved_usd'])})" if c["unknown_calls"]
                   else "0")
        counted = (f"; the run counted {c['counted_by_run']}" if c["counted_by_run"] is not None
                   and c["counted_by_run"] != c["calls"] else "")
        lines.append(f"| {label} | {c['calls']}{counted} | {c['measured_calls']} calls, "
                     f"{_usd_text(c['measured_usd'])} | {unknown} |")
    total = cost["total"]
    lines += [f"| Replays | {cost['replays']['model_calls']} model calls | | |",
              f"| Total | {total['calls']} | {_usd_text(total['measured_usd'])} | {total['unknown_calls']} "
              f"(upper bound {_usd_text(total['upper_bound_usd'])}) |", "",
              f"Not included: {cost['not_included']}.", ""]
    return lines


# --- command line ------------------------------------------------------------------------------


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="playtest-report", description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, required=True, help="the session's run folder")
    parser.add_argument("--trace", type=Path, default=None, help="default: <run>/trace.jsonl")
    parser.add_argument("--reproduce", type=Path, action="append", default=[], help="a reproduce output folder")
    parser.add_argument("--finding", action="append", default=None, help="report only this finding_id")
    parser.add_argument("--output", type=Path, required=True, help="a new folder for report.json and report.md")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    if args.output.exists():
        print("playtest-report: the output folder must be new", file=sys.stderr)
        return 1
    try:
        report = build_report(args.run, trace_path=args.trace, reproduce_dirs=args.reproduce,
                              finding_ids=args.finding)
    except (OSError, ValueError, KeyError) as exc:
        print(f"playtest-report: {exc}", file=sys.stderr)
        return 1
    args.output.mkdir(parents=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False, default=str) + "\n",
                                             encoding="utf-8")
    (args.output / "report.md").write_text(render_markdown(report), encoding="utf-8")
    states = Counter(f["state"] for f in report["findings"])
    print(f"report: {len(report['findings'])} finding(s) {dict(states)}, measured cost "
          f"{_usd_text(report['cost']['total']['measured_usd'])}, {report['cost']['total']['unknown_calls']} unknown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
