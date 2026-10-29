"""Paired preflight comparison with game source withheld (#121).

Arms differ only in the code evidence the packet carries:

    S  source excerpts and IL write facts (the production packet)
    I  IL write facts, source excerpts withheld (what a thin collector would have)
    N  neither (the floor)

Every arm sends one system prompt that does not promise source; production's prompt says the
fields are "followed by the game's source". The swap happens on the outgoing request after
production's own request checks pass, so no production module changes and the preflight cache is
unaffected. Nothing here repairs an answer, retries for a pass or switches models: the stage
runner's validation retries are the only retries, and a transport error ends that chain.

    uv run python scripts/probes/sourceless_preflight.py dry-run --inputs DIR --out DIR
    uv run python scripts/probes/sourceless_preflight.py run --inputs DIR --out DIR --approval FILE --env-file FILE
    uv run python scripts/probes/sourceless_preflight.py score --inputs DIR --out DIR
    uv run python scripts/probes/sourceless_preflight.py primary --inputs DIR --out DIR --judgments FILE

`run` spends money. It refuses to start without an approval file naming the call and cost limits
and a verified balance or the user's explicit override, and it stops before any call that could
exceed them. Raw packets, ledgers and sheets stay under the output directory (keep it in `runs/`).
"""

from __future__ import annotations

import argparse
import copy
import csv
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import random
import statistics
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from itertools import combinations
from pathlib import Path
from typing import Any

from playtest.contracts.bridge import InspectResult
from playtest.contracts.calibration import CalibrationRecord
from playtest.frontier import provider as meta
from playtest.preflight import packet as packet_mod
from playtest.preflight import provider, stages
from playtest.roles import preflight, strict

ARMS = ("S", "I", "N")
SOURCE_PHRASE = "they are listed below with ids, followed by the game's source."
NEUTRAL_PHRASE = "they are listed below with ids, followed by whatever code evidence was supplied."
NOTES = {
    "S": "Runtime field values and enum names are omitted; source excerpts and IL write facts are included.",
    "I": "Runtime field values and enum names are omitted; source excerpts are withheld; IL write facts are included.",
    "N": "Runtime field values and enum names are omitted; source excerpts and IL write facts are withheld.",
}
SOURCE_KEYS = ("source_excerpts", "source_selection")
IL_KEYS = ("il_write_sets", "il_unlisted_writes", "il_issues", "il_complete", "il_scope")
LIVE = frozenset({"idle", "input", "both"})
NOT_LIVE = frozenset({"no_change_observed"})
VERDICTS = ("correct", "plausible", "wrong", "unknown")
REVIEW_CAP = 150
MIN_JUDGED_SLOTS = 5
MARGIN = 0.25
LEVEL = 0.90
RESAMPLES = 10_000


class EnvelopeExhausted(RuntimeError):
    """The next call could exceed an approved limit; nothing was sent."""


# --- packets and prompt -------------------------------------------------------------------------


def neutral_system() -> str:
    if preflight.SYSTEM.count(SOURCE_PHRASE) != 1:
        raise ValueError("the production system prompt changed; update SOURCE_PHRASE before running")
    return preflight.SYSTEM.replace(SOURCE_PHRASE, NEUTRAL_PHRASE)


def variant(built: packet_mod.Packet, arm: str) -> packet_mod.Packet:
    """Return the arm's packet: the same discovery, schema and image, different code evidence."""
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}")
    payload = json.loads(built.text)
    discovery, caps = payload.get("discovery"), payload.get("discovery_caps")
    if not isinstance(discovery, dict) or not isinstance(caps, dict):
        raise ValueError("the packet has no discovery projection")
    missing = [key for key in ("source_excerpts", "il_write_sets") if key not in discovery]
    if missing:
        raise ValueError(f"the production packet lacks {', '.join(missing)}; arms would not differ as declared")
    if arm in ("I", "N"):
        for key in SOURCE_KEYS:
            discovery.pop(key, None)
        for key in list(caps):
            if key in ("source_files", "source_characters") or key.startswith("source_file:"):
                del caps[key]
    if arm == "N":
        for key in IL_KEYS:
            discovery.pop(key, None)
        for key in list(caps):
            if key.startswith("il_"):
                del caps[key]
    payload["discovery_projection_note"] = NOTES[arm]
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return dataclasses.replace(built, text=text)


def load_inputs(args: argparse.Namespace) -> tuple[InspectResult, CalibrationRecord, packet_mod.Packet]:
    inventory = InspectResult.model_validate_json(Path(args.inventory).read_text(encoding="utf-8"))
    discovery = InspectResult.model_validate_json(Path(args.discovery).read_text(encoding="utf-8"))
    calibration = CalibrationRecord.model_validate_json(Path(args.calibration).read_text(encoding="utf-8"))
    image = packet_mod.Screenshot(Path(args.screenshot).read_bytes(), args.screenshot_mime, args.screenshot_provenance)
    return discovery, calibration, packet_mod.build(inventory, discovery, calibration, image=image)


def _sha(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _git(path: Path, *args: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                              text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def frozen_header(args: argparse.Namespace, packets: dict[str, packet_mod.Packet], model: str) -> dict[str, Any]:
    production = Path(packet_mod.__file__).resolve().parents[3]
    schemas = {arm: _sha(strict.canonical(preflight.schema(p.discovery))) for arm, p in packets.items()}
    if len(set(schemas.values())) != 1:
        raise ValueError("the arms' schemas differ; stop")
    return {
        "kind": "header", "issue": 121, "model": model, "reasoning_effort": "minimal", "max_retries": stages.MAX_RETRIES,
        "seed": args.seed, "slots": args.slots, "pilot_slots": args.pilot,
        "system_sha256": _sha(neutral_system()), "production_system_sha256": _sha(preflight.SYSTEM),
        "schema_sha256": next(iter(schemas.values())),
        "packet_sha256": {arm: _sha(p.text) for arm, p in packets.items()},
        "packet_characters": {arm: len(p.text) for arm, p in packets.items()},
        "image_sha256": packets["S"].image.descriptor()["sha256"] if packets["S"].image is not None else None,
        "inputs_sha256": {name: _sha(Path(getattr(args, name)).read_bytes())
                          for name in ("inventory", "discovery", "calibration", "screenshot")},
        "production_commit": _git(production, "rev-parse", "HEAD"),
        "production_dirty": bool(_git(production, "status", "--porcelain", "--", "src")),
        "script_sha256": _sha(Path(__file__).read_bytes()),
    }


def schedule(seed: int, slots: int, pilot: int) -> list[tuple[str, int, str]]:
    """Each slot runs every arm once in a seeded random order; pilot slots are excluded from analysis."""
    rows: list[tuple[str, int, str]] = []
    for phase, numbers in (("pilot", range(pilot)), ("main", range(1, slots + 1))):
        for slot in numbers:
            order = list(ARMS)
            random.Random(f"{seed}:{phase}:{slot}").shuffle(order)
            rows += [(phase, slot, arm) for arm in order]
    return rows


# --- approval, key and ledger --------------------------------------------------------------------


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return result


def _when(value: Any, name: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} is required")
    moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"{name} needs a timezone")
    return moment


def check_approval(approval: dict[str, Any]) -> dict[str, Any]:
    """Mirror the bench approval format: overall and per-arm limits, price validity and balance."""
    for key in ("approved_by", "approved_at_utc", "approval_scope", "approval_source"):
        if not isinstance(approval.get(key), str) or not approval[key].strip():
            raise ValueError(f"the approval needs {key}")
    model = approval.get("model")
    if model not in provider.MODEL_PRICES:
        raise ValueError("the approval must name a supported preflight model")
    if not isinstance(approval.get("max_calls"), int) or isinstance(approval["max_calls"], bool) or approval["max_calls"] < 1:
        raise ValueError("the approval needs a positive overall call limit")
    overall = _decimal(approval.get("max_cost_usd"), "overall cost limit")
    override = approval.get("unverified_balance_override")
    overridden = (isinstance(override, dict) and override.get("approved_by_user") is True and
                  isinstance(override.get("reason"), str) and bool(override["reason"].strip()))
    limits = approval.get("arms")
    if not isinstance(limits, dict) or set(limits) != set(ARMS):
        raise ValueError("the approval needs limits for exactly the arms S, I and N")
    now = dt.datetime.now(dt.timezone.utc)
    for arm in ARMS:
        limit = limits[arm]
        if not isinstance(limit.get("max_calls"), int) or isinstance(limit["max_calls"], bool) or limit["max_calls"] < 1:
            raise ValueError(f"{arm} needs a positive call limit")
        bound = _decimal(limit.get("per_call_max_usd"), f"{arm} per-call bound")
        maximum = _decimal(limit.get("max_cost_usd"), f"{arm} cost limit")
        if bound > maximum or maximum > overall:
            raise ValueError(f"{arm} limits are inconsistent with the overall envelope")
        if _when(limit.get("price_valid_until_utc"), f"{arm} price validity") <= now:
            raise ValueError(f"{arm} pricing validity has expired; verify the rate again")
    balance = approval.get("verified_balance_usd")
    if balance is None:
        if not overridden:
            raise ValueError("the approval needs a verified balance or the user's explicit override")
    else:
        if _decimal(balance, "verified balance") < overall:
            raise ValueError("the overall envelope exceeds the verified balance")
        _when(approval.get("balance_checked_at_utc"), "balance check time")
    return approval


def load_key(env_file: Path, name: str = "META_API_KEY") -> None:
    """Put one key into this process's environment. The value is never printed or returned."""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text.startswith("export "):
            text = text[len("export "):].strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        if key.strip() == name:
            value = value.strip().strip('"').strip("'")
            if not value:
                raise ValueError(f"{name} is empty in the env file")
            os.environ[name] = value
            return
    raise ValueError(f"{name} is not in the env file")


class Ledger:
    """Append-only JSONL. Each row is flushed and synced before the next request."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[dict[str, Any]] = []
        if path.exists():
            self.rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def write(self, row: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.rows.append(row)


@dataclasses.dataclass
class Envelope:
    max_calls: int
    max_cost: Decimal
    arm_calls: dict[str, int]
    arm_cost: dict[str, Decimal]
    bound: dict[str, Decimal]
    calls: int = 0
    spent: Decimal = Decimal(0)
    used_calls: dict[str, int] = dataclasses.field(default_factory=lambda: {arm: 0 for arm in ARMS})
    used_cost: dict[str, Decimal] = dataclasses.field(default_factory=lambda: {arm: Decimal(0) for arm in ARMS})

    def reserve(self, arm: str) -> None:
        bound = self.bound[arm]
        if self.calls + 1 > self.max_calls or self.used_calls[arm] + 1 > self.arm_calls[arm]:
            raise EnvelopeExhausted("the approved call limit would be exceeded")
        if self.spent + bound > self.max_cost or self.used_cost[arm] + bound > self.arm_cost[arm]:
            raise EnvelopeExhausted("the approved cost limit could be exceeded by the next call")
        self.calls += 1
        self.used_calls[arm] += 1

    def charge(self, arm: str, metadata: dict[str, Any]) -> Decimal:
        """Charge the estimated cost, or the per-call bound when usage is missing."""
        estimate = metadata.get("estimated_cost_usd") if isinstance(metadata, dict) else None
        cost = Decimal(str(estimate)) if isinstance(estimate, (int, float)) and math.isfinite(estimate) else self.bound[arm]
        self.spent += cost
        self.used_cost[arm] += cost
        return cost


# --- run -----------------------------------------------------------------------------------------


def run_chain(phase: str, slot: int, arm: str, built: packet_mod.Packet, system: str, model: str,
              envelope: Envelope, ledger: Ledger, stop: dict[str, str]) -> dict[str, Any]:
    def send(request: dict[str, Any]):
        body = copy.deepcopy(request)
        if body["messages"][0] != {"role": "system", "content": preflight.SYSTEM}:
            raise ValueError("the production system message was not where it was expected")
        body["messages"][0] = {"role": "system", "content": system}
        # Routing affinity for the provider's prefix cache; the model's input is unchanged.
        body["prompt_cache_key"] = f"upa-121-{arm}"
        envelope.reserve(arm)
        ledger.write({"kind": "reservation", "phase": phase, "slot": slot, "arm": arm, "request": envelope.calls,
                      "request_sha256": _sha(strict.canonical(body)), "system_sha256": _sha(system),
                      "packet_sha256": _sha(built.text), "model": model, "prompt_cache_key": body["prompt_cache_key"]})
        return meta.send_meta(body)

    def record(attempt: stages.Attempt) -> None:
        cost = envelope.charge(arm, attempt.metadata)
        ledger.write({"kind": "attempt", "phase": phase, "slot": slot, "arm": arm, "request": envelope.calls,
                      "charged_usd": str(cost), "spent_usd": str(envelope.spent), **dataclasses.asdict(attempt)})
        status = attempt.metadata.get("status") if isinstance(attempt.metadata, dict) else None
        if status in (401, 402, 403):
            stop["reason"] = f"billing or auth failure (HTTP {status})"
        elif attempt.failure == "the provider resolved a different model":
            stop["reason"] = "the provider resolved a different model"

    result = stages.run(built, provider.Muse(built, send=send, model=model), record)
    parts = {"readers": result.readers, "actions": result.actions, "progress": result.progress}
    row = {"kind": "chain", "phase": phase, "slot": slot, "arm": arm,
           "status": "complete" if all(p.answer is not None for p in parts.values()) else "incomplete",
           "answers": {name: p.answer.model_dump(mode="json") if p.answer is not None else None
                       for name, p in parts.items()},
           "attempts": {name: len(p.attempts) for name, p in parts.items()},
           "skipped": {name: p.skipped_reason for name, p in parts.items()}}
    ledger.write(row)
    return row


def command_run(args: argparse.Namespace) -> int:
    approval = check_approval(json.loads(Path(args.approval).read_text(encoding="utf-8")))
    model = approval["model"]
    _, _, built = load_inputs(args)
    packets = {arm: variant(built, arm) for arm in ARMS}
    header = frozen_header(args, packets, model)
    header["approval_sha256"] = _sha(Path(args.approval).read_bytes())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(out / "ledger.jsonl")
    if ledger.rows:
        frozen = {k: v for k, v in ledger.rows[0].items() if k != "script_sha256"}
        if frozen != {k: v for k, v in header.items() if k != "script_sha256"}:
            raise ValueError("this output directory holds a different frozen run; use a new directory")
        scripts = [ledger.rows[0]["script_sha256"]] + [r["script_sha256"] for r in ledger.rows if r["kind"] == "resume"]
        if scripts[-1] != header["script_sha256"]:
            if not args.accept_script_change:
                raise ValueError("the runner script changed since this run started; pass --accept-script-change "
                                 "with the reason, which is recorded in the ledger")
            ledger.write({"kind": "resume", "previous_script_sha256": scripts[-1],
                          "script_sha256": header["script_sha256"], "reason": args.accept_script_change})
    else:
        ledger.write(header)
    limits = approval["arms"]
    envelope = Envelope(approval["max_calls"], Decimal(str(approval["max_cost_usd"])),
                        {a: limits[a]["max_calls"] for a in ARMS},
                        {a: Decimal(str(limits[a]["max_cost_usd"])) for a in ARMS},
                        {a: Decimal(str(limits[a]["per_call_max_usd"])) for a in ARMS})
    done: set[tuple[str, int, str]] = set()
    for row in ledger.rows:
        if row["kind"] == "reservation":
            envelope.calls += 1
            envelope.used_calls[row["arm"]] += 1
        elif row["kind"] == "attempt":
            envelope.spent += Decimal(row["charged_usd"])
            envelope.used_cost[row["arm"]] += Decimal(row["charged_usd"])
        elif row["kind"] == "chain":
            done.add((row["phase"], row["slot"], row["arm"]))
        elif row["kind"] == "stop" and not args.resume_after_stop:
            raise ValueError(f"the run stopped earlier ({row['reason']}); pass --resume-after-stop only with approval")
    started = {(r["phase"], r["slot"], r["arm"]) for r in ledger.rows if r["kind"] == "reservation"}
    for key in sorted(started - done):
        ledger.write({"kind": "chain", "phase": key[0], "slot": key[1], "arm": key[2], "status": "interrupted",
                      "answers": None, "attempts": None, "skipped": None})
        done.add(key)
    load_key(Path(args.env_file))
    system, stop = neutral_system(), {}
    for phase, slot, arm in schedule(args.seed, args.slots, args.pilot):
        if (phase, slot, arm) in done or (args.phase and phase != args.phase):
            continue
        try:
            row = run_chain(phase, slot, arm, packets[arm], system, model, envelope, ledger, stop)
        except EnvelopeExhausted as exc:
            ledger.write({"kind": "stop", "reason": str(exc), "at": [phase, slot, arm]})
            print(f"stopped before a call: {exc}")
            break
        print(f"{phase} slot {slot} arm {arm}: {row['status']} attempts={row['attempts']} "
              f"calls={envelope.calls} spent ~${envelope.spent:.4f}", flush=True)
        if stop:
            ledger.write({"kind": "stop", "reason": stop["reason"], "at": [phase, slot, arm]})
            print(f"stopped: {stop['reason']}")
            break
    print(f"ledger: {ledger.path}; calls {envelope.calls}; estimated spend ${envelope.spent:.4f}")
    return 0


def command_dry_run(args: argparse.Namespace) -> int:
    _, _, built = load_inputs(args)
    packets = {arm: variant(built, arm) for arm in ARMS}
    header = frozen_header(args, packets, args.model)
    out = Path(args.out)
    (out / "packets").mkdir(parents=True, exist_ok=False)
    for arm, packet in packets.items():
        (out / "packets" / f"{arm}.json").write_text(packet.text, encoding="utf-8")
    (out / "header.json").write_text(json.dumps(header, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: header[k] for k in ("system_sha256", "schema_sha256", "packet_sha256",
                                             "packet_characters", "production_commit", "production_dirty")}, indent=2))
    print(f"zero calls; packets and header written to {out}")
    return 0


# --- scoring -------------------------------------------------------------------------------------


def classify_freshness(measurement: Any) -> str:
    value = measurement.get("classification") if isinstance(measurement, dict) else None
    return "live" if value in LIVE else "not_live" if value in NOT_LIVE else "unknown"


def effective_controls(calibration: CalibrationRecord) -> tuple[set[str], set[str]]:
    """Controls with an effect in all repeats of some hold, and controls with no such evidence."""
    effective, other = set(), set()
    for summary in calibration.action_summaries:
        (effective if any(h.effective_in_all_repeats is True for h in summary.holds) else other).add(summary.control)
    return effective, other


def _writer_parts(method: str) -> tuple[str, str] | None:
    parts = method.split(":")
    if len(parts) < 3:
        return None
    return parts[1], parts[2].split("(")[0]


def citation_status(source: str, writers: list[str] | None) -> str:
    """consistent, not_recorded_writer, or unknown when IL facts do not cover the field."""
    if writers is None:
        return "unknown"
    text = source.strip().split("(")[0].replace("::", ".")
    if "." not in text:
        return "not_recorded_writer"
    cited_type, cited_method = text.rsplit(".", 1)
    cited_short = cited_type.replace("/", ".").replace("+", ".").split(".")[-1]
    for method in writers:
        parts = _writer_parts(method)
        if parts is None:
            continue
        full, name = parts
        short = full.replace("/", ".").replace("+", ".").split(".")[-1]
        if name == cited_method and (short == cited_short or full.endswith(cited_type)):
            return "consistent"
    return "not_recorded_writer"


def chain_metrics(row: dict[str, Any], bindings: dict[str, str], freshness: dict[str, Any],
                  effective: set[str], offered: set[str], writers: dict[str, list[str]],
                  attempts: list[dict[str, Any]], declarations: dict[str, str]) -> dict[str, Any]:
    """`writers` is keyed by declaration id; readers name field instances, mapped through `declarations`."""
    answers = row.get("answers") or {}
    readers = ((answers.get("readers") or {}).get("stage_4") or {})
    actions = ((answers.get("actions") or {}).get("stage_5") or {})
    progress = ((answers.get("progress") or {}).get("stage_6") or {})
    alias = {r["alias"]: bindings.get(r["field_id"]) for r in readers.get("readers", [])}
    reader_fields = {f for f in alias.values() if f is not None}
    live = [classify_freshness(freshness.get(f)) for f in reader_fields]
    used = {i["control_id"] for a in actions.get("actions", []) for i in a["inputs"]}
    used |= {i["control_id"] for s in actions.get("skills", []) for step in s["steps"] for i in step["inputs"]}
    covered = effective & offered
    cites = [citation_status(inv["source"], writers.get(declarations.get(alias.get(inv["field"]))))
             if inv.get("field") else "unknown" for inv in progress.get("invariants", [])]
    usage = [a.get("metadata", {}).get("usage") or {} for a in attempts]
    return {
        "valid": row.get("status") == "complete",
        "attempts": row.get("attempts"),
        "reader_fields": sorted(reader_fields),
        "action_controls": sorted(used),
        "readers_live": live.count("live"), "readers_not_live": live.count("not_live"),
        "readers_unknown": live.count("unknown"),
        "live_share": live.count("live") / (live.count("live") + live.count("not_live"))
        if live.count("live") + live.count("not_live") else None,
        "effective_controls": len(covered),
        "control_coverage": len(covered & used) / len(covered) if covered else None,
        "invariants": len(progress.get("invariants", [])), "progress_fields": len(progress.get("progress", [])),
        "citations_consistent": cites.count("consistent"), "citations_not_writer": cites.count("not_recorded_writer"),
        "citations_unknown": cites.count("unknown"),
        "citation_share": cites.count("consistent") / (cites.count("consistent") + cites.count("not_recorded_writer"))
        if cites.count("consistent") + cites.count("not_recorded_writer") else None,
        "gaps": {"unobservable": len(readers.get("unobservable", [])),
                 "uncontrollable": len(actions.get("uncontrollable", [])),
                 "unjudgeable": len(progress.get("unjudgeable", []))},
        "prompt_tokens": sum(u.get("prompt_tokens") or 0 for u in usage),
        "cached_tokens": sum((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0 for u in usage),
        "completion_tokens": sum(u.get("completion_tokens") or 0 for u in usage),
        "estimated_cost_usd": sum(float(a.get("charged_usd") or 0) for a in attempts),
        "latency_ms": sum(a.get("metadata", {}).get("latency_ms") or 0 for a in attempts),
        "calls": len(attempts),
    }


def jaccard(a: list[str], b: list[str]) -> float | None:
    union = set(a) | set(b)
    return len(set(a) & set(b)) / len(union) if union else None


def _mean(values: list[float | None]) -> float | None:
    kept = [v for v in values if v is not None]
    return statistics.fmean(kept) if kept else None


def paired_interval(diffs: list[float], seed: str) -> tuple[float, float, float] | None:
    if not diffs:
        return None
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(diffs, k=len(diffs))) for _ in range(RESAMPLES))
    low = means[int((1 - LEVEL) / 2 * RESAMPLES)]
    high = means[int((1 + LEVEL) / 2 * RESAMPLES) - 1]
    return statistics.fmean(diffs), low, high


def decide(interval: tuple[float, float, float] | None, s_mean: float | None) -> str:
    if interval is None or s_mean is None:
        return "unknown"
    margin = -MARGIN * s_mean
    _, low, high = interval
    return "acceptable" if low > margin else "not_acceptable" if high < margin else "inconclusive"


def _main_chains(ledger: Ledger) -> dict[tuple[int, str], dict[str, Any]]:
    return {(r["slot"], r["arm"]): r for r in ledger.rows if r["kind"] == "chain" and r["phase"] == "main"}


def _attempts(ledger: Ledger, slot: int, arm: str) -> list[dict[str, Any]]:
    return [r for r in ledger.rows if r["kind"] == "attempt" and r["phase"] == "main" and r["slot"] == slot and r["arm"] == arm]


def command_score(args: argparse.Namespace) -> int:
    discovery, calibration, built = load_inputs(args)
    out = Path(args.out)
    ledger = Ledger(out / "ledger.jsonl")
    if not ledger.rows or ledger.rows[0].get("packet_sha256") != {arm: _sha(variant(built, arm).text) for arm in ARMS}:
        raise ValueError("these inputs do not reproduce the run's frozen packets")
    chains = _main_chains(ledger)
    freshness = calibration.freshness or {}
    effective, _ = effective_controls(calibration)
    offered = {c["id"] for c in json.loads(built.text)["offered_controls"]}
    writers: dict[str, list[str]] = {}
    for row in discovery.data.get("il_write_sets") or []:
        writers.setdefault(row["field_id"], []).extend(w["method"] for w in row["direct_writers"])
    fields = {f["id"]: f for f in discovery.data.get("fields") or []}
    declarations = {ident: f.get("declaration_id") for ident, f in fields.items()}
    metrics = {f"{slot}:{arm}": chain_metrics(row, built.field_bindings, freshness, effective, offered, writers,
                                              _attempts(ledger, slot, arm), declarations)
               for (slot, arm), row in sorted(chains.items())}
    slots = sorted({slot for slot, _ in chains})
    summary: dict[str, Any] = {"chains": {arm: {"attempted": sum(1 for s, a in chains if a == arm),
                                                "complete": sum(1 for (s, a), r in chains.items()
                                                                if a == arm and r["status"] == "complete")}
                                          for arm in ARMS}}
    secondary = ("live_share", "control_coverage", "citation_share", "invariants", "progress_fields")
    for arm in ARMS:
        rows = [m for key, m in metrics.items() if key.endswith(f":{arm}") and m["valid"]]
        summary[arm] = {name: _mean([m[name] for m in rows]) for name in secondary}
        summary[arm].update({f"gap_{g}": _mean([m["gaps"][g] for m in rows]) for g in ("unobservable", "uncontrollable", "unjudgeable")})
        summary[arm].update({name: _mean([m[name] for m in rows]) for name in
                             ("prompt_tokens", "cached_tokens", "completion_tokens", "estimated_cost_usd", "latency_ms", "calls")})
        within = [jaccard(a["reader_fields"], b["reader_fields"]) for a, b in combinations(rows, 2)]
        summary[arm]["reader_agreement_within"] = _mean(within)
        summary[arm]["control_agreement_within"] = _mean(
            [jaccard(a["action_controls"], b["action_controls"]) for a, b in combinations(rows, 2)])
    for arm in ("I", "N"):
        pairs = [(metrics[f"{s}:S"], metrics[f"{s}:{arm}"]) for s in slots
                 if f"{s}:S" in metrics and f"{s}:{arm}" in metrics and metrics[f"{s}:S"]["valid"] and metrics[f"{s}:{arm}"]["valid"]]
        summary[f"{arm}_vs_S"] = {"pairs": len(pairs), "excluded_slots": len(slots) - len(pairs),
                                  "reader_agreement_between": _mean([jaccard(s["reader_fields"], o["reader_fields"]) for s, o in pairs]),
                                  "control_agreement_between": _mean([jaccard(s["action_controls"], o["action_controls"]) for s, o in pairs])}
        for name in secondary:
            diffs = [o[name] - s[name] for s, o in pairs if o[name] is not None and s[name] is not None]
            summary[f"{arm}_vs_S"][name] = paired_interval(diffs, f"{args.seed}:{arm}:{name}")
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    objects = {o["id"]: o.get("path") or o.get("name") for o in discovery.data.get("game_objects") or []}
    labels = {ident: f"{f.get('declaring_type')}.{f.get('name')} on '{objects.get(f.get('object_id'), f.get('object_id'))}'"
              for ident, f in fields.items()}
    review_sheet(out, chains, {token: labels.get(ident, ident) for token, ident in built.field_bindings.items()}, args.seed)
    print(json.dumps(summary, indent=2))
    return 0


# --- blind review and primary --------------------------------------------------------------------


def _check_text(check: dict[str, Any] | None, alias: dict[str, str | None]) -> str | None:
    if not check:
        return None
    field = alias.get(check["field"], check["field"])
    if check["op"] in ("changed", "unchanged") and check["value"] is None:
        return f"{field} {check['op']}"
    return f"{field} {check['op']} {json.dumps(check['value'])}"


def entries(row: dict[str, Any], bindings: dict[str, str]) -> list[tuple[tuple, dict[str, str]]]:
    """Stage-6 entries as (identity, display). Identity ignores free-text explanations."""
    answers = row.get("answers") or {}
    readers = ((answers.get("readers") or {}).get("stage_4") or {}).get("readers", [])
    progress = ((answers.get("progress") or {}).get("stage_6") or {})
    alias = {r["alias"]: bindings.get(r["field_id"]) for r in readers}
    found: dict[tuple, dict[str, str]] = {}
    for inv in progress.get("invariants", []):
        field = alias.get(inv["field"]) if inv.get("field") else None
        if inv["kind"] == "range":
            rule = f"{field} within [{inv['min']}, {inv['max']}]"
        elif inv["kind"] == "monotonic":
            rule = f"{field} is {inv['direction']}"
        else:
            rule = f"when {_check_text(inv['when'], alias)} then {_check_text(inv['then'], alias)}"
        if inv.get("unless"):
            rule += f", unless {_check_text(inv['unless'], alias)}"
        key = ("invariant", rule, inv["source"].strip())
        found.setdefault(key, {"kind": "invariant", "field": field or "", "rule": rule,
                               "cited_code": inv["source"], "explanation": inv["catches"]})
    for item in progress.get("progress", []):
        field = alias.get(item["field"])
        rule = f"{field} goes {item['direction']} as the game progresses"
        found.setdefault(("progress", rule, ""), {"kind": "progress", "field": field or "", "rule": rule,
                                                  "cited_code": "", "explanation": item["why"]})
    return list(found.items())


def review_sheet(out: Path, chains: dict[tuple[int, str], dict[str, Any]], bindings: dict[str, str], seed: int) -> None:
    """Blind sheet: arms hidden, entries deduplicated and shuffled, capped at REVIEW_CAP by whole slots."""
    complete = {k: r for k, r in chains.items() if r.get("status") == "complete"}
    slots = sorted({slot for slot, _ in complete})
    judged = slots
    for k in range(len(slots), MIN_JUDGED_SLOTS - 1, -1):
        keys = {key for (slot, _), r in complete.items() if slot in slots[:k] for key, _ in entries(r, bindings)}
        judged = slots[:k]
        if len(keys) <= REVIEW_CAP:
            break
    occurrences: dict[tuple, list[list[Any]]] = {}
    displays: dict[tuple, list[dict[str, str]]] = {}
    for (slot, arm), row in sorted(complete.items()):
        if slot not in judged:
            continue
        for key, display in entries(row, bindings):
            occurrences.setdefault(key, []).append([slot, arm])
            displays.setdefault(key, []).append(display)
    rng = random.Random(f"{seed}:review")
    items = sorted(occurrences, key=repr)
    rng.shuffle(items)
    key_rows, sheet_rows = [], []
    for index, key in enumerate(items, start=1):
        item_id = f"E{index:03d}"
        display = rng.choice(displays[key])  # a random occurrence's wording, so wording does not track one arm
        sheet_rows.append({"item_id": item_id, **display, "verdict": "", "rationale": ""})
        key_rows.append({"item_id": item_id, "identity": list(key), "occurrences": occurrences[key]})
    # Self-agreement: about 10% of entries again, reshuffled under fresh ids, judged later.
    repeat = rng.sample(sheet_rows, k=math.ceil(len(sheet_rows) / 10)) if sheet_rows else []
    rejudge = {f"R{index:03d}": row["item_id"] for index, row in enumerate(repeat, start=1)}
    _write_sheet(out / "review_sheet.csv", sheet_rows)
    _write_sheet(out / "rejudge_sheet.csv", [{**row, "item_id": new} for new, row in
                                             zip(rejudge, repeat)])
    (out / "review_key.json").write_text(json.dumps({"judged_slots": judged, "items": key_rows, "rejudge": rejudge},
                                                    indent=2) + "\n", encoding="utf-8")
    print(f"review sheet: {len(sheet_rows)} unique entries from slots {judged[0] if judged else '-'}-"
          f"{judged[-1] if judged else '-'}; re-judge sheet: {len(rejudge)}; key kept separately in review_key.json")


SHEET_FIELDS = ["item_id", "kind", "field", "rule", "cited_code", "explanation", "verdict", "rationale"]


def _write_sheet(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SHEET_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _read_verdicts(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return {row["item_id"]: row["verdict"].strip().lower() for row in csv.DictReader(stream)}


def command_primary(args: argparse.Namespace) -> int:
    out = Path(args.out)
    key = json.loads((out / "review_key.json").read_text(encoding="utf-8"))
    verdicts = _read_verdicts(Path(args.judgments))
    missing = [item["item_id"] for item in key["items"] if verdicts.get(item["item_id"]) not in VERDICTS]
    if missing:
        raise ValueError(f"{len(missing)} entries lack a verdict in {', '.join(VERDICTS)}; e.g. {missing[:5]}")
    correct: dict[tuple[int, str], int] = {}
    for item in key["items"]:
        for slot, arm in item["occurrences"]:
            correct.setdefault((slot, arm), 0)
            correct[(slot, arm)] += verdicts[item["item_id"]] == "correct"
    chains = _main_chains(Ledger(out / "ledger.jsonl"))
    for (slot, arm), row in chains.items():
        if slot in key["judged_slots"] and row.get("status") == "complete":
            correct.setdefault((slot, arm), 0)
    result: dict[str, Any] = {"judged_slots": key["judged_slots"], "margin": -MARGIN, "level": LEVEL,
                              "mean_correct": {arm: _mean([v for (s, a), v in correct.items() if a == arm]) for arm in ARMS},
                              "verdict_counts": {v: sum(1 for x in verdicts.values() if x == v) for v in VERDICTS}}
    for arm in ("I", "N"):
        diffs = [correct[(s, arm)] - correct[(s, "S")] for s in key["judged_slots"]
                 if (s, arm) in correct and (s, "S") in correct]
        interval = paired_interval(diffs, f"{args.seed}:primary:{arm}")
        result[f"{arm}_vs_S"] = {"pairs": len(diffs), "interval": interval,
                                 "decision": decide(interval, result["mean_correct"]["S"])}
    if args.rejudge is not None:
        again = _read_verdicts(Path(args.rejudge))
        pairs = [(verdicts[first], again.get(second)) for second, first in key["rejudge"].items()]
        judged = [(a, b) for a, b in pairs if b in VERDICTS]
        result["self_agreement"] = {"rejudged": len(judged), "of": len(pairs),
                                    "agreement": sum(a == b for a, b in judged) / len(judged) if judged else None}
    (out / "primary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


# --- CLI -----------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # a Windows console code page must not end a paid run
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("dry-run", "run", "score", "primary"):
        p = sub.add_parser(name)
        p.add_argument("--inputs", type=Path, required=True, help="frozen run directory")
        p.add_argument("--out", type=Path, required=True, help="output directory under runs/")
        p.add_argument("--inventory", default="survey.json")
        p.add_argument("--discovery", default="discovery.json")
        p.add_argument("--calibration", default="record.json")
        p.add_argument("--screenshot", default="frame.jpg")
        p.add_argument("--screenshot-mime", default="image/jpeg", choices=("image/png", "image/jpeg"))
        p.add_argument("--screenshot-provenance", required=True)
        p.add_argument("--seed", type=int, default=121)
        p.add_argument("--slots", type=int, default=10)
        p.add_argument("--pilot", type=int, default=1)
        if name == "dry-run":
            p.add_argument("--model", default=meta.MODEL, choices=tuple(provider.MODEL_PRICES))
        if name == "run":
            p.add_argument("--approval", type=Path, required=True)
            p.add_argument("--env-file", type=Path, required=True)
            p.add_argument("--phase", choices=("pilot", "main"))
            p.add_argument("--resume-after-stop", action="store_true")
            p.add_argument("--accept-script-change", metavar="REASON",
                           help="resume after the runner script changed; the reason goes in the ledger")
        if name == "primary":
            p.add_argument("--judgments", type=Path, required=True)
            p.add_argument("--rejudge", type=Path, help="the completed re-judge sheet, for self-agreement")
    args = parser.parse_args(argv)
    for name in ("inventory", "discovery", "calibration", "screenshot"):
        setattr(args, name, str(args.inputs / getattr(args, name)))
    try:
        return {"dry-run": command_dry_run, "run": command_run, "score": command_score,
                "primary": command_primary}[args.command](args)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"sourceless preflight failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
