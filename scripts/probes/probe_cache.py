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

A2 and A3 are single calls issued straight after A's second call, so they test whether a changed
tail or schema name reuses the preceding request; they are not pairs of their own. B runs before
A so that a hit in B cannot come from A's warm-up.

Estimated cost before the run: about 109k input and 8k output tokens, roughly $0.013 at
contributor rates ($0.10 / $0.002 cached / $0.20 per 1M). It refuses to spend without --yes.

`--phase2` re-checks the two single-call misses and their exact repeats, plus the same tail
question on the 1.7k packet. It records per-shape outcomes; it does not isolate a size ceiling,
request-identity rule or server cache mechanism. Worst case about $0.012 more.

`--turns` tests the append-only shape a harness uses. A is the plain 26.4k readers request.
T1 appends A's **actual** assistant `content` string and one user turn; T2 repeats T1's exact
JSON body; T3 appends T1's actual assistant `content` and a second user turn, only when T1 and
T2 both report a cache hit. It is capped at four calls and $0.02 incremental, with a $0.0033
per-call planning bound (29k input + 2k output), so four cold calls fit at about $0.0132.
`--dry-run` wins over `--yes` and makes no call. There is no retry, model switch or output
repair; a transport, HTTP, JSON, schema, reference, model or usage failure stops dependent
spend and is still recorded.

    uv run --extra probe python scripts/probes/probe_cache.py --dry-run
    uv run --extra probe python scripts/probes/probe_cache.py --yes
    uv run --extra probe python scripts/probes/probe_cache.py --phase2 --yes
    uv run --extra probe python scripts/probes/probe_cache.py --phase3 --yes
    uv run --extra probe python scripts/probes/probe_cache.py --turns --dry-run --yes
    uv run --extra probe python scripts/probes/probe_cache.py --turns --yes
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

# --turns envelope: four calls (A, T1, T2, T3), $0.02 incremental. The per-call bound uses
# 29k input (26.4k packet + an assistant turn + a user turn, rounded up) and 2k output, about
# 1.8x the largest historical completion (1,123). Four cold calls therefore plan to $0.0132.
TURNS_BUDGET_USD = 0.02
TURNS_MAX_ATTEMPTS = 4
TURNS_EST_IN = 29_000
TURNS_EST_OUT = 2_000
TURNS_CALL_BOUND_USD = (TURNS_EST_IN * PRICES[MODEL][0] + TURNS_EST_OUT * PRICES[MODEL][2]) / 1e6


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


# Historical only. The first --turns draft appended this constant placeholder instead of A's
# answer. It is kept so the old verifier and analysis remain readable; turns() does not use it.
TURN_ANSWER = json.dumps({"readers": [{"field_id": "f1", "alias": "first_field", "why": "placeholder"}],
                          "gaps": ["placeholder gap"]})
TURN_NEXT = ("Stage 4, second pass. The first pass is above. Choose a second, independent set of up to 16 fields "
             "worth reading, and say what the first pass missed.")
TURN_NEXT2 = ("Stage 4, third pass. The first two passes are above. Choose up to 16 fields worth reading that the "
              "first two passes did not cover, and list any remaining gaps.")


def appended(body: dict, *turns: dict) -> dict:
    out = json.loads(json.dumps(body))
    out["messages"] = [*out["messages"], *turns]
    return out


def assistant_turn(content: str) -> dict:
    """A turn carrying the exact assistant content string, not a re-serialized answer."""
    return {"role": "assistant", "content": content}


def readers_field_ids(schema: dict) -> set[str]:
    try:
        return set(schema["properties"]["readers"]["items"]["properties"]["field_id"]["enum"])
    except (KeyError, TypeError):
        return set()


def row_ok(row: dict | None) -> bool:
    return bool(row) and row.get("status") == 200 and row.get("problem") is None and row.get("output") is not None


def cache_known(row: dict | None) -> bool:
    return row_ok(row) and isinstance(row.get("cached_tokens"), int)


def cache_hit(row: dict | None) -> bool:
    return cache_known(row) and row["cached_tokens"] > 0


def cache_miss(row: dict | None) -> bool:
    return cache_known(row) and row["cached_tokens"] == 0


def add_cost(total: float | None, cost: float | None) -> float | None:
    return None if total is None or cost is None else total + cost


def fmt_usd(cost: float | None) -> str:
    return "unknown" if cost is None else f"${cost:.4f}"


def turns_table(rows: list[dict]) -> str:
    lines = ["| Call | Status | Input (cached) | Output | Latency | Cost | Problem |",
             "|---|---|---|---|---|---|---|"]
    for row in rows:
        u = row.get("usage") or {}
        prompt, cached, out = u.get("prompt_tokens"), row.get("cached_tokens"), u.get("completion_tokens")
        inp = f"{prompt:,} ({'unknown' if cached is None else cached})" if isinstance(prompt, int) else "unknown"
        output = f"{out:,}" if isinstance(out, int) else "unknown"
        problem = (row.get("problem") or "-").replace("|", "\\|")[:120]
        status = row.get("status") if row.get("status") is not None else "no response"
        lines.append(f"| {row['label']} | {status} | {inp} | {output} | "
                     f"{row.get('latency_ms', 0) / 1000:.1f} s | {fmt_usd(row.get('est_cost_usd'))} | {problem} |")
    return "\n".join(lines)


def skip_reason(skipped: list[dict], prefix: str) -> str:
    for item in skipped:
        if item["label"].startswith(prefix):
            return item["reason"]
    return "not attempted"


def cached_note(row: dict) -> str:
    usage = row.get("usage") or {}
    prompt = usage.get("prompt_tokens")
    fraction = row.get("cached_fraction")
    if isinstance(prompt, int) and isinstance(fraction, (int, float)):
        return f"{row['cached_tokens']:,} of {prompt:,} prompt tokens ({fraction:.1%})"
    return f"{row['cached_tokens']:,} cached tokens"


def turns_finding(rows: dict[str, dict], skipped: list[dict]) -> str:
    a, t1, t2, t3 = (rows.get(k) for k in ("A", "T1", "T2", "T3"))
    if not row_ok(a):
        return "Incomplete: A did not return a usable readers answer; no append inference."
    if not cache_known(a):
        return "Incomplete for caching: A returned no cached_tokens field; stopped before T1."
    a_note = ("A was already warm" if cache_hit(a) else "A was cold (0 cached), normal cache seeding")
    if t1 is None:
        return f"Incomplete: T1 was not attempted ({skip_reason(skipped, 'T1')}); no append inference."
    if not row_ok(t1):
        return "Incomplete: T1 failed; no append inference."
    if not cache_known(t1):
        return "Append outcome unknown: T1 returned no cached_tokens field; stopped before T2."
    if cache_hit(t1):
        if t2 is None:
            return (f"T1 reused {cached_note(t1)}, but the exact-repeat control T2 was not attempted "
                    f"({skip_reason(skipped, 'T2')}); the hit is not replicated.")
        if not row_ok(t2):
            return (f"T1 reused {cached_note(t1)}, but T2 failed; the hit is not replicated.")
        if not cache_known(t2):
            return (f"T1 reused {cached_note(t1)}, but T2 returned no cached_tokens; the hit is not replicated.")
        if cache_hit(t2):
            if t3 is None:
                return (f"In this session, appending A's actual answer and one user turn reused "
                        f"{cached_note(t1)} (T1), and the exact repeat reused {cached_note(t2)} (T2). {a_note}. "
                        f"T3 was skipped: {skip_reason(skipped, 'T3')}. This supports reuse for this append shape, "
                        f"schema, model and effort; it does not establish whole-body identity, a server end-marker "
                        f"rule, a cache lifetime, or a production three-stage chain.")
            if not row_ok(t3):
                return (f"T1/T2 both reused ({cached_note(t1)}; {cached_note(t2)}), but the second append T3 "
                        f"failed; a second append is not established.")
            if not cache_known(t3):
                return (f"T1/T2 both reused ({cached_note(t1)}; {cached_note(t2)}), but T3 returned no "
                        f"cached_tokens; a second append is not established.")
            if cache_hit(t3):
                return (f"In this session, two append events reused the earlier request: T1 {cached_note(t1)}, "
                        f"T2 {cached_note(t2)}, T3 {cached_note(t3)}. {a_note}. This supports reuse for this append "
                        f"shape, one schema, one model and one effort; it does not establish whole-body identity, a "
                        f"server end-marker rule, a cache lifetime, Standard-tier behaviour, a real stage-5/6 schema "
                        f"change, or a production three-stage chain.")
            return (f"T1/T2 both reused ({cached_note(t1)}; {cached_note(t2)}), but the second append T3 reported 0 "
                    f"cached. Reuse did not extend to T3 in this observation; one session and one shape.")
        return (f"T1 reused {cached_note(t1)}, but its exact repeat T2 reported 0. The hit was not reproduced; no "
                f"stable conclusion about this append shape.")
    if t2 is None:
        return (f"T1 reported 0 cached and the exact-repeat control T2 was not attempted "
                f"({skip_reason(skipped, 'T2')}); no inference.")
    if not row_ok(t2):
        return "T1 reported 0 cached and T2 failed; no inference."
    if not cache_known(t2):
        return "T1 reported 0 cached and T2 returned no cached_tokens; no inference."
    if cache_hit(t2):
        return (f"T1 reported 0 cached while its exact repeat T2 hit {cached_note(t2)}: this append shape did not "
                f"reuse the earlier request on first send, but the identical repeat did. {a_note}. That is an "
                f"observed difference between a new append and an exact repeat in this session; it does not prove "
                f"whole-request identity as the key, and server prompt rendering, placement, routing, threshold and "
                f"freshness remain unisolated.")
    return (f"Neither T1 nor its exact repeat T2 hit (both 0 cached). No cache reuse was observed for this append "
            f"shape in this session. {a_note}. This does not establish a universal no-reuse rule; an exact 1.7k "
            f"repeat also missed historically, while exact 26.4k repeats hit.")


async def run_turns(client: httpx.AsyncClient, api_key: str, shared: str, sch: dict, rec: Recorder) -> dict:
    """Run A, T1, T2 and, only after two valid hits, T3. One POST per attempt."""
    plain = big_body(shared, sch["readers"])
    ids = readers_field_ids(sch["readers"])
    rows: list[dict] = []
    by: dict[str, dict] = {}
    skipped: list[dict] = []
    spent: float | None = 0.0

    def note_skip(label: str, reason: str) -> None:
        if not any(item["label"].startswith(label) for item in skipped):
            skipped.append({"label": label, "reason": reason})

    async def attempt(key: str, label: str, body: dict) -> dict | None:
        nonlocal spent
        if len(rows) >= TURNS_MAX_ATTEMPTS:
            note_skip(key, f"attempt cap {TURNS_MAX_ATTEMPTS} reached")
            return None
        if spent is None:
            note_skip(key, "a prior call has unknown cost; cannot bound spend")
            return None
        if spent + TURNS_CALL_BOUND_USD > TURNS_BUDGET_USD:
            note_skip(key, f"planning bound ${spent + TURNS_CALL_BOUND_USD:.4f} would exceed "
                           f"${TURNS_BUDGET_USD:.2f}")
            return None
        row, cost, _ = await call(client, api_key, body, sch["readers"], rec, label, field_ids=ids,
                                  attempt=len(rows) + 1)
        rows.append(row)
        by[key] = row
        spent = add_cost(spent, cost)
        return row

    a = await attempt("A", "A/26.4k plain (warm)", plain)
    if not row_ok(a):
        note_skip("T1", "A did not return a usable answer")
        note_skip("T2", "requires a usable T1")
        note_skip("T3", "requires T1 and T2 hits")
    elif not cache_known(a):
        note_skip("T1", "A returned no cached_tokens; cache evidence is unknown")
        note_skip("T2", "requires a usable T1")
        note_skip("T3", "requires T1 and T2 hits")
    else:
        t1_body = appended(plain, assistant_turn(a["assistant_content"]),
                           {"role": "user", "content": TURN_NEXT})
        t1 = await attempt("T1", "T1/append A answer + user turn", t1_body)
        if not row_ok(t1):
            note_skip("T2", "requires a usable T1")
            note_skip("T3", "requires T1 and T2 hits")
        elif not cache_known(t1):
            note_skip("T2", "T1 returned no cached_tokens; cache evidence is unknown")
            note_skip("T3", "requires T1 and T2 hits")
        else:
            t2_body = json.loads(json.dumps(t1["request"]))
            if digest(t2_body) != digest(t1["request"]):
                note_skip("T2", "T2 construction changed T1's request")
                note_skip("T3", "requires T1 and T2 hits")
            else:
                t2 = await attempt("T2", "T2/exact repeat of T1", t2_body)
                if not (cache_hit(t1) and cache_hit(t2)):
                    note_skip("T3", f"requires T1 and T2 valid hits; got T1={t1.get('cached_tokens')}, "
                                    f"T2={t2.get('cached_tokens') if t2 else None}")
                else:
                    t3_body = appended(t1["request"], assistant_turn(t1["assistant_content"]),
                                       {"role": "user", "content": TURN_NEXT2})
                    await attempt("T3", "T3/second append from T1 answer + user turn", t3_body)

    known = [r["est_cost_usd"] for r in rows if isinstance(r.get("est_cost_usd"), (int, float))]
    complete = all(isinstance(r.get("est_cost_usd"), (int, float)) for r in rows)
    return {
        "calls": [{k: v for k, v in r.items() if k != "request"} for r in rows],
        "finding": turns_finding(by, skipped),
        "skipped": skipped,
        "attempts": len(rows),
        "max_attempts": TURNS_MAX_ATTEMPTS,
        "budget_usd": TURNS_BUDGET_USD,
        "cost_bound_per_call_usd": round(TURNS_CALL_BOUND_USD, 6),
        "max_planned_cost_usd": round(TURNS_MAX_ATTEMPTS * TURNS_CALL_BOUND_USD, 6),
        "total_cost_usd": round(sum(known), 6) if complete else None,
        "known_subtotal_usd": round(sum(known), 6),
        "cost_complete": complete,
        "raw": str(rec.path),
        "table_markdown": turns_table(rows),
    }


async def turns(api_key: str, shared: str, sch: dict) -> int:
    """Does a new stage reuse the earlier request when a harness appends a turn?

    A harness keeps the whole earlier conversation and appends a turn. This sends A, then T1
    built from A's exact assistant content, then T2 as an exact repeat, then T3 only after two
    valid hits. It measures reuse of this shape, not a real stage-5/6 schema change or Standard.
    """
    out = run_dir("cache-turns")
    rec = Recorder(out)
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        result = await run_turns(client, api_key, shared, sch, rec)
    (rec.path.parent / "summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"\ntotal {fmt_usd(result['total_cost_usd'])} "
          f"(known subtotal {fmt_usd(result['known_subtotal_usd'])}, {result['attempts']}/"
          f"{result['max_attempts']} attempts)\n{result['table_markdown']}\n\nfinding: {result['finding']}")
    for item in result["skipped"]:
        print(f"skipped {item['label']}: {item['reason']}")
    print(f"summary: {rec.path.parent / 'summary.json'}")
    return 0


async def call(client: httpx.AsyncClient, api_key: str, body: dict, schema: dict, rec: Recorder,
               label: str, *, field_ids: set[str] | None = None,
               attempt: int | None = None) -> tuple[dict, float | None, int | None]:
    """One POST, one recorded row. No retry, repair or model switch.

    Missing values stay null. Transport, HTTP, response-JSON, schema, reference, model and usage
    failures set `problem`; the caller stops dependent calls.
    """
    t0 = timed()
    status: int | None = None
    data: dict | None = None
    text = ""
    problem: str | None = None
    parsed = None
    message = None
    content = None
    usage = None
    cached = None
    cost = None
    response_model = None
    try:
        r = await client.post(URL, json=body, headers={"Authorization": f"Bearer {api_key}"})
        ms = (timed() - t0) * 1000
        status = r.status_code
        text = r.text
    except Exception as exc:  # transport, timeout, mock failure
        ms = (timed() - t0) * 1000
        problem = f"transport {type(exc).__name__}: {str(exc)[:200]}"
    else:
        if status != 200:
            problem = f"HTTP {status}: {text[:300]}"
        else:
            try:
                data = r.json()
            except Exception as exc:
                problem = f"invalid JSON response: {type(exc).__name__}: {str(exc)[:200]}"

    if problem is None:
        if not isinstance(data, dict):
            problem = "response body is not a JSON object"
        else:
            response_model = data.get("model")
            if not response_model:
                problem = "response omitted model"
            elif response_model != MODEL:
                problem = f"unexpected resolved model {response_model!r}"
            choices = data.get("choices")
            if problem is None and (not isinstance(choices, list) or not choices
                                    or not isinstance(choices[0], dict)):
                problem = "response omitted choices[0]"
            if problem is None:
                message = choices[0].get("message")
                if not isinstance(message, dict):
                    problem = "response omitted choices[0].message"
                else:
                    content = message.get("content")
                    if not isinstance(content, str) or not content.strip():
                        problem = "response omitted assistant content"
            if problem is None:
                try:
                    parsed = json.loads(content)
                    jsonschema.validate(parsed, schema)
                except (ValueError, jsonschema.ValidationError) as exc:
                    problem = f"invalid structured output: {str(exc)[:200]}"
            if problem is None and field_ids is not None:
                chosen = ([x.get("field_id") for x in parsed.get("readers", []) if isinstance(x, dict)]
                          if isinstance(parsed, dict) else [])
                unknown = sorted({str(i) for i in chosen if i not in field_ids})
                if unknown:
                    problem = f"unknown field id(s): {', '.join(unknown)[:120]}"
            usage = data.get("usage")
            if not isinstance(usage, dict):
                problem = problem or "response omitted usage"
                usage = None
            else:
                prompt = usage.get("prompt_tokens")
                completion = usage.get("completion_tokens")
                if not isinstance(prompt, int) or not isinstance(completion, int):
                    problem = problem or "usage omitted prompt/completion tokens"
                details = usage.get("prompt_tokens_details")
                if isinstance(details, dict) and "cached_tokens" in details:
                    cached = details.get("cached_tokens")
                    if not isinstance(cached, int) or cached < 0:
                        problem = problem or f"invalid cached_tokens {cached!r}"
                        cached = None
                    elif isinstance(prompt, int) and cached > prompt:
                        problem = problem or "cached_tokens exceeds prompt_tokens"
                        cached = None
                else:
                    cached = None  # unknown, not zero

    if problem is None and isinstance(usage, dict) and isinstance(cached, int):
        cost = price(MODEL, usage)
        if cost is None:
            problem = "cannot price usage"
    elif problem is None:
        problem = "cannot price usage (cached_tokens unknown)"

    body_sha = digest({k: v for k, v in body.items() if k != "prompt_cache_key"})
    prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    completion_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    row = {
        "label": label, "attempt": attempt,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": MODEL, "resolved_model": response_model, "effort": EFFORT,
        "status": status, "latency_ms": round(ms, 1), "problem": problem,
        "usage": usage, "cached_tokens": cached,
        "cached_fraction": (round(cached / prompt_tokens, 3)
                            if isinstance(cached, int) and isinstance(prompt_tokens, int) and prompt_tokens > 0
                            else None),
        "est_cost_usd": cost, "prompt_cache_key": body.get("prompt_cache_key"),
        "messages_sha256": digest(body["messages"]),
        "format_sha256": digest(body["response_format"]),
        "body_sha256": body_sha,
        "request": body, "output": parsed,
        "assistant_content": content,
        "assistant_message": message,
    }
    rec.write(row)
    print(f"{label:44} {status if status is not None else '----'} {ms/1000:6.1f} s  "
          f"in={prompt_tokens} cached={cached} out={completion_tokens}  {fmt_usd(cost)}  {problem or 'ok'}",
          flush=True)
    return row, cost, status


def cached_of(rows: dict, pair: str) -> int | None:
    row = rows.get(pair)
    return None if row is None else row["cached_tokens"]


def verdict(rows: dict) -> str:
    c, b, a, a2, a3 = (cached_of(rows, p) for p in ("C", "B", "A", "A2", "A3"))
    if None in (c, b, a, a2, a3):
        return "Incomplete: a call is missing or failed; draw no conclusion."
    return (f"Recorded final-call cached tokens: 1.7k control={c}, keyed 26.4k={b}, unkeyed 26.4k={a}, "
            f"changed tail={a2}, renamed schema={a3}. These per-shape outcomes do not isolate a whole-body "
            "key, size limit, schema-placement rule or cache lifetime. Keep the no-cache chain budget until "
            "a complete chain is measured; interpret them together with the append-only results.")


def markdown(rows: dict) -> str:
    lines = ["| Call | Variant | Key | Input (cached) | Cached share | Latency | Est. cost |",
             "|---|---|---|---|---|---|---|"]
    names = {"C": "1.7k control", "B": "26.4k with key", "A": "26.4k no key",
             "A2": "26.4k, different tail", "A3": "26.4k, renamed schema"}
    for pair in ("C", "B", "A", "A2", "A3"):
        row = rows.get(pair)
        if row is None:
            continue
        u = row["usage"] or {}
        prompt = u.get("prompt_tokens")
        share = f"{row['cached_fraction']:.0%}" if row["cached_fraction"] is not None else "-"
        key = row["prompt_cache_key"] or "-"
        inp = f"{prompt:,} ({row['cached_tokens']})" if isinstance(prompt, int) else "unknown"
        lines.append(f"| {pair} | {names[pair]} | `{key}` | {inp} "
                     f"| {share} | {row['latency_ms']/1000:.1f} s | {fmt_usd(row['est_cost_usd'])} |")
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
    total: float | None = 0.0
    print(f"phase 2: {len(plan)} calls, worst case $0.0121 (warm hits cost less)\n")
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        for label, name, body in plan:
            schema = FRONTIER_SCHEMA if label.startswith("C") else sch["readers"]
            row, cost, status = await call(client, api_key, body, schema, rec, f"{label}/{name}")
            total = add_cost(total, cost)
            results.append((label, name, row))
            if row.get("problem") is not None:
                print("call failed; stopping dependent calls")
                break
    by = {label: row for label, _, row in results}
    c2, a2, a3 = (by.get(k, {}).get("cached_tokens") for k in ("C2", "A2", "A3"))
    if None in (c2, a2, a3):
        finding = "Incomplete: a call is missing or failed; draw no conclusion."
    else:
        finding = (f"Recorded cached tokens: 1.7k changed tail={c2}, 26.4k changed-tail repeat={a2}, "
                   f"renamed-schema repeat={a3}. These per-shape outcomes do not isolate size, request identity, "
                   "schema placement or freshness; do not infer chain savings from them.")
    lines = ["| Call | Variant | Input (cached) | Latency | Cost |", "|---|---|---|---|---|"]
    for label, name, row in results:
        u = row["usage"]
        prompt = u.get("prompt_tokens") if isinstance(u, dict) else None
        inp = f"{prompt:,} ({row['cached_tokens']})" if isinstance(prompt, int) else "unknown"
        lines.append(f"| {label} | {name} | {inp} | "
                     f"{row['latency_ms']/1000:.1f} s | {fmt_usd(row['est_cost_usd'])} |")
    table = "\n".join(lines)
    result = {"calls": [{k: v for k, v in r.items() if k != "request"} for _, _, r in results],
              "finding": finding, "total_cost_usd": round(total, 4) if total is not None else None,
              "raw": str(rec.path), "table_markdown": table}
    (rec.path.parent / "summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"\ntotal {fmt_usd(total)}\n{table}\n\nfinding: {finding}")
    print(f"summary: {rec.path.parent / 'summary.json'}")
    return 0


async def phase3(api_key: str, shared: str, sch: dict) -> int:
    """Send a new tail after a baseline request, then repeat that exact tail body.

    The three cached-token values are observations about this sequence. They do not by themselves
    identify a whole-body cache key or exclude reuse by a different append shape.
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
    total: float | None = 0.0
    print(f"phase 3: {len(plan)} calls, worst case $0.0034\n")
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        for label, name, body in plan:
            row, cost, status = await call(client, api_key, body, sch["readers"], rec, f"{label}/{name}")
            total = add_cost(total, cost)
            results.append((label, name, row))
            if row.get("problem") is not None:
                break
    by = {label: row for label, _, row in results}
    warm, first, repeat = (by.get(k, {}).get("cached_tokens") for k in ("A", "A4/1", "A4/2"))
    if None in (warm, first, repeat):
        finding = "Incomplete: a call is missing or failed."
    else:
        finding = (f"Recorded cached tokens: baseline A={warm}, first changed tail={first}, exact repeat={repeat}. "
                   "A first-send miss followed by a repeat hit does not establish a whole-body cache key or "
                   "exclude append-only reuse. A cold baseline further limits the first comparison.")
    lines = ["| Call | Variant | Input (cached) | Latency | Cost |", "|---|---|---|---|---|"]
    for label, name, row in results:
        u = row["usage"]
        prompt = u.get("prompt_tokens") if isinstance(u, dict) else None
        inp = f"{prompt:,} ({row['cached_tokens']})" if isinstance(prompt, int) else "unknown"
        lines.append(f"| {label} | {name} | {inp} | "
                     f"{row['latency_ms']/1000:.1f} s | {fmt_usd(row['est_cost_usd'])} |")
    table = "\n".join(lines)
    result = {"calls": [{k: v for k, v in r.items() if k != "request"} for _, _, r in results],
              "finding": finding, "total_cost_usd": round(total, 4) if total is not None else None,
              "raw": str(rec.path), "table_markdown": table}
    (rec.path.parent / "summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"\ntotal {fmt_usd(total)}\n{table}\n\nfinding: {finding}")
    print(f"summary: {rec.path.parent / 'summary.json'}")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the plan and hashes, spend nothing")
    ap.add_argument("--phase2", action="store_true", help="re-check the misses and the 1.7k tail question")
    ap.add_argument("--phase3", action="store_true", help="never-sent body with a warm prefix, then repeated")
    ap.add_argument("--turns", action="store_true", help="append a turn to the previous request, as a harness does")
    ap.add_argument("--yes", action="store_true", help="confirm the estimated cost and make the calls")
    args = ap.parse_args()
    if not PROJECT.is_dir():
        print(f"missing packet source: {PROJECT}")
        return 2
    shared, fields, controls, sch = project_packet(PROJECT)
    if args.phase2:
        if args.dry_run or not args.yes:
            print("dry run: no call, no key read" if args.dry_run else "phase 2 spends money: pass --yes")
            return 0
        api_key = env("META_API_KEY")
        if not api_key:
            print("META_API_KEY is not set")
            return 1
        return await phase2(api_key, shared, sch)
    if args.phase3:
        if args.dry_run or not args.yes:
            print("dry run: no call, no key read" if args.dry_run else "phase 3 spends money: pass --yes")
            return 0
        api_key = env("META_API_KEY")
        if not api_key:
            print("META_API_KEY is not set")
            return 1
        return await phase3(api_key, shared, sch)
    if args.turns:
        plain = big_body(shared, sch["readers"])
        print("turns plan: A (26.4k plain), then T1 = A's actual assistant content + one user turn, "
              "T2 = exact copy of T1, then T3 = T1's actual assistant content + a second user turn, "
              "only if T1 and T2 both hit.")
        print(f"  A  messages {digest(plain['messages'])[:12]}  format {digest(plain['response_format'])[:12]}")
        print("  T1/T2 are built after A returns; T2 is an exact copy of T1's JSON object.")
        print(f"  envelope: at most {TURNS_MAX_ATTEMPTS} calls, ${TURNS_CALL_BOUND_USD:.4f}/call planning bound "
              f"({TURNS_EST_IN:,} input + {TURNS_EST_OUT:,} output tokens), max "
              f"${TURNS_MAX_ATTEMPTS * TURNS_CALL_BOUND_USD:.4f} against the ${TURNS_BUDGET_USD:.2f} budget")
        if args.dry_run or not args.yes:
            print("\ndry run: no call, no key read" if args.dry_run else "\npass --yes to spend")
            return 0
        api_key = env("META_API_KEY")
        if not api_key:
            print("META_API_KEY is not set")
            return 1
        return await turns(api_key, shared, sch)
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
    total: float | None = 0.0
    print()
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        for pair, name, body in plan:
            schema = FRONTIER_SCHEMA if pair == "C" else sch["readers"]
            repeats = 2 if pair in ("C", "B", "A") else 1
            for i in range(repeats):
                row, cost, status = await call(client, api_key, body, schema, rec,
                                               f"{pair}/{name}/{i + 1}")
                total = add_cost(total, cost)
                rows[pair] = row  # a pair keeps its final call, which is the measured one
                if row.get("problem") is not None:
                    print("call failed; stopping instead of paying for dependent calls")
                    break
            if not row_ok(rows.get(pair)):
                break
    result = {"pairs": {k: {kk: vv for kk, vv in v.items() if kk != "request"} for k, v in rows.items()},
              "verdict": verdict(rows), "total_cost_usd": round(total, 4) if total is not None else None,
              "raw": str(rec.path), "table_markdown": markdown(rows)}
    (rec.path.parent / "summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"\ntotal {fmt_usd(total)}\n{result['table_markdown']}\n\ndecision: {result['verdict']}")
    print(f"summary: {rec.path.parent / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
