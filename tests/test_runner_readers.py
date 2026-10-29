"""The runner with the manifest's readers installed: the CLI on the protocol peer, end to end.

The production client, loop, readers, watch, probe, driver and trace writer run for real; only S1 (a scripted
`send`) and the game (a protocol peer that answers `inspect("fields")` with scripted values) are fakes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from catalog_game import CatalogGame
from loop_fakes import GameThread
from runner_manifest import hold
from s1_fakes import Send, reply
from test_runner_session import Project, two_actions, with_fake_send
from turn_trace_check import problems

from playtest.contracts.calibration import HoldMeasurement
from playtest.core.trace import read_trace
from playtest.core.trace_replay import replay
from playtest.preflight.catalog import export
from playtest.preflight.verify import effect_tolerance
from playtest.runner import cli
from playtest.runner import probe as probe_module


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    for name in [n for n in os.environ if n.endswith("_API_KEY")]:
        monkeypatch.delenv(name)
    for name in ("UPA_SESSION_CATALOG", "UPA_SESSION_CATALOG_SHA256", "UPA_RUN_MANIFEST_ORIGIN"):
        monkeypatch.delenv(name, raising=False)


class ReaderGame(CatalogGame):
    """A catalog game whose reader value follows `series`: one value per field inspection, the last one repeating."""

    def __init__(self, *, series: list[Any] | None = None, rows: list[dict[str, Any]] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.series = list(series) if series is not None else None
        self.rows = rows
        self.field_reads = 0

    def field_rows(self) -> list[dict[str, Any]]:
        if self.rows is not None:
            return self.rows
        rows = super().field_rows()
        if self.series is not None:
            rows[0]["value"] = self.series[min(self.field_reads, len(self.series) - 1)]
        self.field_reads += 1
        return rows


def test_the_probe_supplies_the_stage_seven_allowance_only_when_it_measured_and_the_calibration_recorded_a_spread():
    hold_row = HoldMeasurement.model_validate(hold(0.2, mean=2.0, variance=0.0001))
    stage_seven = effect_tolerance(hold_row, {"idle_tolerance": 0.01, "effect_tolerance": 0.03})
    expected = {"net_displacement": 2.0, "sample_variance": 0.0001}
    comparison = probe_module._comparison(expected, {"net_displacement": 2.5})
    assert comparison is not None and (comparison.expected, comparison.observed) == (2.0, 2.5)
    assert comparison.spread == pytest.approx(stage_seven["calibration_spread"])
    assert comparison.allowed == pytest.approx(stage_seven["effect_tolerance"])          # the same formula, not a copy of it
    assert comparison.unit == "world units" and "measurement allowance of 0.05" in comparison.basis
    assert probe_module._comparison(None, {"net_displacement": 2.5}) is None            # nothing calibrated to compare with
    assert probe_module._comparison({**expected, "sample_variance": None}, {"net_displacement": 2.5}) is None   # no recorded spread
    assert probe_module._comparison({**expected, "sample_variance": -1.0}, {"net_displacement": 2.5}) is None
    assert probe_module._comparison(expected, None) is None
    for unmeasured in (None, float("nan"), float("inf")):
        assert probe_module._comparison(expected, {"net_displacement": unmeasured}) is None


def run_cli(tmp_path: Path, monkeypatch, replies: list[Any], *, game: dict[str, Any], origin: str | None = None,
            calls: int = 20, **overrides: Any):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-readers-0001")
    with GameThread(ReaderGame, catalog=export(two_actions()), **game) as thread:
        files = Project(tmp_path, thread.port, calls=calls)
        send = Send(files.out, *replies)
        with_fake_send(monkeypatch, send)
        extra = {"manifest_origin": origin} if origin is not None else {}
        code = cli.main(files.args(max_calls=calls, tokens_per_minute=600000, **extra, **overrides))
    summary = json.loads(files.summary.read_text(encoding="utf-8")) if files.summary.exists() else None
    return code, files, summary, send


def test_the_cli_installs_the_readers_and_the_run_shows_them_changing_and_stated_to_s1(tmp_path: Path, monkeypatch):
    series = [1.0] * 12 + [9.0, 9.5, 9.5]                 # read 0 is the install; turn 11 reads the first 9.0
    replies = [reply("next_action", "look")] * 13 + [reply("next_action", "finish")]
    code, files, summary, send = run_cli(tmp_path, monkeypatch, replies, game={"series": series},
                                         origin="reused: made on another build, not regenerated")
    assert code == 0 and (summary["status"], summary["turns"]) == ("complete", 14)
    # The summary says what was installed and what the watch raised.
    assert summary["readers"]["declared"] == ["speed"] and summary["readers"]["installed"]["resolved"] == ["speed"]
    assert summary["readers"]["reads"] == 15 and summary["readers"]["failed_samples"] == 0
    assert summary["readers"]["unread_by_reader"] == {}
    assert summary["suspicions"]["total"] == 1 and summary["suspicions"]["by_basis"] == {"range_exit": 1}
    assert summary["suspicions"]["by_subject"] == {"public.speed": 1} and summary["suspicions"]["min_range_readings"] == 10
    assert summary["manifest"]["origin"] == "reused: made on another build, not regenerated"
    plan = json.loads((files.out / "plan.json").read_text(encoding="utf-8"))
    assert plan["readers"] == ["speed"] and plan["invariants"] == [] and plan["manifest_origin"].startswith("reused")
    assert plan["suspicions"]["min_range_readings"] == 10 and "square root" in plan["suspicions"]["effect_allowance"]

    # Every observation in the trace carries the reader, and it changes.
    entries = read_trace(files.out / "trace.jsonl")
    observed = [e.body["public"] for e in entries if e.kind.value == "observation"]
    assert [o["speed"] for o in observed] == [1.0] * 11 + [9.0, 9.5, 9.5]
    assert all(e.body["reader_errors"] == [] for e in entries if e.kind.value == "observation")
    assert all("camera" not in o or isinstance(o["camera"], dict) for o in observed)

    # S1's context (the exact payload, as traced) carries the observation's value and the reader's facts.
    contexts = [e.body["payload"] for e in entries if e.kind.value == "context"]
    assert [c["observation"]["public"]["speed"] for c in contexts] == [1.0] * 11 + [9.0, 9.5, 9.5]
    assert contexts[0]["facts"][1] == "public.speed: now 1; previous unknown; earlier readings: none."
    assert "public.speed: now 1; previous 1; earlier range 1 to 1 (10 earlier readings)." in contexts[10]["facts"]
    suspicion_entry = next(e for e in entries if e.kind.value == "suspicion")
    assert suspicion_entry.turn == 11
    stated = f"Suspicion s1 (range_exit, trace_seq {suspicion_entry.trace_seq}): public.speed is 9: outside the range 1 to 1 that its 11 earlier readings showed."
    assert stated in contexts[11]["facts"]
    assert "public.speed: now 9.5; previous 9; earlier range 1 to 9 (12 earlier readings)." in contexts[12]["facts"]

    # The trace is the writer's and replays clean; S1 chose and the game ran exactly that.
    assert replay(entries) == []
    assert problems([e.model_dump(mode="json") for e in entries]) == []
    assert summary["adherence"] == {"rate": 1.0, "violations": []} and summary["outcomes"] == {"executed": 13, "finished": 1}
    assert summary["s1"]["calls"] == 14 and len(send.bodies) == 14


def test_an_unreadable_reader_stays_null_with_its_reason_and_the_summary_counts_it(tmp_path: Path, monkeypatch):
    rows = [{"id": "field", "scene_id": "scene", "field_type": "System.Single", "value_kind": "number",
             "value_state": "present", "value": 3.0, "is_null": False, "value_truncated": None, "read_error": None}]
    code, files, summary, _ = run_cli(tmp_path, monkeypatch, [reply("next_action", "look"), reply("next_action", "finish")],
                                      game={"rows": rows})
    assert code == 0 and summary["readers"]["unread_by_reader"] == {}
    gone = [{**rows[0], "value_state": "destroyed", "value": None}]
    code, files, summary, _ = run_cli(tmp_path / "again", monkeypatch,
                                      [reply("next_action", "look"), reply("next_action", "finish")], game={"rows": gone})
    # Installing found no reader to read, so the run ended before it called S1 at all.
    assert code == 1 and summary["status"] == "inconclusive"
    assert "none of the manifest's 1 readers can be read in the live scene" in summary["error"]
    assert "the live value is destroyed" in summary["error"]
    assert not list((files.out / "calls").glob("*.json")) and not (files.out / "trace.jsonl").exists()


def test_a_reader_that_goes_unreadable_mid_run_is_null_with_an_error_not_zero(tmp_path: Path, monkeypatch):
    class Flaky(ReaderGame):
        def field_rows(self) -> list[dict[str, Any]]:
            rows = super().field_rows()
            if self.field_reads in (3, 4):                         # reads 2 and 3 (turns 1 and 2): the value is gone
                rows[0].update(value_state="null", value_kind="null", value=None, is_null=True)
            return rows

    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-readers-0002")
    with GameThread(Flaky, catalog=export(two_actions()), series=[2.0]) as thread:
        files = Project(tmp_path, thread.port, calls=8)
        with_fake_send(monkeypatch, Send(files.out, *[reply("next_action", "look")] * 5, reply("next_action", "finish")))
        assert cli.main(files.args(max_calls=8, tokens_per_minute=600000)) == 0
    summary = json.loads(files.summary.read_text(encoding="utf-8"))
    assert summary["readers"]["unread_by_reader"] == {"speed": 2} and summary["readers"]["failed_samples"] == 0
    observed = [e.body for e in read_trace(files.out / "trace.jsonl") if e.kind.value == "observation"]
    assert [o["public"]["speed"] for o in observed] == [2.0, None, None, 2.0, 2.0, 2.0]
    assert [o["reader_errors"] for o in observed][1] == [{"reader": "speed", "error": "the live value is null"}]
    assert observed[0]["reader_errors"] == [] and observed[3]["reader_errors"] == []
    assert summary["suspicions"]["total"] == 0                       # an unknown reading raises nothing


def test_an_effect_that_differs_from_its_calibration_is_a_suspicion_in_the_trace_and_the_next_context(tmp_path: Path, monkeypatch):
    # `move` is calibrated at 6.04 (variance 0.0001: allowed 0.06); this world moves it 6.0, then not at all.
    class StuckAfterOne(ReaderGame):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.moves = 0

        def _apply(self, request, seq, session):
            if request.action == "move":
                self.moves += 1
                self._effects["move"] = [6.0, 0.0, 0.0] if self.moves == 1 else [0.0, 0.0, 0.0]
            return super()._apply(request, seq, session)

    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-readers-0003")
    with GameThread(StuckAfterOne, catalog=export(two_actions()), effects={"move": (6.0, 0.0, 0.0)}, series=[1.0]) as thread:
        files = Project(tmp_path, thread.port, calls=6)
        with_fake_send(monkeypatch, Send(files.out, reply("next_action", "move"), reply("next_action", "move"),
                                         reply("next_action", "look"), reply("next_action", "finish")))
        assert cli.main(files.args(max_calls=6, tokens_per_minute=600000)) == 0
    summary = json.loads(files.summary.read_text(encoding="utf-8"))
    assert summary["suspicions"]["by_basis"] == {"effect_mismatch": 1} and summary["suspicions"]["by_subject"] == {"move": 1}
    entries = read_trace(files.out / "trace.jsonl")
    (entry,) = [e for e in entries if e.kind.value == "suspicion"]
    assert entry.turn == 1 and entry.body["detail"]["observed"] == pytest.approx(0.0)
    assert entry.body["detail"]["expected"] == 6.04 and entry.body["detail"]["allowed"] == pytest.approx(0.06)
    contexts = [e.body["payload"] for e in entries if e.kind.value == "context"]
    assert not any(f.startswith("Suspicion") for f in contexts[1]["facts"])
    assert any(f.startswith(f"Suspicion s1 (effect_mismatch, trace_seq {entry.trace_seq}): move: measured net displacement 0 ")
               for f in contexts[2]["facts"])
    assert replay(entries) == []
