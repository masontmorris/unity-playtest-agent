"""Candidate role schemas against Muse Spark 1.3 (#73). Not part of the product.

Phases, run one at a time so each can be reviewed before the next spends anything:

  features   Tiny synthetic prompts. Which strict keywords Muse accepts and which it enforces while
             decoding; whether enum values and descriptions are billed as prompt tokens; how the
             enum budget counts a shared `$defs` enum; and whether every candidate schema is
             accepted. A rejected request is HTTP 400 before generation and unbilled.
  preflight  Stage 4 -> 5 -> 6 as an append-only chain on the FPSKit packet with ONE schema for the
             whole chain (`roles.preflight.StageAnswer`), a stage-4 exact repeat as the cache
             control and at most one retry per stage with the exact problems; two single-call "all"
             answers; and a same-session control chain with the Day-1 per-stage schemas (#70).
  frontier   The candidate frontier schema and the Day-1 probe schema on four scenario packets,
             interleaved, each twice; an append-only conversation of three escalations; one call
             with a clip attached.
  stretch    One video-judge call with the clip and one audit call.

Every call is one row in calls.jsonl, written as it completes. Nothing is repaired, no model is
switched, and an invalid row is preserved; it stops only the calls that depend on it. A spend cap
per phase stops the phase before a call whose bound would exceed it (approved 26 Oct 2026:
contributor $0.20 in total, Standard $0.25).

    uv run --extra probe python scripts/probes/probe_schemas.py --phase features --project <dir> --dry-run
    uv run --extra probe python scripts/probes/probe_schemas.py --phase preflight --project <dir> --yes
    uv run --extra probe python scripts/probes/probe_schemas.py --phase frontier --yes
    uv run --extra probe python scripts/probes/probe_schemas.py --phase preflight --project <dir> \
        --model muse-spark-1.3 --arms staged --runs 1 --yes
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import jsonschema

sys.path.insert(0, str(Path(__file__).parent))
import scenarios  # noqa: E402
from common import Recorder, clip_mp4, data_url, env, run_dir, timed  # noqa: E402
from packets import FRONTIER_SCHEMA, FRONTIER_SYSTEM  # noqa: E402
from probe_preflight import discover  # noqa: E402
from probe_preflight_full import PRICES, discover_controls, price  # noqa: E402
from probe_preflight_full import STAGES as DAY1_STAGES  # noqa: E402
from probe_preflight_full import SYSTEM as DAY1_SYSTEM  # noqa: E402
from probe_preflight_full import schemas as day1_schemas  # noqa: E402

from playtest.contracts.investigation import Comparator  # noqa: E402
from playtest.roles import audit, frontier, judge, preflight, strict  # noqa: E402
from playtest.roles.checks import check_problems  # noqa: E402

URL = "https://api.meta.ai/v1/chat/completions"
CONTRIBUTOR = "muse-spark-1.3-contributor"
STANDARD = "muse-spark-1.3"
EFFORT = "minimal"
CAPS = {  # USD per phase run; the approved totals are contributor $0.20 and Standard $0.25
    (CONTRIBUTOR, "features"): 0.01, (CONTRIBUTOR, "preflight"): 0.10, (CONTRIBUTOR, "frontier"): 0.04,
    (CONTRIBUTOR, "stretch"): 0.005, (CONTRIBUTOR, "refine"): 0.02,
    (STANDARD, "preflight"): 0.20, (STANDARD, "frontier"): 0.05,
}
# Per-call planning bounds: input tokens, output tokens (output includes reasoning).
BOUNDS = {"features": (4_000, 3_000), "preflight": (34_000, 5_000), "frontier": (8_000, 4_000),
          "stretch": (8_000, 3_000), "refine": (34_000, 5_000)}
# Enum counting assumed when rendering candidate schemas. The features phase measures it.
LIMITS = strict.Limits(enum_counting="definitions")


def bound_usd(model: str, phase: str) -> float:
    pin, _, pout = PRICES[model]
    tin, tout = BOUNDS[phase]
    return (tin * pin + tout * pout) / 1e6


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def body(model: str, messages: list[dict], name: str, schema: dict, max_tokens: int = 16_000) -> dict:
    return {"model": model, "max_tokens": max_tokens, "reasoning_effort": EFFORT, "messages": messages,
            "response_format": strict.response_format(name, schema)}


def schema_errors(schema: dict, data: Any) -> list[str]:
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.absolute_path))
    return [f"schema: {'/'.join(map(str, e.absolute_path)) or '$'}: {e.message[:160]}" for e in errors[:10]]


def loads(content: str | None) -> Any:
    try:
        return json.loads(content) if isinstance(content, str) else None
    except ValueError:
        return None


Validate = Callable[[str | None], tuple[Any, list[str], dict]]


class Ledger:
    def __init__(self, cap: float):
        self.cap, self.spent, self.calls = cap, 0.0, 0

    def allows(self, bound: float) -> bool:
        return self.spent + bound <= self.cap + 1e-12


class Session:
    """One phase run: client, key, model, ledger and recorder. `--dry-run` never reads the key."""

    def __init__(self, args: argparse.Namespace, phase: str, rec: Recorder | None):
        self.args, self.phase, self.rec = args, phase, rec
        self.model = args.model
        self.ledger = Ledger(args.cap if args.cap is not None else CAPS[(args.model, phase)])
        self.bound = bound_usd(args.model, phase)
        self.client: httpx.AsyncClient | None = None
        self.key: str | None = None
        self.planned: list[str] = []
        self.stopped = False

    async def send(self, label: str, b: dict, validate: Validate, **meta: Any) -> dict | None:
        if self.args.dry_run:
            chars = len(json.dumps(b["messages"])) + len(json.dumps(b["response_format"]))
            self.planned.append(f"{label:52} ~{chars // 4:>6} tokens in (chars/4), bound ${self.bound:.4f}")
            return None
        if self.stopped:
            return None
        if not self.ledger.allows(self.bound):
            print(f"{label}: not sent; ${self.ledger.spent:.4f} spent + ${self.bound:.4f} bound would pass the "
                  f"${self.ledger.cap:.2f} cap", flush=True)
            self.stopped = True
            return None
        assert self.client is not None and self.key is not None
        t0 = timed()
        status, text, problems = None, "", []
        try:
            r = await self.client.post(URL, json=b, headers={"Authorization": f"Bearer {self.key}"})
            status, text = r.status_code, r.text
        except Exception as exc:  # transport or timeout; recorded, never retried
            problems.append(f"transport {type(exc).__name__}: {str(exc)[:200]}")
        ms = (timed() - t0) * 1000
        data: dict = {}
        if status == 200:
            try:
                data = json.loads(text)
            except ValueError as exc:
                problems.append(f"response is not JSON: {exc}")
        elif status is not None:
            problems.append(f"HTTP {status}: {text[:500]}")
        content = output = finish = None
        details: dict = {}
        if data:
            if data.get("model") != b["model"]:
                problems.append(f"resolved model {data.get('model')!r} is not {b['model']!r}")
            choice = (data.get("choices") or [{}])[0] or {}
            finish = choice.get("finish_reason")
            content = (choice.get("message") or {}).get("content")
            if finish not in (None, "stop"):
                problems.append(f"finish_reason {finish}")
            output, found, details = validate(content)
            problems += found
        usage = data.get("usage") or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") if usage else None
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") if usage else None
        cost = price(b["model"], usage) if usage else None
        if status == 200 and cost is None:
            problems.append("cannot price usage")
        billed = cost if cost is not None else (0.0 if status is not None and 400 <= status < 500 else self.bound)
        self.ledger.spent += billed
        self.ledger.calls += 1
        row = {
            "label": label, "phase": self.phase, **meta,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": b["model"], "resolved_model": data.get("model"), "effort": b.get("reasoning_effort"),
            "status": status, "finish_reason": finish, "latency_ms": round(ms, 1), "problems": problems,
            "usage": usage or None, "prompt_tokens": usage.get("prompt_tokens"), "cached_tokens": cached,
            "completion_tokens": usage.get("completion_tokens"), "reasoning_tokens": reasoning,
            "est_cost_usd": cost, "billed_or_bound_usd": billed,
            "messages_sha256": digest(b["messages"]), "format_sha256": digest(b["response_format"]),
            "format_chars": len(strict.canonical(b["response_format"])), "body_sha256": digest(b),
            "details": details, "output": output, "assistant_content": content, "request": b,
        }
        assert self.rec is not None
        self.rec.write(row)
        cost_text = "unknown" if cost is None else f"${cost:.5f}"
        print(f"{label:52} {status if status is not None else '---'} {ms / 1000:5.1f}s in={usage.get('prompt_tokens')} "
              f"cached={cached} out={usage.get('completion_tokens')} (r={reasoning}) {cost_text}  "
              f"{'ok' if not problems else problems[0][:150]}", flush=True)
        if status is not None and status in (401, 402, 403, 429):
            print("account or rate error: stopping the phase", flush=True)
            self.stopped = True
        return row


# --- features ------------------------------------------------------------------------------------

FEATURE_SYSTEM = "Answer with JSON that matches the response schema, following the user's request as closely as it allows."


def _obj(props: dict, defs: dict | None = None) -> dict:
    out = {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    if defs:
        out["$defs"] = defs
    return out


def feature_cases(disc: preflight.Discovery | None) -> list[tuple[str, dict, str, Callable[[Any], dict]]]:
    """(name, schema, user prompt, judge(output) -> details)."""
    enum200 = [f"v{i:03d}" for i in range(200)]
    enum300 = [f"w{i:03d}" for i in range(300)]
    enum260 = [f"z{i:03d}" for i in range(260)]

    def nested(objects: int) -> dict:
        node: dict = {"type": "string"}
        for _ in range(objects):
            node = _obj({"n": node})
        return node

    cases: list[tuple[str, dict, str, Callable[[Any], dict]]] = [
        ("base", _obj({"a": {"type": "string"}}), "Set a to hello.", lambda o: {}),
        ("description", _obj({"a": {"type": "string", "description": "A short greeting in English, at most five words."}}),
         "Set a to hello.", lambda o: {}),
        ("enum_3", _obj({"a": {"type": "string", "enum": ["x", "y", "z"]}}), "Set a to y.", lambda o: {}),
        ("enum_200", _obj({"a": {"type": "string", "enum": enum200}}), "Set a to v123.", lambda o: {}),
        ("maxItems", _obj({"items": {"type": "array", "items": {"type": "string"}, "maxItems": 2}}),
         "List six different colours in items.", lambda o: {"n": len(o["items"]), "enforced": len(o["items"]) <= 2}),
        ("minItems", _obj({"items": {"type": "array", "items": {"type": "string"}, "minItems": 4}}),
         "List exactly one colour in items.", lambda o: {"n": len(o["items"]), "enforced": len(o["items"]) >= 4}),
        ("minimum_maximum", _obj({"n": {"type": "number", "minimum": 0, "maximum": 10}}), "Set n to 500.",
         lambda o: {"n": o["n"], "enforced": 0 <= o["n"] <= 10}),
        ("exclusiveMinimum", _obj({"n": {"type": "number", "exclusiveMinimum": 0}}), "Set n to -3.",
         lambda o: {"n": o["n"], "enforced": o["n"] > 0}),
        ("pattern", _obj({"s": {"type": "string", "pattern": "^h[0-9]+$"}}), "Set s to banana.",
         lambda o: {"s": o["s"], "enforced": bool(re.match("^h[0-9]+$", o["s"]))}),
        ("maxLength", _obj({"s": {"type": "string", "maxLength": 8}}),
         "Set s to a sentence of at least twelve words.", lambda o: {"len": len(o["s"]), "enforced": len(o["s"]) <= 8}),
        ("integer_enum", _obj({"n": {"type": "integer", "enum": [1, 2, 3]}}), "Set n to 7.",
         lambda o: {"n": o["n"], "enforced": o["n"] in (1, 2, 3)}),
        ("const", _obj({"k": {"type": "string", "const": "fixed"}}), "Set k to other.",
         lambda o: {"k": o["k"], "enforced": o["k"] == "fixed"}),
        ("ref_with_sibling", _obj({"a": {"$ref": "#/$defs/E", "description": "pick one"}},
                                  {"E": {"type": "string", "enum": ["p", "q"]}}), "Set a to q.", lambda o: {}),
        ("anyOf_objects", _obj({"answer": {"anyOf": [
            _obj({"kind": {"type": "string", "enum": ["circle"]}, "radius": {"type": "number"}}),
            _obj({"kind": {"type": "string", "enum": ["box"]}, "w": {"type": "number"}, "h": {"type": "number"}})]}}),
         "Describe a box 2 wide and 3 high.", lambda o: {"kind": o["answer"].get("kind")}),
        ("oneOf", _obj({"a": {"oneOf": [{"type": "string"}, {"type": "number"}]}}), "Set a to 3.", lambda o: {}),
        ("enum_300_shared_x4", _obj({f"a{i}": {"$ref": "#/$defs/W"} for i in range(4)},
                                    {"W": {"type": "string", "enum": enum300}}),
         "Set a0 to w001, a1 to w002, a2 to w003 and a3 to w004.", lambda o: {}),
        ("enum_300_inline_x4", _obj({f"a{i}": {"type": "string", "enum": enum300} for i in range(4)}),
         "Set a0 to w001, a1 to w002, a2 to w003 and a3 to w004.", lambda o: {}),
        ("enum_260_single", _obj({"a": {"type": "string", "enum": enum260}}), "Set a to z200.", lambda o: {}),
        ("depth_8_objects", nested(8), "Fill the nested value with ok.", lambda o: {}),
        ("depth_10_objects", nested(10), "Fill the nested value with ok.", lambda o: {}),
        ("depth_11_objects", nested(11), "Fill the nested value with ok.", lambda o: {}),
    ]
    sc = scenarios.manifest()
    cases.append(("frontier_candidate", frontier.schema(sc, limits=LIMITS),
                  "Return no hypotheses, a null directive, no choices and a null candidate_finding.",
                  lambda o: {"valid": not strict.parse(frontier.FrontierAnswer, json.dumps(o))[1]}))
    if disc is not None:
        cases += candidate_cases(disc)
    return cases


def candidate_cases(disc: preflight.Discovery) -> list[tuple[str, dict, str, Callable[[Any], dict]]]:
    """Set 3: is every current candidate schema accepted, and what do its rendered tokens cost?"""
    sch = preflight.schema(disc, limits=LIMITS)
    st = strict.stats(sch)
    return [
        ("candidate_preflight", sch,
         "Answer stage 4: one reader with field_id f1, alias first_field and why 'probe', nothing unobservable; "
         "stage_5 and stage_6 null.",
         lambda o, st=st: {"enum_definitions": st.enum_values_definitions, "enum_expanded": st.enum_values_expanded,
                           "depth": st.depth, "valid": not strict.parse(preflight.StageAnswer, json.dumps(o))[1]}),
        ("candidate_frontier", frontier.schema(scenarios.manifest(), limits=LIMITS),
         "Return no hypotheses, a null directive, no choices and a null candidate_finding.",
         lambda o: {"valid": not strict.parse(frontier.FrontierAnswer, json.dumps(o))[1]}),
        ("candidate_judge", judge.schema(["q1", "q2"]),
         "Answer q1 yes at 1.0 s and q2 unclear; nothing unexpected.", lambda o: {}),
        ("candidate_audit", audit.schema(), "Return no notes.", lambda o: {}),
    ]


def feature_followups() -> list[tuple[str, dict, str, Callable[[Any], dict]]]:
    """Set 2, after set 1 showed a box request answered with the first (circle) branch."""
    circle = _obj({"kind": {"type": "string", "enum": ["circle"]}, "radius": {"type": "number"}})
    box = _obj({"kind": {"type": "string", "enum": ["box"]}, "w": {"type": "number"}, "h": {"type": "number"}})
    kind = lambda o: {"kind": o["answer"].get("kind")}  # noqa: E731
    rules = {"type": "array", "maxItems": 3, "items": {"anyOf": [
        _obj({"rule": {"type": "string", "enum": ["range"]}, "field": {"type": "string"},
              "min": {"type": "number"}, "max": {"type": "number"}}),
        _obj({"rule": {"type": "string", "enum": ["monotonic"]}, "field": {"type": "string"},
              "direction": {"type": "string", "enum": ["up", "down"]}}),
        _obj({"rule": {"type": "string", "enum": ["implies"]}, "when": {"type": "string"},
              "then": {"type": "string"}})]}}
    unique = lambda n, p: [f"{p}{i:04d}" for i in range(n)]  # noqa: E731
    cases: list[tuple[str, dict, str, Callable[[Any], dict]]] = []
    for i in range(3):
        cases.append((f"anyOf_circle_first_ask_box_{i + 1}", _obj({"answer": {"anyOf": [circle, box]}}),
                      "Describe a box 2 wide and 3 high.", kind))
    for i in range(2):
        cases.append((f"anyOf_box_first_ask_circle_{i + 1}", _obj({"answer": {"anyOf": [box, circle]}}),
                      "Describe a circle of radius 4.", kind))
    cases.append(("anyOf_array_three_kinds", _obj({"rules": rules}),
                  "Write three rules, one of each kind: first an implies rule (when 'paused' then 'still'), then a "
                  "monotonic rule on field 'score' going up, then a range rule on field 'speed' from 0 to 9.",
                  lambda o: {"kinds": [r.get("rule") for r in o["rules"]]}))
    cases.append(("anyOf_array_implies_only", _obj({"rules": rules}),
                  "Write one implies rule: when 'paused' then 'still'.",
                  lambda o: {"kinds": [r.get("rule") for r in o["rules"]]}))
    cases += [
        ("enum_999_unique", _obj({"a": {"type": "string", "enum": unique(999, "e")}}), "Set a to e0500.", lambda o: {}),
        ("enum_1001_unique", _obj({"a": {"type": "string", "enum": unique(1001, "e")}}), "Set a to e0500.", lambda o: {}),
        ("enum_600_600_distinct", _obj({"a": {"type": "string", "enum": unique(600, "p")},
                                        "b": {"type": "string", "enum": unique(600, "q")}}),
         "Set a to p0100 and b to q0200.", lambda o: {}),
        ("minLength", _obj({"s": {"type": "string", "minLength": 20}}), "Set s to hi.",
         lambda o: {"len": len(o["s"]), "enforced": len(o["s"]) >= 20}),
        ("exclusiveMaximum", _obj({"n": {"type": "number", "exclusiveMaximum": 1}}), "Set n to 5.",
         lambda o: {"n": o["n"], "enforced": o["n"] < 1}),
    ]
    return cases


async def phase_features(s: Session, disc: preflight.Discovery | None) -> None:
    if s.args.feature_set == 3 and disc is None:
        print("feature set 3 needs --project")
        return
    cases = {1: lambda: feature_cases(disc), 2: feature_followups, 3: lambda: candidate_cases(disc)}[s.args.feature_set]()
    for name, sch, prompt, judge_fn in cases:
        def validate(content: str | None, sch: dict = sch, judge_fn: Callable = judge_fn) -> tuple[Any, list[str], dict]:
            data = loads(content)
            if data is None:
                return None, ["answer is not JSON"], {}
            problems = schema_errors(sch, data)
            try:
                details = judge_fn(data)
            except Exception as exc:  # the judge reads the answer; a shape surprise is a finding, not a crash
                details, problems = {}, [*problems, f"judge: {type(exc).__name__}: {exc}"]
            return data, problems, details
        msgs = [{"role": "system", "content": FEATURE_SYSTEM}, {"role": "user", "content": prompt}]
        await s.send(f"features/{name}", body(s.model, msgs, "probe", sch, max_tokens=4_000), validate, case=name)


# --- preflight -----------------------------------------------------------------------------------


def load_project(root: Path) -> tuple[str, list[dict], list[dict]]:
    """The packet #68 and #70 sent: discovery JSON, then the source, built the same way."""
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*.cs") if "Editor" not in p.parts)
    fields, code = discover(root, files)
    controls = discover_controls(root)
    shared = json.dumps({"discovered_fields": fields, "discovered_controls": controls}) + "\n\nSource:\n" + code
    return shared, fields, controls


def summarize(answer: preflight.StageAnswer, fields: dict[str, str], controls: dict[str, str]) -> dict:
    out: dict[str, Any] = {}
    if answer.stage_4 is not None:
        out["readers"] = [f"{r.field_id}={fields.get(r.field_id)} as {r.alias}" for r in answer.stage_4.readers]
        out["unobservable"] = [g[:100] for g in answer.stage_4.unobservable]
    if answer.stage_5 is not None:
        out["actions"] = [f"{a.name}: " + "+".join(f"{controls.get(c.control_id)}({c.x},{c.y})" for c in a.inputs)
                          + f" {a.hold_s}s pre={len(a.preconditions)}" for a in answer.stage_5.actions]
        out["skills"] = [f"{k.name} [{len(k.steps)} steps, max {k.max_s}s]" for k in answer.stage_5.skills]
        out["uncontrollable"] = [g[:100] for g in answer.stage_5.uncontrollable]
    if answer.stage_6 is not None:
        out["progress"] = [f"{p.field} {p.direction}" for p in answer.stage_6.progress]
        out["invariants"] = [f"{i.kind}:{i.name}" for i in answer.stage_6.invariants]
        out["unjudgeable"] = [g[:100] for g in answer.stage_6.unjudgeable]
    return out


def stage_validator(stage: str, sch: dict, disc: preflight.Discovery, readers: dict[str, str],
                    names: tuple[dict, dict]) -> Validate:
    def validate(content: str | None) -> tuple[Any, list[str], dict]:
        data = loads(content)
        if data is None:
            return None, ["answer is not JSON"], {}
        problems = schema_errors(sch, data)
        parsed, errors = strict.parse(preflight.StageAnswer, content)
        if parsed is None:
            return data, problems + [f"model: {e}" for e in errors], {}
        found = preflight.review(parsed, stage, disc, readers)  # type: ignore[arg-type]
        return data, problems + [f"review: {p}" for p in found], {
            "review_problems": found, "aliases": parsed.aliases(),  # type: ignore[attr-defined]
            "summary": summarize(parsed, *names)}  # type: ignore[arg-type]
    return validate


def accepted(row: dict | None) -> bool:
    return bool(row) and row["status"] == 200 and not row["problems"]


def only_review_problems(row: dict | None) -> bool:
    return bool(row) and row["status"] == 200 and bool(row["problems"]) and all(
        p.startswith("review: ") for p in row["problems"])


async def chain(s: Session, run: int, shared: str, disc: preflight.Discovery, names: tuple) -> None:
    sch = preflight.schema(disc, limits=LIMITS)
    variant = "staged"
    tag = f"{variant}#{run}"
    messages = [{"role": "system", "content": preflight.SYSTEM},
                {"role": "user", "content": preflight.first_turn(shared, "readers")}]
    readers: dict[str, str] = {}
    for stage in ("readers", "actions", "progress"):
        if stage != "readers":
            messages = [*messages, {"role": "user", "content": preflight.STAGE_PROMPTS[stage]}]
        validate = stage_validator(stage, sch, disc, readers, names)
        b = body(s.model, messages, "preflight_answer", sch, max_tokens=32_000)
        row = await s.send(f"{tag}/{stage}", b, validate, arm=variant, run=run, stage=stage, attempt=1)
        if stage == "readers":
            await s.send(f"{tag}/readers exact repeat (cache control)", b, validate, arm=variant, run=run,
                         stage=stage, attempt="control")
        if only_review_problems(row):
            assert row is not None
            messages = [*messages, {"role": "assistant", "content": row["assistant_content"]},
                        {"role": "user", "content": preflight.retry_turn(stage, row["details"]["review_problems"])}]
            b = body(s.model, messages, "preflight_answer", sch, max_tokens=32_000)
            row = await s.send(f"{tag}/{stage} retry", b, validate, arm=variant, run=run, stage=stage, attempt=2)
        if not accepted(row):
            if not s.args.dry_run:
                print(f"{tag}: {stage} not accepted; the rest of this chain depends on it and is not sent", flush=True)
                return
        if row is not None:
            messages = [*messages, {"role": "assistant", "content": row["assistant_content"]}]
            if stage == "readers":
                readers = dict(row["details"]["aliases"])
        else:
            messages = [*messages, {"role": "assistant", "content": "{}"}]  # dry run placeholder


async def single_call(s: Session, run: int, shared: str, disc: preflight.Discovery, names: tuple) -> None:
    sch = preflight.schema(disc, limits=LIMITS)
    messages = [{"role": "system", "content": preflight.SYSTEM},
                {"role": "user", "content": preflight.first_turn(shared, "all")}]
    await s.send(f"all#{run}/all stages in one call", body(s.model, messages, "preflight_answer", sch, 32_000),
                 stage_validator("all", sch, disc, {}, names), arm="all", run=run, stage="all",
                 attempt=1)


async def day1_chain(s: Session, shared: str, fields: list[dict], controls: list[dict]) -> None:
    """The #70 control: Day-1 prompts and per-stage schemas, appended turns, same session."""
    sch = day1_schemas([f["id"] for f in fields], [c["id"] for c in controls])

    def validate_for(stage: str) -> Validate:
        def validate(content: str | None) -> tuple[Any, list[str], dict]:
            data = loads(content)
            return data, (["answer is not JSON"] if data is None else schema_errors(sch[stage], data)), {}
        return validate

    messages = [{"role": "system", "content": DAY1_SYSTEM},
                {"role": "user", "content": shared + "\n\n=== INSTRUCTIONS ===\n" + DAY1_STAGES["readers"]}]
    for stage in ("readers", "actions", "progress"):
        if stage != "readers":
            messages = [*messages, {"role": "user", "content": DAY1_STAGES[stage]}]
        b = body(s.model, messages, f"stage_{stage}", sch[stage], max_tokens=32_000)
        row = await s.send(f"day1/{stage}", b, validate_for(stage), arm="day1", run=1, stage=stage, attempt=1)
        if stage == "readers":
            await s.send("day1/readers exact repeat (cache control)", b, validate_for(stage), arm="day1", run=1,
                         stage=stage, attempt="control")
        if not s.args.dry_run and not accepted(row):
            print(f"day1: {stage} not accepted; stopping this chain", flush=True)
            return
        messages = [*messages, {"role": "assistant", "content": row["assistant_content"] if row else "{}"}]


async def phase_preflight(s: Session, project: Path) -> None:
    shared, fields, controls = load_project(project)
    disc = preflight.Discovery.from_records(fields, controls)
    names = ({f["id"]: f"{f['component']}.{f['field']}" for f in fields}, {c["id"]: c["action"] for c in controls})
    print(f"packet: {len(fields)} fields, {len(controls)} controls, {len(shared):,} chars", flush=True)
    arms = s.args.arms.split(",") if s.args.arms else ["staged", "day1", "all"]
    runs = s.args.runs
    order: list[tuple[str, int]] = []
    for run in range(1, runs + 1):
        if "staged" in arms:
            order.append(("staged", run))
        if run == 1 and "day1" in arms:
            order.append(("day1", 1))
    if "all" in arms:
        order += [("all", run) for run in range(1, runs + 1)]
    for arm, run in order:
        if s.stopped:
            break
        if arm == "staged":
            await chain(s, run, shared, disc, names)
        elif arm == "day1":
            await day1_chain(s, shared, fields, controls)
        else:
            await single_call(s, run, shared, disc, names)


# --- frontier ------------------------------------------------------------------------------------


def frontier_validator(arm: str, name: str, sch: dict) -> Validate:
    spec = scenarios.manifest()
    refs = scenarios.refs(name)

    def validate(content: str | None) -> tuple[Any, list[str], dict]:
        data = loads(content)
        if data is None:
            return None, ["answer is not JSON"], {}
        problems = schema_errors(sch, data)
        if arm == "candidate":
            parsed, errors = strict.parse(frontier.FrontierAnswer, content)
            if parsed is None:
                return data, problems + [f"model: {e}" for e in errors], {}
            rv = frontier.review(parsed, spec, refs)  # type: ignore[arg-type]
            d = parsed.directive  # type: ignore[attr-defined]
            return data, problems + [f"review: {p.item}: {p.reason}" for p in rv.problems], {
                "directive": None if d is None else {
                    "scoreable": d.expected is not None and not rv.rejected("directive"),
                    "expected": None if d.expected is None else d.expected.model_dump(mode="json"),
                    "deadline_s": d.deadline_s, "tests": d.tests},
                "hypotheses": [(h.id, h.status) for h in parsed.hypotheses],  # type: ignore[attr-defined]
                "adds": [a.name for a in parsed.add_choices],  # type: ignore[attr-defined]
                "prunes": [p.name for p in parsed.prune_choices],  # type: ignore[attr-defined]
                "finding": parsed.candidate_finding is not None,  # type: ignore[attr-defined]
                "rejected_items": sorted({p.item for p in rv.problems})}
        # Day-1 probe schema: the same reference checks, mapped onto its shape.
        exp = (data.get("directive") or {}).get("expected")
        scoreable = False
        if exp is not None:
            kind = spec.fields.get(exp.get("field"))
            if kind is None:
                problems.append(f"review: directive: {exp.get('field')!r} is not a manifest field")
            else:
                found = check_problems("directive", kind, Comparator(exp["op"]), exp.get("value"))
                problems += [f"review: {p}" for p in found]
                scoreable = not found
        names = {*spec.actions, *spec.skills}
        for i, a in enumerate(data.get("add_choices") or []):
            if a.get("skill_or_action") not in names:
                problems.append(f"review: add_choices[{i}]: {a.get('skill_or_action')!r} is not a manifest name")
        f = data.get("candidate_finding")
        if f is not None and set(f.get("evidence_seq") or []) - refs.seqs:
            problems.append(f"review: candidate_finding cites {sorted(set(f['evidence_seq']) - refs.seqs)}")
        return data, problems, {"directive": {"scoreable": scoreable, "expected": exp},
                                "hypotheses": len(data.get("hypotheses") or []),
                                "adds": [a.get("skill_or_action") for a in data.get("add_choices") or []],
                                "finding": f is not None}
    return validate


def frontier_first(arm: str) -> list[dict]:
    system = frontier.SYSTEM if arm == "candidate" else FRONTIER_SYSTEM
    return [{"role": "system", "content": system}, {"role": "user", "content": scenarios.manifest_message()}]


def frontier_schema(arm: str) -> tuple[str, dict]:
    if arm == "candidate":
        return "frontier_answer", frontier.schema(scenarios.manifest(), limits=LIMITS)
    return "FrontierResponse", FRONTIER_SCHEMA


async def phase_frontier(s: Session) -> None:
    arms = s.args.arms.split(",") if s.args.arms else ["candidate", "baseline"]
    names = s.args.scenarios.split(",")
    for run in range(1, s.args.runs + 1):
        for k, name in enumerate(names):
            pair = arms if (run + k) % 2 else list(reversed(arms))
            for arm in pair:
                fmt, sch = frontier_schema(arm)
                messages = [*frontier_first(arm), {"role": "user", "content": scenarios.packet_message(name)}]
                await s.send(f"{arm}#{run}/{name}", body(s.model, messages, fmt, sch), frontier_validator(arm, name, sch),
                             arm=arm, run=run, scenario=name)
    if s.args.conversation and "candidate" in arms:
        fmt, sch = frontier_schema("candidate")
        messages = frontier_first("candidate")
        for name in ("A", "C", "B"):
            messages = [*messages, {"role": "user", "content": scenarios.packet_message(name)}]
            row = await s.send(f"conversation/{name} (append)", body(s.model, messages, fmt, sch),
                               frontier_validator("candidate", name, sch), arm="conversation", scenario=name)
            if not s.args.dry_run and (row is None or row["status"] != 200):
                break
            messages = [*messages, {"role": "assistant", "content": (row or {}).get("assistant_content") or "{}"}]
    if s.args.video and "candidate" in arms:
        fmt, sch = frontier_schema("candidate")
        clip = data_url(clip_mp4(), "video/mp4")
        user = [{"type": "text", "text": "The clip is attached: 4 s, 640x360, aligned with seq 4008-4031.\n"
                 + scenarios.packet_message("D", clip_attached=True)},
                {"type": "video_url", "video_url": {"url": clip}}]
        messages = [*frontier_first("candidate"), {"role": "user", "content": user}]
        await s.send("candidate/D with clip", body(s.model, messages, fmt, sch), frontier_validator("candidate", "D", sch),
                     arm="candidate", scenario="D", clip=True)


# --- refine --------------------------------------------------------------------------------------


async def phase_refine(s: Session, project: Path) -> None:
    """Paired prompt checks after the first runs.

    Stage 6: a recorded chain's exact stage-6 request (history and schema unchanged) is sent with its
    recorded instruction and with the current one, in ABBA order. Frontier: scenario C with the
    current system prompt, twice, to compare with the recorded scenario-C answers.
    """
    _, fields, controls = load_project(project)
    disc = preflight.Discovery.from_records(fields, controls)
    names = ({f["id"]: f"{f['component']}.{f['field']}" for f in fields}, {c["id"]: c["action"] for c in controls})
    rows = [json.loads(line) for line in Path(s.args.from_run).read_text(encoding="utf-8").splitlines()]
    base = next(r for r in rows if r["label"] == s.args.from_label)
    recorded = base["request"]
    if recorded["model"] != s.model:
        print(f"the recorded request used {recorded['model']}; pass --model {recorded['model']}")
        return
    readers_answer = next(m["content"] for m in recorded["messages"] if m["role"] == "assistant")
    parsed, errors = strict.parse(preflight.StageAnswer, readers_answer)
    if parsed is None:
        print(f"cannot read the recorded stage-4 answer: {errors}")
        return
    aliases = parsed.aliases()  # type: ignore[attr-defined]
    current = json.loads(json.dumps(recorded))
    current["messages"][-1]["content"] = preflight.STAGE_PROMPTS["progress"]
    sch = recorded["response_format"]["json_schema"]["schema"]
    validate = stage_validator("progress", sch, disc, aliases, names)
    for label, b in (("recorded", recorded), ("current", current), ("current", current), ("recorded", recorded)):
        await s.send(f"refine/stage6 {label} prompt", b, validate, arm=f"stage6-{label}", stage="progress")
    fmt, fsch = frontier_schema("candidate")
    for run in (1, 2):
        messages = [*frontier_first("candidate"), {"role": "user", "content": scenarios.packet_message("C")}]
        await s.send(f"refine/frontier C current prompt #{run}", body(s.model, messages, fmt, fsch),
                     frontier_validator("candidate", "C", fsch), arm="frontier-current", run=run, scenario="C")


# --- stretch -------------------------------------------------------------------------------------


async def phase_stretch(s: Session) -> None:
    questions = {"q1": "Does the image change during the clip?", "q2": "Is any shape moving across the frame?"}
    sch = judge.schema(list(questions))

    def judge_validate(content: str | None) -> tuple[Any, list[str], dict]:
        data = loads(content)
        parsed, errors = strict.parse(judge.JudgeAnswer, content)
        if parsed is None:
            return data, [f"model: {e}" for e in errors], {}
        found = judge.review(parsed, list(questions), 4.0)  # type: ignore[arg-type]
        return data, schema_errors(sch, data) + [f"review: {p}" for p in found], {}

    clip = data_url(clip_mp4(), "video/mp4")
    user = [{"type": "text", "text": json.dumps({"clip_seconds": 4.0, "questions": questions})},
            {"type": "video_url", "video_url": {"url": clip}}]
    await s.send("judge/test-pattern clip", body(s.model, [{"role": "system", "content": judge.SYSTEM},
                                                           {"role": "user", "content": user}], "judge_answer", sch),
                 judge_validate, arm="judge")

    record = {name: scenarios.SCENARIOS[name] for name in ("A", "C")}
    seqs = scenarios.citable("A") | scenarios.citable("C")
    asch = audit.schema()

    def audit_validate(content: str | None) -> tuple[Any, list[str], dict]:
        data = loads(content)
        parsed, errors = strict.parse(audit.AuditAnswer, content)
        if parsed is None:
            return data, [f"model: {e}" for e in errors], {}
        found = audit.review(parsed, seqs)  # type: ignore[arg-type]
        return data, schema_errors(asch, data) + [f"review: {p}" for p in found], {}

    await s.send("audit/record A+C", body(s.model, [{"role": "system", "content": audit.SYSTEM},
                                                    {"role": "user", "content": json.dumps({"record": record})}],
                                          "audit_answer", asch), audit_validate, arm="audit")


# --- main ----------------------------------------------------------------------------------------


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=["features", "preflight", "frontier", "refine", "stretch"])
    ap.add_argument("--from-run", help="refine: a preflight calls.jsonl to take the stage-6 request from")
    ap.add_argument("--from-label", default="staged#2/progress", help="refine: the recorded row label")
    ap.add_argument("--project", type=Path, help="exported Unity project (FPSKit fork) for features/preflight")
    ap.add_argument("--model", default=CONTRIBUTOR, choices=sorted(PRICES))
    ap.add_argument("--arms", help="comma list: preflight staged,day1,all; frontier candidate,baseline")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--scenarios", default="A,B,C,D")
    ap.add_argument("--feature-set", type=int, default=1, choices=[1, 2, 3],
                    help="features: 1 initial, 2 follow-ups, 3 current candidate schemas")
    ap.add_argument("--conversation", action="store_true", help="frontier: three appended escalations")
    ap.add_argument("--video", action="store_true", help="frontier: one call with the clip attached")
    ap.add_argument("--cap", type=float, help="override the phase spend cap, USD")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; no key read, no call")
    ap.add_argument("--yes", action="store_true", help="spend up to the cap")
    args = ap.parse_args()
    if (args.model, args.phase) not in CAPS and args.cap is None:
        print(f"no approved cap for {args.model} {args.phase}; pass --cap after approval")
        return 2
    if args.phase in ("preflight", "refine") and not args.project:
        print(f"--project is required for the {args.phase} phase")
        return 2
    if args.phase == "refine" and not args.from_run:
        print("--from-run is required for the refine phase")
        return 2
    if not args.dry_run and not args.yes:
        print("refusing to spend without --yes (use --dry-run to see the plan)")
        return 2
    rec = None if args.dry_run else Recorder(run_dir(f"schemas-{args.phase}-{args.model.split('-')[-1]}"))
    s = Session(args, args.phase, rec)
    print(f"phase {args.phase}, model {args.model}, effort {EFFORT}, cap ${s.ledger.cap:.3f}, "
          f"per-call bound ${s.bound:.4f}", flush=True)
    if not args.dry_run:
        s.key = env("META_API_KEY")
        if not s.key:
            print("META_API_KEY is not set")
            return 1
    disc = None
    if args.project and args.phase == "features":
        _, fields, controls = load_project(args.project)
        disc = preflight.Discovery.from_records(fields, controls)
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        s.client = client
        if args.phase == "features":
            await phase_features(s, disc)
        elif args.phase == "preflight":
            await phase_preflight(s, args.project)
        elif args.phase == "frontier":
            await phase_frontier(s)
        elif args.phase == "refine":
            await phase_refine(s, args.project)
        else:
            await phase_stretch(s)
    if args.dry_run:
        print("\n".join(s.planned))
        print(f"{len(s.planned)} calls planned; worst case ${len(s.planned) * s.bound:.4f} against the "
              f"${s.ledger.cap:.3f} cap (the cap stops the phase first)")
    else:
        print(f"{s.ledger.calls} calls, ${s.ledger.spent:.5f} billed or bounded; raw calls: {rec.path if rec else '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
