"""Repeatability probe: is cache reuse stable inside one session, and does prompt_cache_key pin it?

Each block sends one fresh body repeatedly. The nonce changes only the trailing user text, so both
blocks have the same 26.4k packet and readers schema. The unkeyed block and the keyed block differ
only by `prompt_cache_key` and their nonce.

Pre-registered hypothesis: a stable `prompt_cache_key` gives routing affinity, so the keyed block
hits on at least 6 of its 7 repeats after the first, while the unkeyed block hits less often.

    uv run --extra probe python scripts/probes/probe_cache_repeat.py --dry-run
    uv run --extra probe python scripts/probes/probe_cache_repeat.py --yes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
import probe_cache as pc  # noqa: E402
from common import Recorder, env, run_dir  # noqa: E402

BUDGET_USD = 0.06
CALL_BOUND_USD = 0.0036
KEY = "upa-cache-repeat"
HYPOTHESIS = (
    "A stable prompt_cache_key gives routing affinity: the keyed block hits on at least 6 of 7 "
    "repeats after the first, while the unkeyed block hits less often. If both blocks hit "
    "consistently, the key is unnecessary. If both miss consistently, the provider cache is not "
    "repeatable in this session."
)


def fresh_body(shared: str, sch: dict, nonce: str, key: str | None) -> dict:
    body = pc.big_body(shared, sch["readers"])
    body["messages"][1]["content"] += f"\n\nRun tag: {nonce}."
    if key:
        body["prompt_cache_key"] = key
    return body


def block_result(label: str, rows: list[dict]) -> dict:
    known = [r for r in rows if isinstance(r.get("cached_tokens"), int)]
    cached = [r["cached_tokens"] for r in known]
    repeats = cached[1:]
    hits = [c for c in repeats if c > 0]
    best_run = run = 0
    for c in repeats:
        run = run + 1 if c > 0 else 0
        best_run = max(best_run, run)
    return {
        "label": label,
        "calls": len(rows),
        "cached": cached,
        "repeat_hits": len(hits),
        "repeat_total": len(repeats),
        "best_consecutive_hit_run": best_run,
        "first_cached": cached[0] if cached else None,
        "cost_usd": round(sum(r.get("est_cost_usd") or 0 for r in rows), 6),
    }


async def run_block(client: httpx.AsyncClient, api_key: str, rec: Recorder, label: str, body: dict,
                    schema: dict, field_ids: set[str], repeats: int, spent: float) -> tuple[list[dict], float]:
    rows: list[dict] = []
    for i in range(repeats):
        if spent + CALL_BOUND_USD > BUDGET_USD:
            print(f"{label}: stopping at {i} calls; planning bound would exceed ${BUDGET_USD:.2f}")
            break
        row, cost, status = await pc.call(client, api_key, body, schema, rec, f"{label}/{i + 1}",
                                          field_ids=field_ids, attempt=i + 1)
        rows.append(row)
        spent += cost or 0
        if row.get("problem") is not None:
            print(f"{label}: stopping after failure: {row['problem']}")
            break
    return rows, spent


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    shared, fields, controls, sch = pc.project_packet(pc.PROJECT)
    field_ids = {f["id"] for f in fields}
    nonce = uuid.uuid4().hex[:8]
    plain = fresh_body(shared, sch, nonce + "-plain", None)
    keyed = fresh_body(shared, sch, nonce + "-keyed", KEY)
    print(f"repeatability: {args.repeats} unkeyed + {args.repeats} keyed calls, worst case "
          f"${2 * args.repeats * CALL_BOUND_USD:.4f}; cap ${BUDGET_USD:.2f}")
    print(f"  unkeyed body {pc.digest(plain['messages'])[:12]}  key=-")
    print(f"  keyed   body {pc.digest(keyed['messages'])[:12]}  key={KEY}")
    print(f"  hypothesis: {HYPOTHESIS}")
    if args.dry_run or not args.yes:
        print("\ndry run: no call, no key read" if args.dry_run else "\npass --yes to spend")
        return 0
    api_key = env("META_API_KEY")
    if not api_key:
        print("META_API_KEY is not set")
        return 1
    out = run_dir("cache-repeat")
    rec = Recorder(out)
    spent = 0.0
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        unkeyed_rows, spent = await run_block(client, api_key, rec, "unkeyed", plain, sch["readers"],
                                             field_ids, args.repeats, spent)
        keyed_rows, spent = await run_block(client, api_key, rec, "keyed", keyed, sch["readers"],
                                            field_ids, args.repeats, spent)
    unkeyed = block_result("unkeyed", unkeyed_rows)
    keyed_res = block_result("keyed", keyed_rows)
    if keyed_res["repeat_hits"] >= 6 and unkeyed["repeat_hits"] <= 3:
        finding = "Keyed repeats were consistent while unkeyed repeats were not; routing affinity is supported."
    elif unkeyed["repeat_hits"] >= 6 and keyed_res["repeat_hits"] >= 6:
        finding = "Both blocks repeated consistently; the key is not required in this session."
    elif unkeyed["repeat_hits"] < 6 and keyed_res["repeat_hits"] < 6:
        finding = "Neither block repeated consistently; the provider cache is not repeatable in this session."
    else:
        finding = "Mixed result; neither affinity nor general repeatability is established."
    result = {"hypothesis": HYPOTHESIS, "unkeyed": unkeyed, "keyed": keyed_res,
              "finding": finding, "total_cost_usd": round(spent, 6), "raw": str(rec.path)}
    (rec.path.parent / "summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"\nunkeyed cached={unkeyed['cached']} hits={unkeyed['repeat_hits']}/{unkeyed['repeat_total']} "
          f"best_run={unkeyed['best_consecutive_hit_run']}")
    print(f"keyed   cached={keyed_res['cached']} hits={keyed_res['repeat_hits']}/{keyed_res['repeat_total']} "
          f"best_run={keyed_res['best_consecutive_hit_run']}")
    print(f"\ntotal ${spent:.4f}\nfinding: {finding}\nsummary: {rec.path.parent / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
