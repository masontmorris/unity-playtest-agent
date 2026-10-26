"""Day-1 preflight probe (#12): can Muse Spark 1.3 make a stage-4/6 choice over real game code?

It reads a few gameplay scripts from a local Unity project (never copied into this repo), builds
a candidate field list with a crude regex standing in for stage-1 discovery, and asks the model
to choose readers, progress fields, invariants and gaps. The schema restricts every field
reference to the discovered ids, so the model chooses from what code found and invents nothing.

Two calls (reasoning_effort minimal, then low), estimated under $0.04.

    uv run --extra probe python scripts/probes/probe_preflight.py <unity-project> <script.cs>...
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
import jsonschema

sys.path.insert(0, str(Path(__file__).parent))
from common import Recorder, env, run_dir, timed  # noqa: E402
from probe_meta import MODEL, URL, cost  # noqa: E402

FIELD = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)*(public|protected|private|internal)?\s*(?:static\s+|readonly\s+)*"
    r"(bool|int|float|double|string|Vector2|Vector3|Quaternion|[A-Z]\w*)\s+(m_\w+|[A-Za-z_]\w*)\s*(=[^;]*)?;",
    re.M)


def discover(root: Path, files: list[str]) -> tuple[list[dict], str]:
    fields, excerpts = [], []
    for rel in files:
        src = (root / rel).read_text(encoding="utf-8-sig")
        cls = (re.search(r"class\s+(\w+)", src) or [None, Path(rel).stem])[1]
        for m in FIELD.finditer(src):
            access, typ, name = m.group(1) or "private", m.group(2), m.group(3)
            if typ in ("return", "using", "new", "var"):
                continue
            serialized = "[SerializeField" in src[max(0, m.start() - 120):m.start()] or access == "public"
            fields.append({"id": f"f{len(fields) + 1}", "component": cls, "field": name, "type": typ,
                           "serialized": serialized, "runtime_writes": "unknown"})
        excerpts.append(f"// ===== {rel} =====\n{src}")
    return fields, "\n\n".join(excerpts)


def schema(ids: list[str]) -> dict:
    ref = {"type": "string", "enum": ids}
    closed = lambda props, req: {"type": "object", "additionalProperties": False,  # noqa: E731
                                 "required": req, "properties": props}
    return closed({
        "readers": {"type": "array", "items": closed(
            {"field_id": ref, "alias": {"type": "string"}, "why": {"type": "string"}}, ["field_id", "alias", "why"])},
        "progress_fields": {"type": "array", "items": ref},
        "invariants": {"type": "array", "items": closed(
            {"text": {"type": "string"}, "field_ids": {"type": "array", "items": ref},
             "kind": {"type": "string", "enum": ["never_decreases", "never_increases", "bounded", "implies", "stable_unless"]}},
            ["text", "field_ids", "kind"])},
        "gaps": {"type": "array", "items": {"type": "string"}},
    }, ["readers", "progress_fields", "invariants", "gaps"])


SYSTEM = (
    "You are the preflight analyst for an autonomous game playtester. Code has discovered the fields below in "
    "the game's scripts. Choose up to 12 fields worth reading every observation (state that changes in play and "
    "that a player could notice), the subset that tracks progress, invariants that should hold during normal "
    "play, and gaps: things a playtester would need to see or do that these fields do not cover. Use only the "
    "listed field ids. Prefer runtime state over configuration."
)


async def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    key = env("META_API_KEY")
    root, files = Path(sys.argv[1]), sys.argv[2:]
    fields, code = discover(root, files)
    ids = [f["id"] for f in fields]
    sch = schema(ids)
    user = json.dumps({"discovered_fields": fields}) + "\n\nSource excerpts:\n" + code
    out = run_dir("preflight")
    rec = Recorder(out)
    print(f"{len(fields)} candidate fields from {len(files)} files")
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
        for effort in ("minimal", "low"):
            body = {"model": MODEL, "max_tokens": 16000, "reasoning_effort": effort,
                    "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                    "response_format": {"type": "json_schema",
                                        "json_schema": {"name": "PreflightChoice", "strict": True, "schema": sch}}}
            t0 = timed()
            r = await client.post(URL, json=body, headers={"Authorization": f"Bearer {key}"})
            ms = (timed() - t0) * 1000
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            problem, parsed = (None if r.status_code == 200 else f"HTTP {r.status_code}: {r.text[:300]}"), None
            if problem is None:
                try:
                    parsed = json.loads(data["choices"][0]["message"]["content"])
                    jsonschema.validate(parsed, sch)
                except (KeyError, TypeError, ValueError, jsonschema.ValidationError) as exc:
                    problem = f"invalid structured output: {str(exc)[:200]}"
            u = data.get("usage") or {}
            rec.write({"effort": effort, "status": r.status_code, "latency_ms": round(ms, 1), "problem": problem,
                       "usage": u, "est_cost_usd": cost(u), "output": parsed, "fields": fields})
            print(f"{effort:8} {r.status_code} {ms/1000:5.1f} s in={u.get('prompt_tokens')} out={u.get('completion_tokens')} "
                  f"${round(cost(u) or 0, 4)} {problem or 'ok'}")
            if parsed:
                by_id = {f["id"]: f for f in fields}
                name = lambda i: f"{by_id[i]['component']}.{by_id[i]['field']}"  # noqa: E731
                print("  readers:", ", ".join(f"{name(x['field_id'])} as {x['alias']}" for x in parsed["readers"]))
                print("  progress:", ", ".join(name(i) for i in parsed["progress_fields"]))
                for inv in parsed["invariants"]:
                    print(f"  invariant [{inv['kind']}]: {inv['text'][:110]}")
                for g in parsed["gaps"]:
                    print(f"  gap: {g[:120]}")
            if r.status_code >= 400:
                break
    print(f"raw calls: {rec.path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
