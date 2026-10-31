"""System Zero request-format probe (31 October 2026). Predeclared in docs/probes/2026-10-31-system-zero-format.md.

G2 sent System Zero the System One request every S1 arm uses: the objective inside `state`, generic question
instructions. System Zero's docs say to keep data in `state` and the task in each question's `instructions`.
This probe asks whether the format, not the model, caused G2's 7/40 and 19/45. Each format changes one thing
from that baseline; the frames, labels, model (`system-zero-2.0.0`) and choices stay fixed:

- `task-in-instructions` (F1): the question's instructions begin with the objective, verbatim. State unchanged.
- `task-and-rule` (F2, an upper bound): F1 plus the bench's checking rule spelled out. It hands the model the
  rule the labels encode, so passing shows the model can follow an explicit rule, not that it infers one.
- `text-state` (F4): the same payload rendered as indented text rather than a JSON object. Instructions unchanged.

Stages: A screens every format on the shuffled corpus (45 frames plus the re-test's 5 repeated frames) in one
seeded order. B runs the first corpus's 40 frames for each format with at least 44/45 in A. C (diagnostic)
asks 8 boolean questions per shuffled check frame ("has cN been checked in this pass?") to separate reading
the facts from choosing. A row is reserved before each POST; nothing retries; an in-flight row stops the
run; the approved envelope is checked by per-call bounds, and the operator reads the balance between stages.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from playtest.bench import replay, system_zero
from playtest.bench.record import digest, recorded_payload
from playtest.core.context import canonical

FORMATS = ("task-in-instructions", "task-and-rule", "text-state")
REPEATED = ("pass-02-s8", "pass-03-s8", "pass-04-s3", "pass-05-s2", "pass-05-s8")  # the re-test's seed-2410 draw
SEED = 3110
PASS_BAR = 44  # of 45 in stage A, to go on to stage B
RULE_NOTE = (" Compare the offered action choices with the facts. An action has been checked in this pass when a fact "
             "reports its completed hold with verified release. Choose an offered action that has not been checked "
             "yet; choose finish only when every offered action has been checked.")
BOUND = {"choice": Decimal("0.0007"), "diagnostic": Decimal("0.0018")}  # per-call reservation, USD


def _scalar(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)


def render(value: Any, indent: int = 0) -> list[str]:
    """Every key and value of a payload as indented text lines, in the payload's own order."""
    pad = "  " * indent
    lines: list[str] = []
    items = value.items() if isinstance(value, dict) else ((None, item) for item in value)
    for key, item in items:
        head = f"{pad}{key}:" if key is not None else f"{pad}-"
        if isinstance(item, (dict, list)) and item:
            lines.append(head)
            lines.extend(render(item, indent + 1))
        else:
            lines.append(f"{head} {_scalar(item)}")
    return lines


def shaped(fmt: str, payload: dict[str, Any]) -> tuple[str, Any]:
    instructions = payload["instructions"]
    if fmt == "task-in-instructions":
        return f"Task: {payload['objective']} {instructions}", payload
    if fmt == "task-and-rule":
        return f"Task: {payload['objective']}{RULE_NOTE} {instructions}", payload
    if fmt == "text-state":
        return instructions, "\n".join(render(payload))
    raise ValueError(f"unknown format {fmt!r}")


def choice_body(fmt: str, payload: dict[str, Any]) -> dict[str, Any]:
    instructions, state = shaped(fmt, payload)
    items = [(c["id"], c["label"], c["kind"]) for c in payload["choices"]]
    questions = system_zero.layout("next_action", instructions, items, media=False)
    return {"model": system_zero.MODEL, "questions": questions, "state": state}


def diagnostic_body(payload: dict[str, Any]) -> dict[str, Any]:
    questions = {}
    for choice in payload["choices"]:
        if choice["kind"] != "action":
            continue
        cid = choice["id"]
        questions[f"checked_{cid}"] = {
            "type": "boolean",
            "instructions": f"Has the action choice {cid} been checked in this pass? A check is a fact that reports a "
                            f"completed hold of {cid} with verified release.",
            "criteria": {"true": f"a fact reports a completed hold of {cid} with verified release",
                         "false": f"no fact reports a completed hold of {cid}"}}
    return {"model": system_zero.MODEL, "questions": questions, "state": payload}


def load(corpus_path: Path, labels_path: Path) -> tuple[list[dict[str, Any]], dict[str, set[str]], str]:
    raw = corpus_path.read_bytes()
    corpus, labels = json.loads(raw), json.loads(labels_path.read_text(encoding="utf-8"))
    if labels.get("corpus_sha256") != digest(raw):
        raise ValueError(f"{labels_path.name} is bound to a different corpus")
    frames = corpus["frames"]
    for frame in frames:
        if recorded_payload(frame) is None:
            raise ValueError(f"{frame['id']} no longer rebuilds byte for byte")
    return frames, {row["frame_id"]: set(row["acceptable"]) for row in labels["labels"]}, digest(raw)


def _rows(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((directory / "calls").glob("*.json"))]


def _write(path: Path, data: dict[str, Any], *, exclusive: bool = False) -> None:
    with path.open("x" if exclusive else "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=1, allow_nan=False))


def schedule(stage: str, shuffled: list[dict[str, Any]], first: list[dict[str, Any]],
             shuffled_labels: dict[str, set[str]], directory: Path) -> list[tuple[str, str, dict[str, Any], int]]:
    """(format, corpus, frame, repeat) entries for a stage, in its seeded order."""
    if stage == "A":
        by_id = {f["id"]: f for f in shuffled}
        entries = [(fmt, "shuffled", frame, 0) for fmt in FORMATS for frame in shuffled]
        entries += [(fmt, "shuffled", by_id[fid], 1) for fmt in FORMATS for fid in REPEATED]
    elif stage == "B":
        passed = [fmt for fmt, score in stage_scores(_rows(directory), "A", shuffled_labels).items()
                  if score["hits"] >= PASS_BAR]
        entries = [(fmt, "first", frame, 0) for fmt in passed for frame in first]
    elif stage == "C":
        entries = [("diagnostic", "shuffled", frame, 0) for frame in shuffled
                   if shuffled_labels[frame["id"]] != {"finish"}]
    else:
        raise ValueError("stage must be A, B or C")
    random.Random(f"{SEED}-{stage}").shuffle(entries)
    return entries


def stage_scores(rows: list[dict[str, Any]], stage: str, labels: dict[str, set[str]]) -> dict[str, dict[str, Any]]:
    scores: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["stage"] != stage or row["repeat"] != 0 or row.get("status") != "finished":
            continue
        entry = scores.setdefault(row["format"], {"hits": 0, "scored": 0})
        entry["scored"] += 1
        entry["hits"] += int(row.get("choice") in labels[row["frame_id"]])
    return scores


def run(args: argparse.Namespace) -> int:
    shuffled, shuffled_labels, shuffled_sha = load(args.shuffled_corpus, args.shuffled_labels)
    first, _, first_sha = load(args.first_corpus, args.first_labels)
    approval, approval_sha = replay._load(args.approval)
    replay.validate_approval(approval, [system_zero.ARM])
    args.directory.mkdir(parents=True, exist_ok=True)
    (args.directory / "calls").mkdir(exist_ok=True)
    plan = {"version": 1, "probe_source_sha256": digest(Path(__file__).read_bytes()),
            "system_zero_source_sha256": digest(Path(system_zero.__file__).read_bytes()),
            "model": system_zero.MODEL, "seed": SEED, "formats": list(FORMATS), "pass_bar": PASS_BAR,
            "shuffled_corpus_sha256": shuffled_sha, "first_corpus_sha256": first_sha, "approval_sha256": approval_sha,
            "predeclaration": "docs/probes/2026-10-31-system-zero-format.md"}
    plan_path = args.directory / "plan.json"
    if plan_path.exists():
        if json.loads(plan_path.read_text(encoding="utf-8")) != plan:
            raise ValueError("the saved plan differs; do not mix frozen inputs")
    else:
        _write(plan_path, plan, exclusive=True)
    rows = _rows(args.directory)
    if any(row.get("status") == "in_flight" for row in rows):
        raise ValueError("a prior call has an unknown outcome; reconcile it before resuming")
    done = {row["slot"] for row in rows}
    blocked = {row["format"] for row in rows if row.get("http_status") != 200 or row.get("failed")}
    limit = Decimal(str(approval["max_cost_usd"]))
    made = 0
    for fmt, corpus, frame, repeat in schedule(args.stage, shuffled, first, shuffled_labels, args.directory):
        if fmt in blocked:
            continue
        payload = recorded_payload(frame)
        body = diagnostic_body(payload) if fmt == "diagnostic" else choice_body(fmt, payload)
        request_sha = digest(canonical(body))
        slot = digest(canonical({"stage": args.stage, "format": fmt, "frame": frame["id"], "repeat": repeat,
                                 "request_sha256": request_sha}))
        if slot in done:
            continue
        bound = BOUND["diagnostic" if fmt == "diagnostic" else "choice"]
        reserved = sum((Decimal(row["reserved_usd"]) for row in rows), Decimal(0))
        if len(rows) >= approval["max_calls"] or reserved + bound > limit:
            print(f"envelope reached after {made} new calls", file=sys.stderr)
            break
        row = {"slot": slot, "stage": args.stage, "format": fmt, "corpus": corpus, "frame_id": frame["id"],
               "repeat": repeat, "payload_sha256": frame["payload_sha256"], "request_sha256": request_sha,
               "reserved_usd": str(bound), "status": "in_flight"}
        path = args.directory / "calls" / f"{slot}.json"
        _write(path, row, exclusive=True)
        try:
            status, data, latency_ms, problem = system_zero.send(system_zero.ARM, body)
        except Exception as exc:  # noqa: BLE001 - the outcome is unknown; keep the row and stop
            row.update(status="finished", http_status=None, problem=f"adapter error: {type(exc).__name__}", failed=True)
            _write(path, row)
            rows.append(row)
            print(f"stopped: {row['problem']}", file=sys.stderr)
            return 1
        answers = (data or {}).get("answers") if status == 200 else None
        row.update(status="finished", http_status=status, latency_ms=latency_ms, problem=problem,
                   resolved_model=(data or {}).get("model"), usage=(data or {}).get("usage"), answers=answers)
        if fmt == "diagnostic":
            row["failed"] = status != 200
        else:
            offered = {c["id"] for c in payload["choices"]}
            choice, distribution, answer_problem = system_zero.decode("next_action", body["questions"], data if status == 200
                                                                      else None, offered)
            # A refusal or invalid answer on HTTP 200 is a scored miss, not a stop: nothing unknown was billed.
            row.update(choice=choice, distribution=distribution, answer_problem=answer_problem, failed=status != 200)
        _write(path, row)
        rows.append(row)
        made += 1
        if row["failed"]:
            blocked.add(fmt)
    print(f"new calls: {made}")
    return 0


def _quantile(values: list[float], q: float) -> float | None:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)] if ordered else None


def _baseline(directory: Path | None) -> dict[str, str | None]:
    if directory is None:
        return {}
    out = {}
    for path in (directory / "calls").glob("*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("phase") in ("sweep", None) and row.get("repeat", 0) == 0:
            out[row["frame_id"]] = row.get("choice")
    return out


def score(args: argparse.Namespace) -> dict[str, Any]:
    _, shuffled_labels, _ = load(args.shuffled_corpus, args.shuffled_labels)
    _, first_labels, _ = load(args.first_corpus, args.first_labels)
    labels = {"shuffled": shuffled_labels, "first": first_labels}
    baselines = {"shuffled": _baseline(args.baseline_shuffled), "first": _baseline(args.baseline_first)}
    rows = [row for row in _rows(args.directory) if row.get("status") == "finished"]
    report: dict[str, Any] = {"choice": {}, "diagnostic": None}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row["format"] != "diagnostic":
            groups.setdefault((row["format"], row["corpus"]), []).append(row)
    for (fmt, corpus), group in sorted(groups.items()):
        main = [r for r in group if r["repeat"] == 0]
        truth = labels[corpus]
        terminal = [r for r in main if truth[r["frame_id"]] == {"finish"}]
        check = [r for r in main if truth[r["frame_id"]] != {"finish"}]
        misses = Counter()
        for r in check:
            if r.get("choice") not in truth[r["frame_id"]]:
                misses["premature-finish" if r.get("choice") == "finish" else
                       "wait" if r.get("choice") == "wait" else "re-check" if r.get("choice") else "no answer"] += 1
        masses = [sum(p for k, p in r["distribution"].items() if k in truth[r["frame_id"]])
                  for r in main if r.get("distribution")]
        paired = Counter()
        for r in main:
            base = baselines[corpus].get(r["frame_id"], "absent")
            if base == "absent":
                continue
            now, then = r.get("choice") in truth[r["frame_id"]], base in truth[r["frame_id"]]
            paired["win" if now and not then else "loss" if then and not now else "tie"] += 1
        repeats = []
        for r in (r for r in group if r["repeat"] == 1):
            twin = next((m for m in main if m["frame_id"] == r["frame_id"]), None)
            if twin and twin.get("distribution") and r.get("distribution"):
                repeats.append({"frame_id": r["frame_id"], "same_choice": twin["choice"] == r["choice"],
                                "max_probability_difference": max(abs(twin["distribution"][k] - r["distribution"][k])
                                                                  for k in r["distribution"])})
        latencies = [r["latency_ms"] for r in group if r.get("latency_ms") is not None]
        report["choice"][f"{fmt} / {corpus}"] = {
            "top1_acceptable": {"all": f"{sum(r.get('choice') in truth[r['frame_id']] for r in main)}/{len(main)}",
                                "check": f"{sum(r.get('choice') in truth[r['frame_id']] for r in check)}/{len(check)}",
                                "terminal": f"{sum(r.get('choice') == 'finish' for r in terminal)}/{len(terminal)}"},
            "misses": dict(misses), "picks": dict(Counter(r.get("choice") for r in main).most_common()),
            "acceptable_mass_mean": round(sum(masses) / len(masses), 3) if masses else None,
            "acceptable_mass_min": round(min(masses), 3) if masses else None,
            "paired_vs_system_one_format": dict(paired), "repeats": repeats,
            "latency_p50_ms": _quantile(latencies, 0.5), "latency_p95_ms": _quantile(latencies, 0.95),
            "input_tokens_mean": round(sum((r.get("usage") or {}).get("input_tokens", 0) for r in group) / len(group)),
        }
    diag = [row for row in rows if row["format"] == "diagnostic" and row.get("answers")]
    if diag:
        judged = correct = exact = refused = 0
        p_checked, p_unchecked = [], []
        for row in diag:
            truth = shuffled_labels[row["frame_id"]]
            frame_ok = True
            for name, answer in row["answers"].items():
                cid = name.removeprefix("checked_")
                checked = cid not in truth
                if not isinstance(answer, dict) or "probability" not in answer:
                    refused += 1
                    frame_ok = False
                    continue
                judged += 1
                decision = answer.get("decision")
                correct += int(decision is checked)
                frame_ok &= decision is checked
                (p_checked if checked else p_unchecked).append(answer["probability"])
            exact += int(frame_ok)
        report["diagnostic"] = {
            "frames": len(diag), "judgments": judged, "refused": refused,
            "accuracy": f"{correct}/{judged}", "frames_exactly_right": f"{exact}/{len(diag)}",
            "mean_p_true_when_checked": round(sum(p_checked) / len(p_checked), 3) if p_checked else None,
            "mean_p_true_when_unchecked": round(sum(p_unchecked) / len(p_unchecked), 3) if p_unchecked else None,
            "latency_p50_ms": _quantile([r["latency_ms"] for r in diag], 0.5)}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "score"))
    parser.add_argument("--stage", choices=("A", "B", "C"))
    for name in ("shuffled-corpus", "shuffled-labels", "first-corpus", "first-labels", "directory"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--baseline-shuffled", type=Path)
    parser.add_argument("--baseline-first", type=Path)
    args = parser.parse_args(argv)
    if args.command == "run":
        if not args.stage or not args.approval:
            parser.error("run needs --stage and --approval")
        return run(args)
    print(json.dumps(score(args), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
