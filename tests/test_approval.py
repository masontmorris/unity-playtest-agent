"""`playtest-approval`: a per-call bound is derived from measured calls, or stated by the operator.

The fixtures are synthetic: invented arms, invented costs, invented run folders. Nothing here calls a
provider, reads a key or touches a real run.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from playtest.bench import call_budget, replay
from playtest.frontier import provider as frontier_provider
from playtest.runner import approval as approval_cli
from playtest.runner import cli as runner_cli

S1 = "invented-s1"
FRONTIER = frontier_provider.MODEL


def _row(arm: str, cost: object, *, http_status: object = 200, question: str = "next_action",
         prompt: int = 1000, completion: int = 100) -> dict:
    return {"slot": f"{arm}-{cost}-{http_status}-{question}-{prompt}", "arm": arm, "question": question,
            "status": "finished", "http_status": http_status, "cost_usd": cost,
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}}


def _ledger(root: Path, name: str, rows: list[dict]) -> Path:
    """A prior run folder with its `calls/*.json` rows."""
    calls = root / name / "calls"
    calls.mkdir(parents=True)
    for index, row in enumerate(rows):
        (calls / f"{index:04d}.json").write_text(json.dumps(row, allow_nan=False), encoding="utf-8")
    return root / name


def _measured(root: Path, name: str, arm: str, costs: list[float], **extra) -> Path:
    return _ledger(root, name, [_row(arm, cost, **extra) for cost in costs])


def _run(tmp_path: Path, *args: str, output: str = "approval.json") -> tuple[int, dict | None]:
    path = tmp_path / output
    code = approval_cli.main([*args, "--approved-by", "Test User", "--output", str(path),
                              "--unverified-balance-reason", "Synthetic balance override for a test."])
    return code, (json.loads(path.read_text(encoding="utf-8")) if path.exists() else None)


# --- the bound is measured ---------------------------------------------------------------------

def test_the_bound_is_the_largest_measured_cost_times_the_margin(tmp_path: Path) -> None:
    # A single priced `while_pending` call is still an S1 call of this arm: question does not matter.
    ledger = _ledger(tmp_path / "runs", "prior",
                     [_row(S1, 0.002, question="while_pending", prompt=1200, completion=40)])
    code, approval = _run(tmp_path, "--arm", f"{S1}=20", "--ledger", f"{S1}={ledger}",
                          "--max-cost-usd", "5", "--min-samples", "1")
    assert code == 0 and approval is not None
    limit = approval["arms"][S1]
    evidence = limit["evidence"]
    assert limit["per_call_max_usd"] == pytest.approx(0.002 * 1.5)
    assert limit["estimated_price_basis"] == "measured"
    assert limit["allow_unverified_price"] is True
    assert limit["estimate_use_by_utc"].endswith("Z")
    assert evidence["basis"] == "measured"
    assert evidence["samples"] == 1
    assert evidence["observed_max_usd"] == pytest.approx(0.002)
    assert evidence["margin"] == 1.5
    assert evidence["per_call_max_usd"] == limit["per_call_max_usd"]
    assert evidence["max_prompt_tokens"] == 1200 and evidence["max_completion_tokens"] == 40
    assert [Path(entry).name for entry in evidence["ledgers"]] == ["prior"]


def test_the_bound_uses_the_largest_of_many_measured_calls(tmp_path: Path) -> None:
    costs = [0.001 * step for step in range(1, 13)]  # 12 calls, 0.001 .. 0.012
    ledger = _measured(tmp_path / "runs", "prior", S1, costs)
    code, approval = _run(tmp_path, "--arm", f"{S1}=20", "--ledger", f"{S1}={ledger}",
                          "--max-cost-usd", "5")
    assert code == 0 and approval is not None
    limit = approval["arms"][S1]
    evidence = limit["evidence"]
    assert limit["per_call_max_usd"] == pytest.approx(0.012 * 1.5)
    assert evidence["samples"] == 12
    assert (evidence["observed_min_usd"], evidence["observed_median_usd"], evidence["observed_max_usd"]) \
        == (pytest.approx(0.001), pytest.approx(0.0065), pytest.approx(0.012))
    assert evidence["expected_cost_usd"] == pytest.approx(0.0065 * 20)
    assert evidence["worst_case_usd"] == pytest.approx(0.018 * 20)
    summary = approval["summary"]
    assert summary["calls"] == 20
    assert summary["expected_cost_usd"] == pytest.approx(0.13)
    assert summary["worst_case_usd"] == pytest.approx(0.36)
    assert summary["max_cost_usd"] == pytest.approx(5)


# --- refusals ----------------------------------------------------------------------------------

def test_too_few_samples_is_refused_unless_the_operator_states_the_bound(tmp_path: Path, capsys) -> None:
    ledger = _measured(tmp_path / "runs", "prior", S1, [0.004, 0.006, 0.011])
    code, refused = _run(tmp_path, "--arm", f"{S1}=20", "--ledger", f"{S1}={ledger}",
                         "--max-cost-usd", "5")
    assert code == 1 and refused is None
    message = capsys.readouterr().err
    assert "3 measured calls" in message and "10 are required" in message
    assert "--bound" in message

    code, approval = _run(tmp_path, "--arm", f"{S1}=20", "--ledger", f"{S1}={ledger}",
                          "--max-cost-usd", "5", "--bound", f"{S1}=0.1")
    assert code == 0 and approval is not None
    limit = approval["arms"][S1]
    assert limit["per_call_max_usd"] == pytest.approx(0.1)
    assert limit["estimated_price_basis"] == "operator"
    assert limit["evidence"]["basis"] == "operator"
    assert limit["evidence"]["samples"] == 3  # recorded, but it did not decide the bound

    # An operator bound below a measured call would halt the run, so it is refused too.
    code = approval_cli.main(["--arm", f"{S1}=1", "--ledger", f"{S1}={ledger}",
                              "--bound", f"{S1}=0.001", "--max-cost-usd", "5",
                              "--approved-by", "Test User",
                              "--unverified-balance-reason", "Synthetic override.",
                              "--output", str(tmp_path / "second.json")])
    assert code == 1
    assert "below the largest measured call" in capsys.readouterr().err
    assert not (tmp_path / "second.json").exists()


def test_a_worst_case_over_the_cap_is_refused_and_says_by_how_much(tmp_path: Path, capsys) -> None:
    costs = [0.001 * step for step in range(1, 13)]  # bound 0.018, 100 calls -> worst case 1.8
    ledger = _measured(tmp_path / "runs", "prior", S1, costs)
    code, approval = _run(tmp_path, "--arm", f"{S1}=100", "--ledger", f"{S1}={ledger}",
                          "--max-cost-usd", "1.0")
    assert code == 1 and approval is None
    message = capsys.readouterr().err
    assert "worst case $1.8" in message
    assert "$1.0" in message and "by $0.8" in message


def test_non_200_and_unknown_cost_rows_are_not_evidence(tmp_path: Path) -> None:
    rows = [_row(S1, 0.05) for _ in range(10)]
    rows += [
        _row(S1, 0.9, http_status=500),      # failed
        _row(S1, 0.9, http_status=402),      # refused by the provider
        _row(S1, None),                      # answered, cost unknown
        _row(S1, "0.9"),                     # a string is not a measurement
        _row(S1, -0.5),                      # not a cost
        _row(S1, True),                      # a bool is not a dollar amount
        {"arm": S1, "status": "in_flight"},  # never settled, so never evidence
        _row("another-arm", 5.0),            # a different arm's spending
    ]
    ledger = _ledger(tmp_path / "runs", "prior", rows)
    code, approval = _run(tmp_path, "--arm", f"{S1}=10", "--ledger", f"{S1}={ledger}",
                          "--max-cost-usd", "5")
    assert code == 0 and approval is not None
    evidence = approval["arms"][S1]["evidence"]
    assert evidence["samples"] == 10
    assert evidence["ignored_rows"] == 7
    assert evidence["observed_max_usd"] == pytest.approx(0.05)
    assert approval["arms"][S1]["per_call_max_usd"] == pytest.approx(0.075)
    # Every row was read; only this arm's rows could have been evidence at all.
    assert evidence["rows_read"] == len(rows)


# --- the file the runner reads -----------------------------------------------------------------

def test_the_output_passes_the_runners_own_approval_checks(tmp_path: Path) -> None:
    s1_ledger = _measured(tmp_path / "runs", "s1-prior", S1,
                          [0.002 + 0.0005 * step for step in range(12)])
    frontier_ledger = _measured(tmp_path / "runs", "frontier-prior", FRONTIER,
                                [0.01 + 0.001 * step for step in range(12)])
    code, approval = _run(tmp_path, "--arm", f"{S1}=40", "--arm", f"{FRONTIER}=4",
                          "--ledger", f"{S1}={s1_ledger}", "--ledger", f"{FRONTIER}={frontier_ledger}",
                          "--max-cost-usd", "1.0")
    assert code == 0 and approval is not None

    # 1. The reader's own validation, as `replay.run` and the benches apply it.
    replay.validate_approval(approval, [S1, FRONTIER])
    # 2. The pricing deadline a ledger enforces before every call.
    for arm in (S1, FRONTIER):
        assert replay._price_deadline(approval["arms"][arm], arm) > dt.datetime.now(dt.timezone.utc)
    # 3. The runner's pre-flight check of the envelope it was started with.
    runner_cli._fits(approval, 40, 4, arm=S1)
    # 4. The ledger reserves in both arms and the post-call check has a readable bound to compare with.
    for arm, turn in ((S1, 0), (FRONTIER, 1)):
        ledger = call_budget.Ledger(tmp_path / f"live-{arm}", approval, arm, concurrent_arms=True)
        reservation = ledger.reserve(episode=1, turn=turn, based_on_seq=turn + 1,
                                     request_sha256=str(turn) * 64, requested_model=arm)
        assert reservation.bound == replay._decimal(approval["arms"][arm]["per_call_max_usd"], "bound")
        assert reservation.finish(http_status=200, cost_usd=0.0)["cost_bound_exceeded"] is False
    assert approval["max_calls"] == 44
    assert approval["max_cost_usd"] == pytest.approx(1.0)


# --- explicit evidence, explicit approval ------------------------------------------------------

def test_ledger_entries_are_explicit_per_arm(tmp_path: Path, capsys) -> None:
    s1_ledger = _measured(tmp_path / "runs", "s1-prior", S1, [0.01] * 10)
    code, approval = _run(tmp_path, "--arm", f"{S1}=5", "--ledger", str(s1_ledger),
                          "--ledger", f"{S1}={s1_ledger}", "--max-cost-usd", "5")
    assert code == 1 and approval is None
    assert "arm-scoped and unscoped" in capsys.readouterr().err

    code = approval_cli.main(["--arm", f"{S1}=5", "--ledger", f"{S1}={s1_ledger}",
                              "--ledger", f"stranger={s1_ledger}", "--max-cost-usd", "5",
                              "--approved-by", "Test User",
                              "--unverified-balance-reason", "Synthetic override.",
                              "--output", str(tmp_path / "other.json")])
    assert code == 1
    assert "--ledger names arms that --arm does not" in capsys.readouterr().err


def test_the_tool_adds_no_approval_of_its_own_and_never_overwrites(tmp_path: Path, capsys) -> None:
    ledger = _measured(tmp_path / "runs", "prior", S1, [0.01] * 10)
    existing = tmp_path / "approval.json"
    existing.write_text("{}\n", encoding="utf-8")
    code = approval_cli.main(["--arm", f"{S1}=5", "--ledger", f"{S1}={ledger}", "--max-cost-usd", "5",
                              "--approved-by", "Test User", "--output", str(existing)])
    assert code == 1 and "exists; an approval is written to a new file" in capsys.readouterr().err

    # Without a stated reason the runner refuses the balance, so the tool writes nothing.
    code = approval_cli.main(["--arm", f"{S1}=5", "--ledger", f"{S1}={ledger}", "--max-cost-usd", "5",
                              "--approved-by", "Test User", "--output", str(tmp_path / "fresh.json")])
    assert code == 1 and not (tmp_path / "fresh.json").exists()
    assert "verified balance or explicit user override" in capsys.readouterr().err

    # `--approved-by` has no default: the user's approval is never assumed.
    with pytest.raises(SystemExit):
        approval_cli.main(["--arm", f"{S1}=5", "--ledger", f"{S1}={ledger}", "--max-cost-usd", "5",
                           "--unverified-balance-reason", "Synthetic override.",
                           "--output", str(tmp_path / "no-user.json")])
