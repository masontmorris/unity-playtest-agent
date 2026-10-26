"""Expanded preflight probe: stages 4-6 chained over a whole Unity project's gameplay code.

Discovery stand-ins (code, no model):
  fields    every field declared in the gameplay scripts (regex; stage 1 will use the Editor)
  controls  every serialized InputAction in prefabs and scenes, plus .inputactions JSON assets

Model stages, chained as the real preflight will be:
  4 readers           choose fields to observe              (ids restricted to discovered fields)
  5 actions + skills  compose actions from discovered controls only, and bounded skills
  6 progress/invariants/gaps, given the stage 4 and 5 answers

Every stage sends the same system prompt and the same code block first, with the stage's
instructions last. That does not buy a cache hit: Muse reuses only an identical request body,
so stages 5 and 6 each pay for the whole project (#70). The chain runs at reasoning_effort
minimal and low; stage 4 also runs once on Standard mode for comparison.

    uv run --extra probe python scripts/probes/probe_preflight_full.py <exported-project-dir> [--model M]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
import jsonschema

sys.path.insert(0, str(Path(__file__).parent))
from common import Recorder, env, run_dir, timed  # noqa: E402
from probe_preflight import discover  # noqa: E402

URL = "https://api.meta.ai/v1/chat/completions"
PRICES = {  # USD per 1M tokens (input, cached, output), 26 Oct 2026
    "muse-spark-1.3": (1.25, 0.15, 4.25),
    "muse-spark-1.3-contributor": (0.10, 0.002, 0.20),
}

ACTION_BLOCK = re.compile(r"^(\s*)(\w+):\n\1  m_Name: (.+)\n\1  m_Type: \d+\n\1  m_ExpectedControlType: (\w*)", re.M)
BINDING = re.compile(r"- m_Name: ?(.*)\n\s+m_Id: .*\n\s+m_Path: (.+)\n(?:\s+m_\w+: .*\n)*?\s+m_Action: (.+)")


def discover_controls(root: Path) -> list[dict]:
    controls: dict[str, dict] = {}
    for path in sorted([*root.rglob("*.prefab"), *root.rglob("*.unity")]):
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in ACTION_BLOCK.finditer(text):
            name = m.group(3).strip()
            controls.setdefault(name, {"action": name, "field": m.group(2), "control_type": m.group(4) or "Button",
                                       "bindings": [], "source": str(path.relative_to(root))})
        for b in BINDING.finditer(text):
            action, part, bind = b.group(3).strip(), b.group(1).strip(), b.group(2).strip()
            if action in controls and not bind.endswith("Vector") and bind != "2DVector":
                label = f"{bind} ({part})" if part else bind
                if label not in controls[action]["bindings"]:
                    controls[action]["bindings"].append(label)
    for path in sorted(root.rglob("*.inputactions")):
        for amap in json.loads(path.read_text(encoding="utf-8-sig")).get("maps", []):
            for a in amap.get("actions", []):
                name = f"{amap.get('name')}/{a['name']}"
                controls[name] = {"action": name, "field": None, "control_type": a.get("expectedControlType") or "Button",
                                  "bindings": [b["path"] for b in amap.get("bindings", []) if b.get("action") == a["name"]],
                                  "source": str(path.relative_to(root))}
    return [{"id": f"c{i + 1}", **c} for i, c in enumerate(controls.values())]


def closed(props: dict, req: list[str] | None = None) -> dict:
    return {"type": "object", "additionalProperties": False, "required": req or list(props), "properties": props}


def schemas(field_ids: list[str], control_ids: list[str]) -> dict[str, dict]:
    f = {"type": "string", "enum": field_ids}
    c = {"type": "string", "enum": control_ids}
    s = {"type": "string"}
    step = closed({"control_id": c, "kind": {"type": "string", "enum": ["press", "hold", "axis_hold"]},
                   "x": {"type": "number"}, "y": {"type": "number"}, "hold_s": {"type": "number"}})
    return {
        "readers": closed({"readers": {"type": "array", "items": closed({"field_id": f, "alias": s, "why": s})},
                           "gaps": {"type": "array", "items": s}}),
        "actions": closed({
            "actions": {"type": "array", "items": closed({"name": s, **step["properties"], "expected_effect": s})},
            "skills": {"type": "array", "items": closed({"name": s, "purpose": s,
                                                          "steps": {"type": "array", "items": step},
                                                          "max_s": {"type": "number"}})},
            "gaps": {"type": "array", "items": s}}),
        "progress": closed({
            "progress_fields": {"type": "array", "items": f},
            "invariants": {"type": "array", "items": closed({
                "text": s, "field_ids": {"type": "array", "items": f},
                "kind": {"type": "string", "enum": ["never_decreases", "never_increases", "bounded", "implies", "stable_unless"]}})},
            "gaps": {"type": "array", "items": s}}),
    }


SYSTEM = (
    "You are the preflight analyst for an autonomous game playtester. Code has already discovered the game's "
    "fields and input controls; you choose from them and never invent ids, fields, controls or code. Answers "
    "must match the JSON schema exactly. The stage instructions come at the end of the user message."
)

STAGES = {
    "readers": "Stage 4. Choose up to 16 fields worth reading on every observation: runtime state that changes "
               "during play and that a player could notice. Prefer runtime state over configuration. List gaps: "
               "state a playtester needs that these fields do not cover.",
    "actions": "Stage 5. Compose up to 14 actions from the discovered controls only: name, control, kind, axis "
               "values in [-1, 1] (x=1, y=0 for buttons), hold time in seconds, and the effect you expect. Then "
               "up to 6 skills: short bounded sequences of those steps, such as a recovery or reaching a menu. "
               "List gaps: things a playtester must do that no control supports.",
    "progress": "Stage 6. Given the chosen readers and actions below, choose the fields that track progress, "
                "invariants that should hold in normal play (each over chosen or discovered fields), and gaps "
                "that would stop a playtester from judging success or noticing a bug.",
}


def price(model: str, u: dict) -> float | None:
    pin, pcached, pout = PRICES.get(model, (None, None, None))
    try:
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        return ((u["prompt_tokens"] - cached) * pin + cached * pcached + u["completion_tokens"] * pout) / 1e6
    except (KeyError, TypeError):
        return None


async def call(client, key, model, effort, shared, stage, extra, schema, rec, label):
    body = {"model": model, "max_tokens": 32000, "reasoning_effort": effort,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": shared + extra + "\n\n=== INSTRUCTIONS ===\n" + STAGES[stage]}],
            "response_format": {"type": "json_schema", "json_schema": {"name": f"stage_{stage}", "strict": True, "schema": schema}}}
    t0 = timed()
    r = await client.post(URL, json=body, headers={"Authorization": f"Bearer {key}"})
    ms = (timed() - t0) * 1000
    data = r.json() if "json" in r.headers.get("content-type", "") else {}
    problem, parsed = (None if r.status_code == 200 else f"HTTP {r.status_code}: {r.text[:300]}"), None
    if problem is None:
        try:
            parsed = json.loads(data["choices"][0]["message"]["content"])
            jsonschema.validate(parsed, schema)
        except (KeyError, TypeError, ValueError, jsonschema.ValidationError) as exc:
            problem = f"invalid structured output: {str(exc)[:200]}"
    u = data.get("usage") or {}
    c = price(model, u)
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    rec.write({"label": label, "model": model, "resolved_model": data.get("model"), "effort": effort, "stage": stage,
               "status": r.status_code, "latency_ms": round(ms, 1), "problem": problem, "usage": u,
               "est_cost_usd": c, "output": parsed})
    print(f"{label:28} {r.status_code} {ms/1000:5.1f} s  in={u.get('prompt_tokens')} cached={cached} "
          f"out={u.get('completion_tokens')}  ${round(c or 0, 4)}  {problem or 'ok'}", flush=True)
    return parsed, c or 0, r.status_code


def show(stage, out, fields, controls):
    if not out:
        return
    fid = {f["id"]: f"{f['component']}.{f['field']}" for f in fields}
    cid = {c["id"]: c["action"] for c in controls}
    if stage == "readers":
        print("    readers:", ", ".join(fid[r["field_id"]] for r in out["readers"]))
    elif stage == "actions":
        print("    actions:", "; ".join(f"{a['name']}={cid[a['control_id']]}({a['x']},{a['y']}) {a['hold_s']}s" for a in out["actions"]))
        print("    skills:", "; ".join(f"{s['name']} [{len(s['steps'])} steps]" for s in out["skills"]))
    else:
        print("    progress:", ", ".join(fid[i] for i in out["progress_fields"]))
        print("    invariants:", " | ".join(i["text"][:70] for i in out["invariants"]))
    for g in out["gaps"]:
        print(f"    gap: {g[:110]}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--model", default="muse-spark-1.3-contributor")
    args = ap.parse_args()
    root = Path(args.project)
    scripts = sorted(str(p.relative_to(root)) for p in root.rglob("*.cs") if "Editor" not in p.parts)
    fields, code = discover(root, scripts)
    controls = discover_controls(root)
    print(f"{len(scripts)} scripts, {len(fields)} fields, {len(controls)} controls "
          f"({sum(len(c['bindings']) for c in controls)} bindings), code {len(code):,} chars")
    sch = schemas([f["id"] for f in fields], [c["id"] for c in controls])
    shared = (json.dumps({"discovered_fields": fields, "discovered_controls": controls}) + "\n\nSource:\n" + code)
    key = env("META_API_KEY")
    rec = Recorder(run_dir("preflight-full"))
    (rec.path.parent / "discovery.json").write_text(json.dumps({"fields": fields, "controls": controls}, indent=1), encoding="utf-8")
    total = 0.0
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        for effort in ("minimal", "low"):
            prior = ""
            for stage in ("readers", "actions", "progress"):
                extra = prior if stage == "progress" else ""
                out, c, status = await call(client, key, args.model, effort, shared, stage, extra, sch[stage],
                                            rec, f"{args.model.split('-')[-1]}/{effort}/{stage}")
                total += c
                show(stage, out, fields, controls)
                if status >= 400:
                    return 1
                if out and stage in ("readers", "actions"):
                    prior += f"\n\n=== STAGE {stage.upper()} ANSWER ===\n" + json.dumps(out)
        if args.model != "muse-spark-1.3":
            out, c, _ = await call(client, key, "muse-spark-1.3", "minimal", shared, "readers", "", sch["readers"],
                                   rec, "standard/minimal/readers")
            total += c
            show("readers", out, fields, controls)
    print(f"estimated total ${total:.4f}; raw calls: {rec.path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
