"""`playtest-approval`: derive a run's per-call cost bounds from measured calls.

A live run reads one approval file: an overall call cap, an overall cost cap, and one limit per arm
carrying `per_call_max_usd` (`playtest.bench.replay.validate_approval`). That per-call number is a
promise the runner keeps: every call reserves its row before the POST, the envelope is checked
against the reservations, and after the call the provider-reported cost is compared with the bound,
so a call that exceeds it halts the run (`playtest.bench.call_budget`).

Hand-written bounds are guesses. A bound copied from a planning formula that assumes a full context
window can be thirty times the cost of the calls the run actually makes, and the run then stops
after a fraction of the calls it paid for. So this tool derives the bound from calls of the same
kind: for each arm, the largest `cost_usd` observed in that arm's own successful rows, times a
stated margin. The post-call check stays as the safety net; the derivation only has to be honest.

Measured means a row with `http_status == 200` and a finite, non-negative `cost_usd`, for that arm,
whatever its `question`: S1's `next_action` and `while_pending` calls are both S1-arm calls. Rows
that failed, were never answered or never reported a cost are not evidence and are not counted. An
arm with fewer than `--min-samples` measured calls is refused, unless the operator states the bound
with `--bound`, which is then recorded with basis `operator` so nobody mistakes it for a measurement.

Which ledgers feed which arm is explicit: `--ledger ARM=DIR` scopes a run folder to one arm, and a
plain `--ledger DIR` applies to every arm named there. Mixing the two for the same arm is refused,
because choosing between them would be a silent guess about the evidence.

Nothing here reads a key, a balance, the network or a `.env` file, and nothing here approves
anything: `--approved-by` is required, and the file records what the user said. An unverified
balance needs `--unverified-balance-reason`, the reason the user gave, or the runner refuses the
file it produced.

A local arm (`replay.LOCAL_ARMS`, the open-weights model on this machine) is billed nothing, so it
has no ledger, no balance and no price: its bounds are 0 and its call count is its whole envelope.
`--max-cost-usd` may be left out, or be 0, when every arm named is local.

    playtest-approval --arm meta-muse-spark-s1=100 --arm muse-spark-1.3=4 \\
        --ledger meta-muse-spark-s1=runs/<game>/live-05 \\
        --ledger muse-spark-1.3=runs/<game>/night/session-01 \\
        --max-cost-usd 2.10 --approved-by user \\
        --unverified-balance-reason "User approved unverified Meta balance, Standard only." \\
        --output runs/<game>/live-06-approval.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from collections.abc import Sequence
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from playtest.bench import replay

# Bounds are money, and a bound is rounded up: a bound below the largest measured call would halt
# the run it was written for.
PLACES = Decimal("0.000001")
SHOWN = Decimal("0.000000001")
BASIS_MEASURED = "measured"
BASIS_OPERATOR = "operator"
BASIS_LOCAL = "local"
DEFAULT_USE_BY_HOURS = 48


def _money(value: Any, name: str, *, zero_ok: bool = False) -> Decimal:
    """A finite, positive dollar amount. Zero is refused, since a zero bound forbids every paid call, unless
    `zero_ok`: the overall cap of an approval that names only local arms."""
    kind = "non-negative" if zero_ok else "positive"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a finite {kind} dollar amount") from None
    if not amount.is_finite() or amount < 0 or (amount == 0 and not zero_ok):
        raise ValueError(f"{name} must be a finite {kind} dollar amount")
    return amount


def _ceil(amount: Decimal) -> Decimal:
    return amount.quantize(PLACES, rounding=ROUND_CEILING)


def _shown(amount: Decimal) -> float:
    return float(amount.quantize(SHOWN, rounding=ROUND_HALF_UP))


def _stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _moment(text: str, name: str) -> dt.datetime:
    try:
        value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from None
    if value.tzinfo is None:
        raise ValueError(f"{name} needs a timezone, for example 2026-11-03T00:00:00Z")
    return value.astimezone(dt.timezone.utc)


def _pairs(values: Sequence[str], flag: str, shape: str) -> list[tuple[str, str]]:
    """`NAME=VALUE` arguments: both halves present, and the name is not blank."""
    pairs = []
    for value in values:
        name, separator, rest = value.partition("=")
        if not separator or not name.strip() or not rest.strip():
            raise ValueError(f"{flag} {value!r} must be {shape}")
        pairs.append((name.strip(), rest.strip()))
    return pairs


def _call_count(value: str, name: str) -> int:
    try:
        count = int(value)
    except ValueError:
        raise ValueError(f"--arm {name} needs a whole number of calls, not {value!r}") from None
    if count < 1:
        raise ValueError(f"--arm {name} needs at least one call")
    return count


def _ledgers(values: Sequence[str], arms: Sequence[str]) -> dict[str, tuple[Path, ...]]:
    """Resolve `--ledger` entries to the run folders each arm is measured from.

    `ARM=DIR` scopes a folder to one arm; a bare `DIR` applies to every arm. An arm-scoped folder
    wins, and mixing the two for one arm is refused rather than silently preferred.
    """
    scoped: dict[str, list[Path]] = {}
    shared: list[Path] = []
    for value in values:
        head, separator, tail = value.partition("=")
        # A Windows drive (`C:\...`) or any path separator before the `=` means this is a bare DIR.
        if separator and head.strip() and tail.strip() and not any(ch in head for ch in "/\\:"):
            scoped.setdefault(head.strip(), []).append(Path(tail.strip()).resolve())
        else:
            shared.append(Path(value).resolve())
    unknown = sorted(set(scoped) - set(arms))
    if unknown:
        raise ValueError(f"--ledger names arms that --arm does not: {', '.join(unknown)}")
    local = sorted(set(scoped) & replay.LOCAL_ARMS)
    if local:
        raise ValueError(f"{', '.join(local)} runs locally and costs nothing: it has no ledger to measure")
    resolved: dict[str, tuple[Path, ...]] = {}
    for arm in arms:
        if arm in replay.LOCAL_ARMS:
            resolved[arm] = ()
            continue
        if arm in scoped and shared:
            raise ValueError(f"{arm} has both arm-scoped and unscoped --ledger entries; give one or the other")
        entries = tuple(scoped.get(arm, ())) or tuple(shared)
        if not entries:
            raise ValueError(f"{arm} has no --ledger: a bound needs the runs it is measured from")
        for entry in entries:
            if not entry.is_dir():
                raise ValueError(f"{arm} ledger {entry} is not a directory")
            if not (entry / "calls").is_dir():
                raise ValueError(f"{arm} ledger {entry} has no calls folder (a run folder holds calls/*.json)")
        resolved[arm] = entries
    return resolved


def _cost(row: dict[str, Any]) -> Decimal | None:
    """The provider-reported cost of a row, when it is a measurement rather than an unknown."""
    if row.get("http_status") != 200:
        return None
    reported = row.get("cost_usd")
    if isinstance(reported, bool) or not isinstance(reported, (int, float)):
        return None
    amount = Decimal(str(reported))
    return amount if amount.is_finite() and amount >= 0 else None


def _token_max(rows: Sequence[dict[str, Any]], key: str) -> int | None:
    counts = []
    for row in rows:
        usage = row.get("usage")
        value = usage.get(key) if isinstance(usage, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            counts.append(value)
    return max(counts) if counts else None


def _measure(ledgers: Sequence[Path], arm: str) -> dict[str, Any]:
    """The measurable evidence for one arm: its own successful, priced rows and nothing else."""
    rows: list[dict[str, Any]] = []
    for ledger in ledgers:
        rows.extend(replay._calls(ledger / "calls"))
    arm_rows = [row for row in rows if row.get("arm") == arm]
    measured = [(row, _cost(row)) for row in arm_rows]
    samples = [row for row, cost in measured if cost is not None]
    costs = [cost for _, cost in measured if cost is not None]
    return {"rows_read": len(rows), "samples": len(samples), "rows": samples,
            "ignored_rows": len(arm_rows) - len(samples), "costs": costs}


def _limit(arm: str, calls: int, evidence: dict[str, Any], *, basis: str, bound: Decimal,
           margin: Decimal, ledgers: Sequence[Path], deadline: dt.datetime) -> dict[str, Any]:
    costs: list[Decimal] = evidence["costs"]
    # A sample count of one is still a median; zero samples make each summary statistic unknown.
    median = statistics.median(costs) if costs else None
    expected = _ceil(median * calls) if median is not None else None
    worst = _ceil(bound * calls)
    return {
        "allow_unverified_price": True,
        "estimated_price_basis": basis,
        "estimate_use_by_utc": _stamp(deadline),
        "max_calls": calls,
        "per_call_max_usd": _shown(bound),
        "max_cost_usd": _shown(worst),
        "evidence": {
            "basis": basis,
            "calls": calls,
            "ledgers": [str(ledger) for ledger in ledgers],
            "rows_read": evidence["rows_read"],
            "samples": evidence["samples"],
            "ignored_rows": evidence["ignored_rows"],
            "observed_min_usd": _shown(min(costs)) if costs else None,
            "observed_median_usd": _shown(median) if median is not None else None,
            "observed_max_usd": _shown(max(costs)) if costs else None,
            "max_prompt_tokens": _token_max(evidence["rows"], "prompt_tokens"),
            "max_completion_tokens": _token_max(evidence["rows"], "completion_tokens"),
            "margin": float(margin),
            "per_call_max_usd": _shown(bound),
            "expected_cost_usd": _shown(expected) if expected is not None else None,
            "worst_case_usd": _shown(worst),
        },
    }


def _local_limit(calls: int) -> dict[str, Any]:
    """A local arm's limit: nothing is billed, so there is no price to estimate and the call count is its envelope."""
    return {
        "max_calls": calls,
        "per_call_max_usd": 0,
        "max_cost_usd": 0,
        "evidence": {"basis": BASIS_LOCAL, "calls": calls, "samples": 0, "per_call_max_usd": 0.0,
                     "expected_cost_usd": 0.0, "worst_case_usd": 0.0},
    }


def build(arms: Sequence[tuple[str, int]], bounds: dict[str, Decimal], ledgers: dict[str, tuple[Path, ...]], *,
          margin: Decimal, min_samples: int, max_cost_usd: Decimal, use_by_utc: dt.datetime,
          approved_by: str, balance_reason: str | None, now: dt.datetime) -> dict[str, Any]:
    """Build the approval the runner reads, refusing every arm whose bound is not evidence.

    `bounds` are the operator's own per-call numbers; they are used verbatim, recorded as `operator`
    basis, and checkable against the measurement when one exists. Every other arm's bound is its
    largest measured cost times `margin`.
    """
    if not arms:
        raise ValueError("name at least one --arm NAME=CALLS")
    if len({arm for arm, _ in arms}) != len(arms):
        raise ValueError("name each --arm once")
    if margin < 1:
        raise ValueError("--margin below 1 would put the bound under the largest measured call")
    if min_samples < 1:
        raise ValueError("--min-samples must be at least 1")
    unknown = sorted(set(bounds) - {arm for arm, _ in arms})
    if unknown:
        raise ValueError(f"--bound names arms that --arm does not: {', '.join(unknown)}")
    if not approved_by.strip():
        raise ValueError("--approved-by is required: this tool does not invent an approval")
    if now >= use_by_utc:
        raise ValueError("--use-by-utc has already passed; the run would stop before its first call")

    limits: dict[str, dict[str, Any]] = {}
    for arm, calls in arms:
        if calls < 1:
            raise ValueError(f"{arm} needs at least one call")
        if arm in replay.LOCAL_ARMS:
            if arm in bounds:
                raise ValueError(f"--bound {arm}: a local arm costs nothing, so its bound is 0")
            limits[arm] = _local_limit(calls)
            continue
        evidence = _measure(ledgers[arm], arm)
        if arm in bounds:
            basis, bound = BASIS_OPERATOR, bounds[arm]
            if evidence["samples"]:
                observed = max(evidence["costs"])
                if bound < observed:
                    raise ValueError(f"--bound {arm}={bound} is below the largest measured call ${observed}; "
                                     "the run would halt on that call")
        else:
            if evidence["samples"] < min_samples:
                raise ValueError(
                    f"{arm} has {evidence['samples']} measured calls; {min_samples} are required before a bound "
                    f"is derived from them (or state one with --bound {arm}=USD and it is recorded as operator)")
            basis, bound = BASIS_MEASURED, _ceil(max(evidence["costs"]) * margin)
        limits[arm] = _limit(arm, calls, evidence, basis=basis, bound=bound, margin=margin,
                             ledgers=ledgers[arm], deadline=use_by_utc)

    worst = sum((Decimal(str(limit["max_cost_usd"])) for limit in limits.values()), Decimal(0))
    if worst > max_cost_usd:
        raise ValueError(f"the worst case ${worst} exceeds the approved cap ${max_cost_usd} "
                         f"by ${worst - max_cost_usd}")
    expected = [limit["evidence"]["expected_cost_usd"] for limit in limits.values()]
    total_expected = None if any(value is None for value in expected) else sum(
        (Decimal(str(value)) for value in expected), Decimal(0))
    total_calls = sum(calls for _, calls in arms)

    approval: dict[str, Any] = {
        "approved_by": approved_by.strip(),
        "approved_at_utc": _stamp(now),
        "max_calls": total_calls,
        "max_cost_usd": _shown(max_cost_usd),
        "arms": limits,
        "summary": {
            "calls": total_calls,
            "expected_cost_usd": _shown(total_expected) if total_expected is not None else None,
            "worst_case_usd": _shown(worst),
            "max_cost_usd": _shown(max_cost_usd),
            "slack_usd": _shown(max_cost_usd - worst),
            "note": "expected cost is each arm's measured median times its approved calls; the worst case "
                    "is each bound times its calls, which is what the runner reserves before every call",
            "arms": {arm: {"calls": limit["max_calls"], "basis": limit["evidence"]["basis"],
                           "samples": limit["evidence"]["samples"],
                           "per_call_max_usd": limit["per_call_max_usd"],
                           "expected_cost_usd": limit["evidence"]["expected_cost_usd"],
                           "worst_case_usd": limit["evidence"]["worst_case_usd"]}
                     for arm, limit in limits.items()},
        },
    }
    if balance_reason is not None:
        if not balance_reason.strip():
            raise ValueError("--unverified-balance-reason cannot be blank; say what the user approved")
        approval["unverified_balance_override"] = {"approved_by_user": True, "reason": balance_reason.strip()}
    return approval


def check_runner_accepts(approval: dict[str, Any], calls: dict[str, int]) -> None:
    """Run the checks the runner applies to this file, so a rejected file is never written.

    `replay.validate_approval` is the reader's own validation, `replay._price_deadline` is the
    deadline the ledger enforces before every call, and `playtest.runner.cli._fits` is the call and
    cost envelope the runner checks against the cap it was started with.
    """
    replay.validate_approval(approval, list(calls))
    from playtest.runner import cli as runner_cli  # the reader that enforces the file this tool writes

    now = dt.datetime.now(dt.timezone.utc)
    for arm, count in calls.items():
        if now >= replay._price_deadline(approval["arms"][arm], arm):
            raise ValueError(f"{arm} pricing validity has expired; verify the rate again")
        runner_cli._fits(approval, count, 0, arm=arm)
    total = sum((replay.arm_money(arm, approval["arms"][arm]["per_call_max_usd"], "bound") * count
                 for arm, count in calls.items()), Decimal(0))
    cap = replay.overall_cap(approval)
    if total > cap:
        raise ValueError(f"the reserved envelope ${total} exceeds the approved cap ${cap}")


def _write(path: Path, approval: dict[str, Any]) -> None:
    text = json.dumps(approval, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def _report(approval: dict[str, Any]) -> list[str]:
    summary = approval["summary"]
    lines = [f"approval: {summary['calls']} calls, worst case ${summary['worst_case_usd']}, "
             f"expected ${summary['expected_cost_usd']}, cap ${summary['max_cost_usd']} "
             f"(slack ${summary['slack_usd']})"]
    for arm, entry in approval["arms"].items():
        evidence = entry["evidence"]
        if evidence["basis"] == BASIS_LOCAL:
            lines.append(f"  {arm}: local, $0/call x {entry['max_calls']} (the call limit is its whole envelope)")
            continue
        lines.append(
            f"  {arm}: {evidence['samples']} samples, max ${evidence['observed_max_usd']} "
            f"x {evidence['margin']} -> ${entry['per_call_max_usd']}/call "
            f"x {entry['max_calls']} = ${evidence['worst_case_usd']} "
            f"(expected ${evidence['expected_cost_usd']}, basis {evidence['basis']})")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="playtest-approval", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", action="append", default=[], metavar="NAME=CALLS",
                        help="an arm and the calls the run may make of it (repeatable)")
    parser.add_argument("--ledger", action="append", default=[], metavar="[ARM=]DIR",
                        help="a prior run folder holding calls/*.json; ARM=DIR scopes it to one arm, "
                             "a bare DIR measures every arm from it (repeatable)")
    parser.add_argument("--bound", action="append", default=[], metavar="NAME=USD",
                        help="an operator-stated per-call bound, recorded as basis operator instead of "
                             "derived from measurements (repeatable)")
    parser.add_argument("--margin", default="1.5", help="bound = largest measured cost x this (default 1.5)")
    parser.add_argument("--min-samples", type=int, default=10,
                        help="measured calls an arm needs before its bound is derived (default 10)")
    parser.add_argument("--max-cost-usd", help="the approved overall cost cap (required unless every arm is local, "
                                               "where it is 0)")
    parser.add_argument("--use-by-utc",
                        help=f"when the bound stops being valid (default: {DEFAULT_USE_BY_HOURS} hours from now)")
    parser.add_argument("--unverified-balance-reason",
                        help="what the user said when approving without a verified balance; without it the "
                             "runner refuses a file that has no verified balance")
    parser.add_argument("--approved-by", required=True, help="who approved this run and its envelope")
    parser.add_argument("--output", type=Path, required=True, help="new approval file")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    try:
        arms = [(name, _call_count(calls, name)) for name, calls in _pairs(args.arm, "--arm", "NAME=CALLS")]
        named = [name for name, _ in arms]
        bounds = {name: _money(value, f"--bound {name}")
                  for name, value in _pairs(args.bound, "--bound", "NAME=USD")}
        ledgers = _ledgers(args.ledger, named)
        only_local = all(name in replay.LOCAL_ARMS for name in named)
        if args.max_cost_usd is None and not only_local:
            raise ValueError("--max-cost-usd is required: an arm named here is billed")
        cap = _money(0 if args.max_cost_usd is None else args.max_cost_usd, "--max-cost-usd", zero_ok=only_local)
        now = dt.datetime.now(dt.timezone.utc)
        use_by = (_moment(args.use_by_utc, "--use-by-utc") if args.use_by_utc is not None
                  else now + dt.timedelta(hours=DEFAULT_USE_BY_HOURS))
        if output.exists():
            raise ValueError(f"{output} exists; an approval is written to a new file")
        approval = build(arms, bounds, ledgers, margin=_money(args.margin, "--margin"),
                         min_samples=args.min_samples, max_cost_usd=cap,
                         use_by_utc=use_by, approved_by=args.approved_by,
                         balance_reason=args.unverified_balance_reason, now=now)
        check_runner_accepts(approval, dict(arms))
        _write(output, approval)
    except (OSError, ValueError) as exc:
        print(f"playtest-approval: {exc}", file=sys.stderr)
        return 1
    print("\n".join(_report(approval)))
    print(f"saved to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
