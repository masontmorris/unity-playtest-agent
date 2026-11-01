"""Live check of the opt-in S1 request formats on Liquid d1-ultra, over frozen night requests.

Each frozen context is a 31 October night-session payload together with the exact JPEG it was sent with,
recovered from the session recording by its SHA-256. Three arms send the same context:

* `night`: the night configuration (object state, `dup-state-ref/1` layout). The smoke phase checks that
  its rebuilt request hash equals the hash the night run recorded.
* `compact-default` and `compact-all`: `compact_state="default"` / `"all"` (`playtest.runner.state_codec`).

Phases:
* `smoke`: three `night` calls. Checks the endpoint, the credit, exact reproduction and agreement with the
  night's own choice.
* `gate`: one call each of `compact-default`, `compact-all`, and `compact-all` with the batched pending
  question.
* `bench`: 40 contexts × 3 arms × 5 repeats in a seeded random order.
* `summary`: the predeclared comparison, with no calls.
* `followup` (`--run-dir` of its own; `--bench-dir` the bench's): three single-variable arms, each built
  directly from the night's exact request, judged against the bench's 5-repeat night arm.
  * `null-string`: the night's state as the indented text the server renders. It should reproduce the night.
  * `reorder`: the same text with the state's keys reordered, a harmless change certain to reach the model,
    because a string state is rendered verbatim.
  * `format-only`: the night's exact state as compact JSON, with no header or rules.

The scoring rule is written to `plan.json` before any call is made. Every call is reserved in the
approval's ledger before it is sent, and the run is paced on the reported input tokens. It stops at the
first 401, 402, 403 or 429, at any halt, or after three consecutive failures. Only `LIQUID_API_KEY` and
`LIQUID_D1.1_URL` are read from the env file, and neither is printed. The adapter is
`playtest.runner.s1.SystemOneS1`: the production request path, unchanged.

    uv run python -I scripts/probes/probe_s1_compact_live.py --phase smoke \
        --night-dir <night runs dir> --run-dir runs/s1-opt-live --pred <pred.json> --env-file <main .env>
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import datetime as dt
import hashlib
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from playtest.bench import call_budget, replay
from playtest.bench.record import digest
from playtest.contracts.common import ClockMode
from playtest.core.context import canonical
from playtest.runner import request_layout
from playtest.runner.s1 import SystemOneS1
from playtest.runner.state_order import state_keys

ARM = "liquid-d1-ultra"
ARMS = {"night": {"layout": "dup-state-ref/1"},
        "compact-default": {"compact_state": "default"},
        "compact-all": {"compact_state": "all"}}
REPEATS, SEED = 5, 20261031
PRICE = 0.042e-6  # $ per input token, Liquid's reported usage.cost basis for d1-ultra
STOP_STATUS = {401, 402, 403, 429}

PLAN = {
    "question": "Does sending S1's state compactly (state_codec 'default' or 'all') change d1-ultra's choices "
                "relative to the night configuration, and what does each arm bill?",
    "unit": "one frozen night context (exact payload and exact JPEG), primary question only (stepped clock)",
    "arms": ARMS, "repeats": REPEATS, "seed": SEED,
    "primary_estimand": "per arm, the share of contexts whose modal choice over its successful repeats equals "
                        "the night arm's modal choice for that context",
    "noise_reference": "the night arm's per-call agreement with its own modal choice; each arm's per-call agreement "
                       "with the night modal is compared against it",
    "decision_rule": "an arm keeps S1's choices if modal agreement >= 36/40 and its per-call agreement with the night "
                     "modal is within 5 percentage points of the night arm's own; it changes them if modal "
                     "agreement < 32/40; otherwise inconclusive. Labels of acceptable choices do not exist for these "
                     "frames, so this measures change, not quality.",
    "secondary": ["billed input tokens per arm against the offline prediction", "latency p50/p90 per arm",
                  "failed or invalid calls per arm"],
    "attrition": "a failed call is recorded and excluded from the modal choice; a context with fewer than 3 "
                 "successful repeats in an arm is unknown for that arm, never a disagreement",
}


def load_credentials(env_file: Path) -> None:
    wanted = {"LIQUID_API_KEY": None, "LIQUID_D1.1_URL": None}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        if sep and name in wanted:
            wanted[name] = value.strip().strip('"').strip("'")
    missing = [name for name, value in wanted.items() if not value]
    if missing:
        raise SystemExit(f"missing in the env file: {missing}")
    os.environ.update(wanted)  # this process only; never printed


def load_contexts(night: Path, keys: list[str]) -> dict[str, dict[str, Any]]:
    """Each key's exact payload, JPEG, and the night's primary call (hash, choice, billed tokens)."""
    out: dict[str, dict[str, Any]] = {}
    for session in sorted({k.split("/")[0] for k in keys}):
        frames = {hashlib.sha256(f.read_bytes()).hexdigest(): f
                  for f in (night / session / "recording" / "frames").rglob("*.jpg")}
        contexts, calls = {}, {}
        for line in open(night / session / "trace.jsonl", encoding="utf-8"):
            r = json.loads(line)
            if r.get("kind") == "context":
                contexts[r["turn"]] = r["body"]["payload"]
            elif r.get("kind") == "s1_call" and r["body"].get("question") == "next_action":
                calls[r["turn"]] = r["body"]
        for key in keys:
            s, turn = key.split("/")
            if s != session:
                continue
            payload, call = contexts[int(turn)], calls[int(turn)]
            answer = ((call.get("response") or {}).get("answers") or {}).get("next_action") or {}
            out[key] = {"payload": payload, "image": frames[payload["game_view_sha256"]].read_bytes(),
                        "night_sha": call["request_sha256"], "night_choice": answer.get("choice"),
                        "night_tokens": (call.get("usage") or {}).get("input_tokens")}
    return out


def approval(path: Path, bounds: dict[str, float], calls: dict[str, int]) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    now = dt.datetime.now(dt.timezone.utc)
    arms = {name: {"max_calls": calls[name], "per_call_max_usd": bounds[name],
                   "max_cost_usd": round(calls[name] * bounds[name], 6), "verified_balance_usd": None,
                   "balance_checked_at_utc": None, "allow_unverified_price": True,
                   "estimated_price_basis": "Liquid-reported usage.cost: $0.042 per million input tokens (night sessions).",
                   "estimate_use_by_utc": (now + dt.timedelta(hours=8)).isoformat()} for name in bounds}
    data = {"approved_by": "Mason Morris", "approved_at_utc": now.isoformat(),
            "approval_scope": "d1-ultra quick try (3 calls), a 3-call gate and a 600-call paired bench of the "
                              "compact S1 state over frozen night requests; expected about $0.38; the reserved "
                              "envelope below is the per-call bound times the calls.",
            "approval_source": "Conversation 2026-10-31: 'give d1-ultra a quick try then if it's good start testing "
                               "these optimizations', after the plan stated ~600 calls at about $0.40.",
            "max_calls": sum(calls.values()), "max_cost_usd": round(sum(a["max_cost_usd"] for a in arms.values()), 6),
            "arms": arms,
            "unverified_balance_override": {"approved_by_user": True, "reason": (
                "Liquid exposes no balance API here; the smoke calls' status and reported cost are the balance check, "
                "and the run stops at the first 402.")}}
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


class Pacer:
    def __init__(self, per_minute: int) -> None:
        self.per_minute, self.window = per_minute, collections.deque()

    async def wait(self, tokens: int) -> None:
        while True:
            now = time.monotonic()
            while self.window and now - self.window[0][0] > 60:
                self.window.popleft()
            if sum(t for _, t in self.window) + tokens <= self.per_minute or not self.window:
                return
            await asyncio.sleep(0.25)

    def add(self, tokens: int) -> None:
        self.window.append((time.monotonic(), tokens))


async def one(ledgers, arm_key: str, options: dict[str, Any], ctx: dict[str, Any], episode: int,
              clock: ClockMode = ClockMode.stepped) -> dict[str, Any]:
    port = SystemOneS1(ledgers[arm_key], max_calls=2, send=replay.send_systemone, arm=ARM, clock_mode=clock,
                       episode=episode, game_view=True, instructions=ctx["payload"]["instructions"], **options)
    got = await port.decide(ctx["payload"], image=ctx["image"])
    call = got.calls[0] if got.calls else None
    usage = (call.usage if call is not None else None) or {}
    answer = got.answer or {}
    response = (call.response if call is not None else None) or {}
    second = ((response.get("answers") or {}).get("while_pending") or {}).get("choice")
    return {"status": call.http_status if call else None, "choice": answer.get("choice_id"),
            "distribution": answer.get("distribution"), "while_pending": second,
            "input_tokens": usage.get("input_tokens"), "cost": usage.get("cost"),
            "latency_ms": call.latency_ms if call else None, "request_sha256": call.request_sha256 if call else None,
            "failure": got.failure, "halt": got.halt, "calls": len(got.calls)}


def rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in open(path, encoding="utf-8")] if path.exists() else []


def append(path: Path, row: dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def stop_reason(row: dict[str, Any]) -> str | None:
    if row.get("status") in STOP_STATUS:
        return f"HTTP {row['status']}"
    return row.get("halt")


async def run(args: argparse.Namespace) -> int:
    pred = json.loads(Path(args.pred).read_text(encoding="utf-8"))
    keys = list(pred)
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    plan_path = run_dir / "plan.json"
    if not plan_path.exists():
        plan_path.write_text(json.dumps({**PLAN, "contexts": keys, "script_sha256": digest(Path(__file__).read_bytes()),
                                         "predicted_tokens": pred}, indent=1), encoding="utf-8")
    if args.phase == "summary":
        return summary(run_dir, pred)
    if args.phase == "followup":
        return await followup(args, pred, run_dir)
    bounds = {"probe": 0.0013, "night": 0.00125, "compact-default": 0.0010, "compact-all": 0.0009}
    calls = {"probe": 6, "night": len(keys) * REPEATS, "compact-default": len(keys) * REPEATS,
             "compact-all": len(keys) * REPEATS}
    approved = approval(run_dir / "approval.json", bounds, calls)
    replay.validate_approval(approved, list(bounds))
    ledgers = {name: call_budget.Ledger(run_dir, approved, name) for name in bounds}
    load_credentials(Path(args.env_file))
    pacer = Pacer(args.tpm)

    if args.phase == "smoke":
        picks = [keys[0], keys[len(keys) // 2], keys[-1]]
        ctxs = load_contexts(Path(args.night_dir), picks)
        for i, key in enumerate(picks):
            await pacer.wait(pred[key]["night"])
            row = {"phase": "smoke", "key": key, "arm": "night",
                   **await one(ledgers, "probe", ARMS["night"], ctxs[key], 1001 + i)}
            row.update(exact=row["request_sha256"] == ctxs[key]["night_sha"], night_choice=ctxs[key]["night_choice"],
                       night_tokens=ctxs[key]["night_tokens"])
            pacer.add(row["input_tokens"] or pred[key]["night"])
            append(run_dir / "smoke.jsonl", row)
            print(json.dumps({k: row[k] for k in ("key", "status", "exact", "choice", "night_choice", "input_tokens",
                                                  "night_tokens", "cost", "latency_ms", "failure")}))
            if stop_reason(row):
                print("STOP:", stop_reason(row))
                return 2
        return 0

    if args.phase == "gate":
        key = keys[len(keys) // 2]
        ctx = load_contexts(Path(args.night_dir), [key])[key]
        trials = [("compact-default", ARMS["compact-default"], ClockMode.stepped),
                  ("compact-all", ARMS["compact-all"], ClockMode.stepped),
                  ("compact-all+batched", {**ARMS["compact-all"], "pending_question": "batched"}, ClockMode.realtime)]
        for i, (name, options, clock) in enumerate(trials):
            expected = pred[key][name.split("+")[0]]
            await pacer.wait(expected + 2500)
            row = {"phase": "gate", "key": key, "arm": name, "predicted_single_question": expected,
                   **await one(ledgers, "probe", options, ctx, 1101 + i, clock)}
            pacer.add(row["input_tokens"] or expected)
            append(run_dir / "gate.jsonl", row)
            print(json.dumps({k: row[k] for k in ("arm", "status", "choice", "while_pending", "input_tokens",
                                                  "predicted_single_question", "latency_ms", "failure")}))
            if stop_reason(row) or row["status"] != 200:
                print("STOP:", stop_reason(row) or "a gate call failed")
                return 2
        return 0

    # bench
    jobs = [(key, arm, rep) for key in keys for arm in ARMS for rep in range(REPEATS)]
    random.Random(SEED).shuffle(jobs)
    done = {(r["key"], r["arm"], r["repeat"]) for r in rows(run_dir / "results.jsonl")}
    ctxs = load_contexts(Path(args.night_dir), keys)
    failures = 0
    for index, (key, arm, rep) in enumerate(jobs):
        if (key, arm, rep) in done:
            continue
        await pacer.wait(pred[key][arm])
        row = {"phase": "bench", "job": index, "key": key, "arm": arm, "repeat": rep,
               "wall_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
               **await one(ledgers, arm, ARMS[arm], ctxs[key], 1 + index)}
        pacer.add(row["input_tokens"] or pred[key][arm])
        append(run_dir / "results.jsonl", row)
        failures = 0 if row["status"] == 200 and row["choice"] else failures + 1
        if index % 25 == 0:
            print(f"{index + 1}/{len(jobs)} {arm} {key} status={row['status']} tokens={row['input_tokens']}", flush=True)
        if stop_reason(row) or failures >= 3:
            print("STOP:", stop_reason(row) or "three consecutive failures", flush=True)
            return 2
    return summary(run_dir, pred)


def tv(a: dict[str, float], b: dict[str, float]) -> float:
    return 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in set(a) | set(b))


def mean_dist(ds: list[dict[str, float]]) -> dict[str, float]:
    keys = set().union(*ds)
    return {k: sum(d.get(k, 0.0) for d in ds) / len(ds) for k in keys}


def summary(run_dir: Path, pred: dict[str, Any]) -> int:
    data = [r for r in rows(run_dir / "results.jsonl") if r.get("phase") == "bench"]
    by = collections.defaultdict(list)
    for r in data:
        by[(r["key"], r["arm"])].append(r)
    ok = lambda rs: [r for r in rs if r["status"] == 200 and r["choice"]]  # noqa: E731
    modal = {}
    for (key, arm), rs in by.items():
        good = ok(rs)
        if len(good) >= 3:
            modal[(key, arm)] = collections.Counter(r["choice"] for r in good).most_common(1)[0][0]
    out: dict[str, Any] = {"calls": len(data), "arms": {}}
    keys = sorted({k for k, _ in by})
    night_self = [r["choice"] == modal.get((r["key"], "night")) for r in data
                  if r["arm"] == "night" and r["status"] == 200 and r["choice"] and (r["key"], "night") in modal]
    for arm in ARMS:
        arm_rows = [r for r in data if r["arm"] == arm]
        good = ok(arm_rows)
        agree_ctx = [modal[(k, arm)] == modal[(k, "night")] for k in keys if (k, arm) in modal and (k, "night") in modal]
        per_call = [r["choice"] == modal.get((r["key"], "night")) for r in good if (r["key"], "night") in modal]
        tvs = []
        for k in keys:
            mine = [r["distribution"] for r in ok(by[(k, arm)]) if r.get("distribution")]
            base = [r["distribution"] for r in ok(by[(k, "night")]) if r.get("distribution")]
            if arm == "night" and len(base) >= 4:
                tvs.append(tv(mean_dist(base[: len(base) // 2]), mean_dist(base[len(base) // 2:])))
            elif arm != "night" and mine and base:
                tvs.append(tv(mean_dist(mine), mean_dist(base)))
        billed = [r["input_tokens"] for r in good if r.get("input_tokens")]
        predicted = [pred[r["key"]][arm] for r in good if r.get("input_tokens")]
        lat = sorted(r["latency_ms"] for r in good if r.get("latency_ms") is not None)
        out["arms"][arm] = {
            "calls": len(arm_rows), "ok": len(good), "failed": len(arm_rows) - len(good),
            "contexts_known": len(agree_ctx), "modal_agreement_with_night": f"{sum(agree_ctx)}/{len(agree_ctx)}",
            "per_call_agreement_with_night_modal": round(sum(per_call) / len(per_call), 4) if per_call else None,
            "mean_tv_vs_night" if arm != "night" else "night_split_half_tv": round(statistics.mean(tvs), 4) if tvs else None,
            "billed_tokens_median": statistics.median(billed) if billed else None,
            "billed_tokens_total": sum(billed),
            "billed_vs_predicted": round(sum(billed) / sum(predicted) - 1, 4) if predicted else None,
            "cost_usd": round(sum(r["cost"] for r in good if isinstance(r.get("cost"), (int, float))), 6),
            "latency_ms_p50": lat[len(lat) // 2] if lat else None,
            "latency_ms_p90": lat[int(len(lat) * 0.9)] if lat else None,
        }
    out["night_self_agreement"] = round(sum(night_self) / len(night_self), 4) if night_self else None
    for arm in ("compact-default", "compact-all"):
        a = out["arms"][arm]
        agreed, known = (int(x) for x in a["modal_agreement_with_night"].split("/"))
        gap = (out["night_self_agreement"] or 0) - (a["per_call_agreement_with_night_modal"] or 0)
        a["verdict"] = ("keeps S1's choices" if known >= 36 and agreed >= 36 and gap <= 0.05 else
                        "changes S1's choices" if known and agreed < 32 * known / 40 else "inconclusive")
    (run_dir / "summary.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))
    return 0


FOLLOWUP = {"null-string": 1, "reorder": 2, "format-only": 2}  # repeats per context
FOLLOWUP_PLAN = {
    "question": "Is the choice change specific to compaction, or does any harmless presentation change flip "
                "d1-ultra's near-ties? And does whitespace alone do it?",
    "arms": {"null-string": "the night's sent state as the indented text the server renders (a null change)",
             "reorder": "the same indented text with state's top-level keys in STATE_ORDER (a harmless change)",
             "format-only": "the night's sent state as compact JSON, with no header and no rules"},
    "repeats": FOLLOWUP, "seed": SEED + 1,
    "reference": "the bench's night arm: 5 repeats per context, its modal choice and mean distribution",
    "metric": "per-call agreement with the night modal (night leave-one-out 0.905; compact arms 0.705-0.725)",
    "reading": "null-string must agree >= 0.85 (no drift, string path equivalent), else the follow-up is void. "
               "reorder <= 0.80: any harmless change flips near-ties, so compaction is not special. reorder >= 0.85 and "
               "format-only <= 0.80: whitespace itself moves choices. reorder >= 0.85 and format-only >= 0.85: the "
               "structure (tables, '=', '@', header) moves them. Anything else is inconclusive.",
}


def night_request(ctx: dict[str, Any]) -> dict[str, Any]:
    """The night's exact request body: the payload under `dup-state-ref/1`, with its JPEG."""
    p = ctx["payload"]
    body = {"model": replay.ARMS[ARM][0], "questions": {"next_action": {
        "type": "choice", "instructions": p["instructions"], "criteria": {c["id"]: c["label"] for c in p["choices"]}}},
        "state": p}
    sent = request_layout.encode(body)
    sent["images"] = ["data:image/jpeg;base64," + base64.b64encode(ctx["image"]).decode("ascii")]
    return sent


def variant(sent: dict[str, Any], arm: str) -> dict[str, Any]:
    state = sent["state"]
    if arm == "null-string":
        text = json.dumps(state, ensure_ascii=False, indent=2)
    elif arm == "reorder":
        text = json.dumps({k: state[k] for k in state_keys(state)}, ensure_ascii=False, indent=2)
    elif arm == "format-only":
        text = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    else:
        raise ValueError(arm)
    return dict(sent, state=text)


async def direct(ledger: call_budget.Ledger, body: dict[str, Any], episode: int, seq: int) -> dict[str, Any]:
    """One ledgered call that posts exactly `canonical(body)` and hashes that text."""
    text = canonical(body)
    sha = digest(text)
    criteria = body["questions"]["next_action"]["criteria"]
    reservation = ledger.reserve(episode=episode, turn=0, based_on_seq=seq, request_sha256=sha,
                                 requested_model=body["model"], extra={"question": "next_action"})
    status, data, latency_ms, problem = await asyncio.to_thread(replay.send_systemone, ARM, text)
    choice, distribution, answer_problem = replay.parse_answer(data if status == 200 else None, set(criteria))
    usage = (data or {}).get("usage") if isinstance(data, dict) else None
    cost = usage.get("cost") if isinstance(usage, dict) else None
    row = reservation.finish(http_status=status, choice=choice, distribution=distribution,
                             problem=problem or answer_problem, latency_ms=latency_ms,
                             resolved_model=(data or {}).get("model") if isinstance(data, dict) else None,
                             usage=usage, cost_usd=cost if isinstance(cost, (int, float)) else None)
    return {"status": status, "choice": choice, "distribution": distribution,
            "input_tokens": (usage or {}).get("input_tokens"), "cost": cost, "latency_ms": latency_ms,
            "request_sha256": sha, "failure": problem or answer_problem,
            "halt": "a provider-reported cost exceeded the per-call bound" if row.get("cost_bound_exceeded") else None}


async def followup(args: argparse.Namespace, pred: dict[str, Any], run_dir: Path) -> int:
    keys = list(pred)
    plan_path = run_dir / "followup-plan.json"
    if not plan_path.exists():
        plan_path.write_text(json.dumps({**FOLLOWUP_PLAN, "contexts": keys, "bench_dir": args.bench_dir,
                                         "script_sha256": digest(Path(__file__).read_bytes())}, indent=1),
                             encoding="utf-8")
    bounds = {"null-string": 0.00125, "reorder": 0.00125, "format-only": 0.0010}
    calls = {arm: len(keys) * n for arm, n in FOLLOWUP.items()}
    approval_path = run_dir / "approval.json"
    if not approval_path.exists():
        now = dt.datetime.now(dt.timezone.utc)
        arms = {name: {"max_calls": calls[name], "per_call_max_usd": bounds[name],
                       "max_cost_usd": round(calls[name] * bounds[name], 6), "verified_balance_usd": None,
                       "balance_checked_at_utc": None, "allow_unverified_price": True,
                       "estimated_price_basis": "Liquid-reported usage.cost: $0.042 per million input tokens.",
                       "estimate_use_by_utc": (now + dt.timedelta(hours=4)).isoformat()} for name in bounds}
        approval_path.write_text(json.dumps({
            "approved_by": "Mason Morris", "approved_at_utc": now.isoformat(),
            "approval_scope": "A shorter follow-up to the compact-state bench: 200 calls on the same 40 contexts "
                              "(null-string x1, reorder x2, format-only x2), expected about $0.13.",
            "approval_source": "Conversation 2026-10-31: 'can you run a shorter follow-up?', after a ~600-call, "
                               "~$0.35 follow-up was proposed.",
            "max_calls": sum(calls.values()),
            "max_cost_usd": round(sum(a["max_cost_usd"] for a in arms.values()), 6),
            "arms": arms,
            "unverified_balance_override": {"approved_by_user": True, "reason": (
                "Liquid exposes no balance API here; the bench an hour earlier billed normally, and the run stops "
                "at the first 402.")}}, indent=1), encoding="utf-8")
    approved = json.loads(approval_path.read_text(encoding="utf-8"))
    replay.validate_approval(approved, list(bounds))
    ledgers = {name: call_budget.Ledger(run_dir, approved, name) for name in bounds}
    ctxs = load_contexts(Path(args.night_dir), keys)
    for key in keys:  # free check: each rebuilt night request is the one the night run sent
        if digest(canonical(night_request(ctxs[key]))) != ctxs[key]["night_sha"]:
            raise SystemExit(f"{key}: the rebuilt night request does not match the recorded hash")
    load_credentials(Path(args.env_file))
    pacer = Pacer(args.tpm)
    jobs = [(key, arm, rep) for key in keys for arm, n in FOLLOWUP.items() for rep in range(n)]
    random.Random(SEED + 1).shuffle(jobs)
    out = run_dir / "followup.jsonl"
    done = {(r["key"], r["arm"], r["repeat"]) for r in rows(out)}
    failures = 0
    for index, (key, arm, rep) in enumerate(jobs):
        if (key, arm, rep) in done:
            continue
        expected = pred[key]["night"]
        await pacer.wait(expected)
        body = variant(night_request(ctxs[key]), arm)
        row = {"phase": "followup", "job": index, "key": key, "arm": arm, "repeat": rep,
               "night_tokens": ctxs[key]["night_tokens"],
               **await direct(ledgers[arm], body, 2001 + index, ctxs[key]["payload"]["as_of"]["seq"])}
        pacer.add(row["input_tokens"] or expected)
        append(out, row)
        failures = 0 if row["status"] == 200 and row["choice"] else failures + 1
        if index % 25 == 0:
            print(f"{index + 1}/{len(jobs)} {arm} {key} status={row['status']} tokens={row['input_tokens']}",
                  flush=True)
        if stop_reason(row) or failures >= 3:
            print("STOP:", stop_reason(row) or "three consecutive failures", flush=True)
            return 2
    return followup_summary(run_dir, Path(args.bench_dir))


def followup_summary(run_dir: Path, bench_dir: Path) -> int:
    bench = [r for r in rows(bench_dir / "results.jsonl") if r["arm"] == "night" and r["status"] == 200 and r["choice"]]
    by = collections.defaultdict(list)
    for r in bench:
        by[r["key"]].append(r)
    night_modal = {k: collections.Counter(r["choice"] for r in v).most_common(1)[0][0] for k, v in by.items()}
    night_dist = {k: mean_dist([r["distribution"] for r in v if r.get("distribution")]) for k, v in by.items()}
    margins = {k: (lambda d: d[0] - d[1])(sorted(night_dist[k].values(), reverse=True)) for k in night_dist}
    data = [r for r in rows(run_dir / "followup.jsonl") if r["status"] == 200 and r["choice"]]
    out: dict[str, Any] = {"calls": len(rows(run_dir / "followup.jsonl")), "ok": len(data), "arms": {}}
    for arm in FOLLOWUP:
        mine = [r for r in data if r["arm"] == arm]
        agree = [r["choice"] == night_modal[r["key"]] for r in mine]
        near = [r["choice"] == night_modal[r["key"]] for r in mine if margins[r["key"]] < 0.08]
        far = [r["choice"] == night_modal[r["key"]] for r in mine if margins[r["key"]] >= 0.08]
        per_key = collections.defaultdict(list)
        for r in mine:
            per_key[r["key"]].append(r)
        tvs = [tv(mean_dist([r["distribution"] for r in v if r.get("distribution")]), night_dist[k])
               for k, v in per_key.items() if any(r.get("distribution") for r in v)]
        exact_tokens = sum(r["input_tokens"] == r["night_tokens"] for r in mine)
        billed = sum(r["input_tokens"] or 0 for r in mine)
        night_billed = sum(r["night_tokens"] or 0 for r in mine)
        lat = sorted(r["latency_ms"] for r in mine if r.get("latency_ms") is not None)
        out["arms"][arm] = {
            "calls_ok": len(mine),
            "per_call_agreement_with_night_modal": round(sum(agree) / len(agree), 4) if agree else None,
            "agreement_on_near_ties_margin_lt_0.08": f"{sum(near)}/{len(near)}",
            "agreement_elsewhere": f"{sum(far)}/{len(far)}",
            "mean_tv_vs_night": round(statistics.mean(tvs), 4) if tvs else None,
            "billed_vs_night": round(billed / night_billed - 1, 4) if night_billed else None,
            "billed_equal_to_night": f"{exact_tokens}/{len(mine)}",
            "cost_usd": round(sum(r["cost"] for r in mine if isinstance(r.get("cost"), (int, float))), 6),
            "latency_ms_p50": lat[len(lat) // 2] if lat else None,
        }
    a = {arm: out["arms"][arm]["per_call_agreement_with_night_modal"] or 0 for arm in FOLLOWUP}
    if a["null-string"] < 0.85:
        reading = "void: the null arm does not reproduce the night arm (drift or a different string rendering)"
    elif a["reorder"] <= 0.80:
        reading = "any harmless change flips d1-ultra's near-ties: compaction is not special"
    elif a["reorder"] >= 0.85 and a["format-only"] <= 0.80:
        reading = "whitespace itself moves d1-ultra's choices"
    elif a["reorder"] >= 0.85 and a["format-only"] >= 0.85:
        reading = "the structure (tables, '=', '@', header) moves the choices; whitespace alone does not"
    else:
        reading = "inconclusive"
    out["reading"] = reading
    (run_dir / "followup-summary.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--phase", choices=["smoke", "gate", "bench", "summary", "followup"], required=True)
    parser.add_argument("--bench-dir", default="runs/s1-opt-live", help="the bench whose night arm is the reference")
    parser.add_argument("--night-dir", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--pred", required=True)
    parser.add_argument("--env-file", default=r"C:\Users\mason\unity-playtest-agent\.env")
    parser.add_argument("--tpm", type=int, default=350_000)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
