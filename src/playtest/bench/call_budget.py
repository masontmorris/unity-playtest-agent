"""One shared call ledger for S1 and frontier calls (#33).

Every paid call in a live run -- whichever arm makes it -- reserves its row in the same
`<run>/calls` folder before the POST and settles that row afterwards, even when the call failed.
The approval is checked against every row for the overall limit and against rows of the same arm
for the per-arm limit, so one arm cannot spend another arm's allowance. An existing `in_flight`
row means an earlier attempt's outcome is unknown: no new call is sent, and the run stops. The
pricing deadline is checked on every reservation. Nothing here retries, substitutes or repairs.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from playtest.bench import replay
from playtest.core.context import canonical


class BudgetStop(RuntimeError):
    """The approved envelope forbids another call. The adapter sends nothing."""


@dataclass(frozen=True)
class Reservation:
    """An exclusively written `in_flight` row that settles exactly once."""

    ledger: "Ledger"
    path: Path
    row: dict[str, Any]
    bound: Decimal

    def finish(self, **fields: Any) -> dict[str, Any]:
        """Settle the reservation. An unknown cost stays null and the bound is then unknown."""
        self.row.update(status="finished", **fields)
        cost = self.row.get("cost_usd")
        measured = (isinstance(cost, (int, float)) and not isinstance(cost, bool)
                    and math.isfinite(cost) and cost >= 0)
        self.row["cost_bound_exceeded"] = Decimal(str(cost)) > self.bound if measured else None
        replay._write(self.path, self.row)
        self.ledger.rows.append(self.row)
        return self.row


class Ledger:
    """The reservations of one run, shared by every adapter that spends from the approval."""

    def __init__(self, directory: Path, approval: Mapping[str, Any], arm: str) -> None:
        self.directory = directory
        self.call_dir = directory / "calls"
        self.call_dir.mkdir(parents=True, exist_ok=True)
        self.approval = approval
        self.arm = arm
        self.limit = approval["arms"][arm]
        self.bound = replay._decimal(self.limit["per_call_max_usd"], f"{arm} per-call bound")
        self.rows: list[dict[str, Any]] = replay._calls(self.call_dir)

    def arm_rows(self) -> list[dict[str, Any]]:
        self._reload()
        rows = [row for row in self.rows if row.get("arm") == self.arm]
        return sorted(rows, key=lambda row: (row.get("episode") or 0, row.get("turn") or 0))

    def _reload(self) -> None:
        """Read the shared folder, so rows another adapter wrote are counted too."""
        self.rows = replay._calls(self.call_dir)

    def reserve(
        self,
        *,
        episode: int,
        turn: int,
        based_on_seq: int,
        request_sha256: str,
        requested_model: str,
        extra: Mapping[str, Any] | None = None,
    ) -> Reservation:
        """Write the reservation exclusively before the POST, or raise `BudgetStop`.

        Missing or unreadable reserved costs fail closed: an envelope that cannot be totalled is
        not assumed to have room.
        """
        self._reload()
        deadline = replay._price_deadline(self.limit, self.arm)
        if dt.datetime.now(dt.timezone.utc) >= deadline:
            raise BudgetStop(f"{self.arm} pricing validity expired; stopped before another call")
        if any(row.get("status") == "in_flight" for row in self.rows):
            raise BudgetStop("an earlier call was left in flight; no new call is sent until it is resolved")

        arm_rows = self.arm_rows()
        total = self._total(self.rows, "the run's reservations")
        arm_total = self._total(arm_rows, f"{self.arm}'s reservations")
        if (len(self.rows) >= self.approval["max_calls"]
                or len(arm_rows) >= self.limit["max_calls"]
                or total + self.bound > replay._decimal(self.approval["max_cost_usd"], "overall limit")
                or arm_total + self.bound > replay._decimal(self.limit["max_cost_usd"], "arm limit")):
            raise BudgetStop("the approved call or cost envelope would be exceeded")

        slot = replay.digest(canonical({"episode": episode, "turn": turn, "request_sha256": request_sha256}))
        row: dict[str, Any] = {
            "slot": slot,
            "episode": episode,
            "turn": turn,
            "arm": self.arm,
            "based_on_seq": based_on_seq,
            "request_sha256": request_sha256,
            "requested_model": requested_model,
            "reserved_usd": str(self.bound),
            "status": "in_flight",
        }
        row.update(extra or {})
        path = self.call_dir / f"{slot}.json"
        replay._write(path, row, exclusive=True)  # Reserve before the POST; a crash must not retry.
        return Reservation(self, path, row, self.bound)

    @staticmethod
    def _total(rows: list[dict[str, Any]], what: str) -> Decimal:
        total = Decimal(0)
        for row in rows:
            try:
                value = Decimal(str(row.get("reserved_usd")))
            except (InvalidOperation, ValueError):
                raise BudgetStop(f"{what} include an unreadable reserved cost; no new call is sent") from None
            if not value.is_finite() or value < 0:
                raise BudgetStop(f"{what} include an unreadable reserved cost; no new call is sent")
            total += value
        return total
