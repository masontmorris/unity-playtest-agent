"""Controlled prefix-cache test at preflight size (#70).

The #68 preflight chain reported `cached_tokens: 0` on seven calls that repeated a
byte-identical 26,401-token prefix, while the 1.7k Day-1 packet cached 1,649 tokens.
This probe separates the three candidate causes with eight calls on
`muse-spark-1.3-contributor` at `reasoning_effort: minimal`:

  C  1.7k Day-1 packet, sent twice          positive control: caching works today
  B  26.4k FPSKit packet with the key, x2   does `prompt_cache_key` break it?
  A  26.4k FPSKit packet without the key, x2 does size allow caching at all?
  A2 26.4k packet with one trailing sentence  does a differing tail still reuse the prefix?
  A3 26.4k packet with a renamed json_schema  is the schema part of the cached prefix?

A2 and A3 are single calls issued straight after A's second call, so they measure reuse of
the prefix A just wrote; they are not pairs of their own. B runs before A so that a hit in B
cannot come from A's warm-up.

Estimated cost before the run: about 109k input and 8k output tokens, roughly $0.013 at
contributor rates ($0.10 / $0.002 cached / $0.20 per 1M). It refuses to spend without --yes.

`--phase2` re-checks the two single-call misses and asks the same tail question on the 1.7k
packet, to tell a size ceiling from identity-only caching. Worst case about $0.012 more.

    uv run --extra probe python scripts/probes/probe_cache.py --dry-run
    uv run --extra probe python scripts/probes/probe_cache.py --yes
    uv run --extra probe python scripts/probes/probe_cache.py --phase2 --yes
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jsonschema

sys.path.insert(0, str(Path(__file__).parent))
from common import Recorder, env, run_dir, timed  # noqa: E402
from packets import FRONTIER_SCHEMA, FRONTIER_SYSTEM, frontier_user  # noqa: E402
from probe_preflight import discover  # noqa: E402
from probe_preflight_full import PRICES, STAGES, SYSTEM, discover_controls, price, schemas  # noqa: E402

URL = "https://api.meta.ai/v1/chat/completions"
MODEL = "muse-spark-1.3-contributor"
EFFORT = "minimal"
CACHE_KEY = "upa-preflight"
PROJECT = Path(__file__).resolve().parents[2] / "runs" / "probes" / "fpskit-8965ad4-full"

# Measured prompt sizes from #68 (readers stage) and the Day-1 packet, for the estimate only.
EST_IN = {"C": 1715, "B": 26401, "A": 26401, "A2": 26409, "A3": 26401}
EST_OUT = 1000


def digest(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def project_packet(root: Path) -> tuple[str, list[dict], list[dict], dict]:
    """Rebuild the exact prefix #68 sent: discovery JSON, then the source, then instructions."""
    scripts = sorted(str(p.relative_to(root)) for p in root.rglob("*.cs") if "Editor" not in p.parts)
    fields, code = discover(root, scripts)
    controls = discover_controls(root)
    shared = json.dumps({"discovered_fields": fields, "discovered_controls": controls}) + "\n\nSource:\n" + code
    return shared, fields, controls, schemas([f["id"] for f in fields], [c["id"] for c in controls])


def big_body(shared: str, schema: dict, *, tail: str = "", schema_name: str = "stage_readers",
             key: str | None = None) -> dict:
    body = {"model": MODEL, "max_tokens": 32000, "reasoning_effort": EFFORT,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": shared + "\n\n=== INSTRUCTIONS ===\n" + STAGES["readers"] + tail}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": schema_name, "strict": True, "schema": schema}}}
    if key:
        body["prompt_cache_key"] = key
    return body


def small_body(tail: str = "") -> dict:
    """The Day-1 positive control, byte-identical in shape to probe_meta.py's `small` call."""
    return {"model": MODEL, "max_tokens": 16000, "reasoning_effort": EFFORT,
            "messages": [{"role": "system", "content": FRONTIER_SYSTEM},
                         {"role": "user", "content": frontier_user(8) + tail}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "FrontierResponse", "strict": True, "schema": FRONTIER_SCHEMA}}}


def estimated_cost() -> float:
    pin, _, pout = PRICES[MODEL]
    return (sum(EST_IN.values()) * pin + len(EST_IN) * EST_OUT * pout) / 1e6


async def call(client: httpx.AsyncClient, api_key: str, body: dict, schema: dict, rec: Recorder,
               label: str) -> tuple[dict, float, int | None]:
    t0 = timed()
    r = await client.post(URL, json=body, headers={"Authorization": f"Bearer {api_key}"})
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
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    body_sha = digest({k: v for k, v in body.items() if k != "prompt_cache_key"})
    cost = price(MODEL, u)
    row = {"label": label, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "model": MODEL, "resolved_model": data.get("model"), "effort": EFFORT, "status": r.status_code,
           "latency_ms": round(ms, 1), "problem": problem, "usage": u, "cached_tokens": cached,
           "cached_fraction": round(cached / u["prompt_tokens"], 3) if cached and u.get("prompt_tokens") else None,
           "est_cost_usd": cost, "prompt_cache_key": body.get("prompt_cache_key"),
           "messages_sha256": digest(body["messages"]), "format_sha256": digest(body["response_format"]),
           "body_sha256": body_sha, "request": body, "output": parsed}
    rec.write(row)
    print(f"{label:14} {r.status_code} {ms/1000:6.1f} s  in={u.get('prompt_tokens')} cached={cached} "
          f"out={u.get('completion_tokens')}  ${round(cost or 0, 4)}  {problem or 'ok'}", flush=True)
    return row, cost or 0, r.status_code


def cached_of(rows: dict, pair: str) -> int | None:
    row = rows.get(pair)
    return None if row is None else row["cached_tokens"]


def verdict(rows: dict) -> str:
    c, b, a, a2, a3 = (cached_of(rows, p) for p in ("C", "B", "A", "A2", "A3"))
    if None in (c, b, a, a2, a3):
        return "Incomplete: a call is missing or failed; draw no conclusion."
    control = "" if c else " (the 1.7k control also missed in this session)"
    if a == 0 and b == 0:
        return ("26.4k misses with and without the key: size (or packet shape) gates the cache" + control +
                ". Keep the no-cache budget ($0.119 Standard) and let #28 assume no provider cache.")
    if a == 0 and b > 0:
        return ("26.4k caches only with the key (unexpected): keep prompt_cache_key and investigate further.")
    if a > 0 and b == 0:
        return ("The key suppresses the cache: drop prompt_cache_key from the preflight probe, and #28 may "
                "rely on stages 5-6 reusing stage 4's prefix" + control + ".")
    if a2 == 0:
        return ("Identical requests reuse at 26.4k, a differing tail does not: the staged chain cannot rely on "
                "the provider cache. Keep the no-cache budget" + control + ".")
    if a3 == 0:
        return ("A differing tail reuses the prefix but a renamed json_schema does not: schema identity is part "
                "of the prefix. Share one schema across stages before relying on the cache" + control + ".")
    return ("Prefix reuse survives a differing tail and a renamed schema: stages 5-6 can reuse stage 4's prefix. "
            "The cached projection applies and #28 may build on it" + control + ".")


def markdown(rows: dict) -> str:
    lines = ["| Call | Variant | Key | Input (cached) | Cached share | Latency | Est. cost |",
             "|---|---|---|---|---|---|---|"]
    names = {"C": "1.7k control", "B": "26.4k with key", "A": "26.4k no key",
             "A2": "26.4k, different tail", "A3": "26.4k, renamed schema"}
    for pair in ("C", "B", "A", "A2", "A3"):
        row = rows.get(pair)
        if row is None:
            continue
        u = row["usage"]
        share = f"{row['cached_fraction']:.0%}" if row["cached_fraction"] is not None else "-"
        key = row["prompt_cache_key"] or "-"
        lines.append(f"| {pair} | {names[pair]} | `{key}` | {u.get('prompt_tokens'):,} ({row['cached_tokens']}) "
                     f"| {share} | {row['latency_ms']/1000:.1f} s | ${row['est_cost_usd']:.4f} |")
    return "\n".join(lines)


TAIL = "\n\nNote: keep the answer compact."


async def phase2(api_key: str, shared: str, sch: dict) -> int:
    """Re-check the two misses and ask the same tail question at 1.7k, inside the approved budget."""
    plan = [
        ("C", "1.7k plain", small_body()),
        ("C2", "1.7k different tail", small_body(tail=TAIL)),
        ("A", "26.4k plain (warm)", big_body(shared, sch["readers"])),
        ("A2", "26.4k different tail (recheck)", big_body(shared, sch["readers"], tail=TAIL)),
        ("A", "26.4k plain (warm)", big_body(shared, sch["readers"])),
        ("A3", "26.4k renamed schema (recheck)", big_body(shared, sch["readers"], schema_name="stage_readers_alt")),
    ]
    out = run_dir("cache-phase2")
    rec = Recorder(out)
    results: list[tuple[str, str, dict]] = []
    total = 0.0
    print(f"phase 2: {len(plan)} calls, worst case $0.0121 (warm hits cost less)\n")
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        for label, name, body in plan:
            schema = FRONTIER_SCHEMA if label.startswith("C") else sch["readers"]
            row, cost, status = await call(client, api_key, body, schema, rec, f"{label}/{name}")
            total += cost
            results.append((label, name, row))
            if status and 400 <= status < 500:
                print("client error repeats on every call; stopping")
                break
    by = {label: row for label, _, row in results}
    c2, a2, a3 = (by.get(k, {}).get("cached_tokens") for k in ("C2", "A2", "A3"))
    if None in (c2, a2, a3):
        finding = "Incomplete: a call is missing or failed; draw no conclusion."
    elif c2:
        finding = ("A tail change still reuses the prefix at 1.7k but not at 26.4k: reuse depends on prompt size, "
                   "not only on request identity.")
    else:
        finding = ("A tail change misses at both sizes: the cache is request-identity shaped, not a reusable text "
                   "prefix.")
    finding += f" Recheck on identical prefix: 26.4k tail change {a2} cached, renamed schema {a3} cached."
    lines = ["| Call | Variant | Input (cached) | Latency | Cost |", "|---|---|---|---|---|"]
    for label, name, row in results:
        u = row["usage"]
        lines.append(f"| {label} | {name} | {u.get('prompt_tokens'):,} ({row['cached_tokens']}) | "
                     f"{row['latency_ms']/1000:.1f} s | ${row['est_cost_usd']:.4f} |")
    table = "\n".join(lines)
    result = {"calls": [{k: v for k, v in r.items() if k != "request"} for _, _, r in results],
              "finding": finding, "total_cost_usd": round(total, 4), "raw": str(rec.path),
              "table_markdown": table}
    (rec.path.parent / "summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"\ntotal ${total:.4f}\n{table}\n\nfinding: {finding}")
    print(f"summary: {rec.path.parent / 'summary.json'}")
    return 0


async def phase3(api_key: str, shared: str, sch: dict) -> int:
    """A never-sent body with a warm 26.4k prefix, then the same body repeated.

    If the first misses and the repeat hits, caching is bound to the request body and a new
    stage can never reuse the previous stage's prefix. If the first hits, prefix reuse works.
    """
    fresh = big_body(shared, sch["readers"], tail="\n\nAnswer with at most eight readers.")
    plan = [
        ("A", "26.4k plain (warm)", big_body(shared, sch["readers"])),
        ("A4/1", "26.4k new tail, never sent", fresh),
        ("A4/2", "26.4k new tail, repeated", fresh),
    ]
    out = run_dir("cache-phase3")
    rec = Recorder(out)
    results: list[tuple[str, str, dict]] = []
    total = 0.0
    print(f"phase 3: {len(plan)} calls, worst case $0.0034\n")
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        for label, name, body in plan:
            row, cost, status = await call(client, api_key, body, sch["readers"], rec, f"{label}/{name}")
            total += cost
            results.append((label, name, row))
            if status and 400 <= status < 500:
                break
    by = {label: row for label, _, row in results}
    warm, first, repeat = (by.get(k, {}).get("cached_tokens") for k in ("A", "A4/1", "A4/2"))
    if None in (warm, first, repeat):
        finding = "Incomplete: a call is missing or failed."
    elif warm == 0:
        finding = "Inconclusive: the warm plain call missed, so the cache was cold before the new body."
    elif first and repeat:
        finding = ("A never-sent body reused the 26.4k prefix: prefix reuse across different bodies works, and "
                   "the phase-1/phase-2 difference is flaky routing or timing.")
    elif first == 0 and repeat:
        finding = ("A never-sent body missed even with a warm 26k prefix, and its own repeat hit: caching is bound "
                   "to the request body, so a new stage cannot reuse the previous stage's prefix.")
    else:
        finding = "Neither the new body nor its repeat hit: caching is not reliable in this window."
    lines = ["| Call | Variant | Input (cached) | Latency | Cost |", "|---|---|---|---|---|"]
    for label, name, row in results:
        lines.append(f"| {label} | {name} | {row['usage'].get('prompt_tokens'):,} ({row['cached_tokens']}) | "
                     f"{row['latency_ms']/1000:.1f} s | ${row['est_cost_usd']:.4f} |")
    table = "\n".join(lines)
    result = {"calls": [{k: v for k, v in r.items() if k != "request"} for _, _, r in results],
              "finding": finding, "total_cost_usd": round(total, 4), "raw": str(rec.path),
              "table_markdown": table}
    (rec.path.parent / "summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"\ntotal ${total:.4f}\n{table}\n\nfinding: {finding}")
    print(f"summary: {rec.path.parent / 'summary.json'}")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the plan and hashes, spend nothing")
    ap.add_argument("--phase2", action="store_true", help="re-check the misses and the 1.7k tail question")
    ap.add_argument("--phase3", action="store_true", help="never-sent body with a warm prefix, then repeated")
    ap.add_argument("--yes", action="store_true", help="confirm the estimated cost and make the calls")
    args = ap.parse_args()
    if not PROJECT.is_dir():
        print(f"missing packet source: {PROJECT}")
        return 2
    shared, fields, controls, sch = project_packet(PROJECT)
    if args.phase2:
        if not args.yes:
            print("phase 2 spends money: pass --yes")
            return 0
        api_key = env("META_API_KEY")
        if not api_key:
            print("META_API_KEY is not set")
            return 1
        return await phase2(api_key, shared, sch)
    if args.phase3:
        if not args.yes:
            print("phase 3 spends money: pass --yes")
            return 0
        api_key = env("META_API_KEY")
        if not api_key:
            print("META_API_KEY is not set")
            return 1
        return await phase3(api_key, shared, sch)
    plan = [
        ("C", "1.7k control", small_body()),
        ("B", "26.4k with key", big_body(shared, sch["readers"], key=CACHE_KEY)),
        ("A", "26.4k no key", big_body(shared, sch["readers"])),
        ("A2", "26.4k different tail", big_body(shared, sch["readers"], tail=TAIL)),
        ("A3", "26.4k renamed schema", big_body(shared, sch["readers"], schema_name="stage_readers_alt")),
    ]
    print(f"{len(fields)} fields, {len(controls)} controls, shared prefix {len(shared):,} chars, "
          f"estimated cost ${estimated_cost():.4f} for 8 calls")
    for pair, name, body in plan:
        print(f"  {pair:2} {name:22} messages {digest(body['messages'])[:12]}  format "
              f"{digest(body['response_format'])[:12]}  key={body.get('prompt_cache_key') or '-'}")
    if args.dry_run or not args.yes:
        print("\ndry run: pass --yes to spend" if args.dry_run else "\nrefusing to spend without --yes")
        return 0
    api_key = env("META_API_KEY")
    if not api_key:
        print("META_API_KEY is not set")
        return 1
    out = run_dir("cache")
    rec = Recorder(out)
    rows: dict[str, dict] = {}
    total = 0.0
    print()
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        for pair, name, body in plan:
            schema = FRONTIER_SCHEMA if pair == "C" else sch["readers"]
            repeats = 2 if pair in ("C", "B", "A") else 1
            for i in range(repeats):
                row, cost, status = await call(client, api_key, body, schema, rec,
                                               f"{pair}/{name}/{i + 1}")
                total += cost
                rows[pair] = row  # a pair keeps its final call, which is the measured one
                if status and 400 <= status < 500:
                    print("client error repeats on every call; stopping instead of paying for it")
                    break
            if rows.get(pair) is None or (rows[pair]["status"] or 0) >= 400:
                break
    result = {"pairs": {k: {kk: vv for kk, vv in v.items() if kk != "request"} for k, v in rows.items()},
              "verdict": verdict(rows), "total_cost_usd": round(total, 4), "raw": str(rec.path),
              "table_markdown": markdown(rows)}
    (rec.path.parent / "summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"\ntotal ${total:.4f}\n{result['table_markdown']}\n\ndecision: {result['verdict']}")
    print(f"summary: {rec.path.parent / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
