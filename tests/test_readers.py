"""Reader values in the observation, and the live readers that produce them.

`install` puts a sample in an observation; `FieldReaders` builds the sample from the Editor's field inspection.
A reader that cannot be read is null with a reason, never zero; a lost bridge is not a failed read.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import pytest

from loop_fakes import TOKEN, TransformsGame
from runner_manifest import fixture

from playtest.contracts.bridge import InspectionCap, InspectResult, Observation
from playtest.contracts.manifest import ManifestReader
from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.ports import ReaderSample
from playtest.core.readers import install, readings, unread
from playtest.runner.readers import FieldReaders

START = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)


def observation(**public: Any) -> Observation:
    return Observation(session="s", seq=4, game_s=1.0, wall_utc=START, clock_mode="realtime", scene="main", playing=True,
                       paused=False, time_scale=1.0, frame=9, public=public, ui=None, frame_ref=None, reader_errors=[])


# --- installing a sample ---------------------------------------------------------------------


def test_a_sample_puts_every_reader_under_public_and_each_failure_in_reader_errors():
    base = observation(camera={"fov": 65.0})
    sample = ReaderSample(values={"speed": 4.5, "gone": None, "flag": False}, errors={"gone": "the field is not in the live scene"})
    out = install(base, sample)
    assert out.public == {"camera": {"fov": 65.0}, "speed": 4.5, "gone": None, "flag": False}
    assert [(e.reader, e.error) for e in out.reader_errors] == [("gone", "the field is not in the live scene")]
    assert base.public == {"camera": {"fov": 65.0}} and base.reader_errors == []         # the bridge's own is untouched
    assert out.seq == base.seq and out.game_s == base.game_s


def test_false_zero_and_empty_are_values_and_a_none_without_a_reason_still_gets_an_error():
    out = install(observation(), ReaderSample(values={"a": 0, "b": False, "c": "", "d": None}, errors={}))
    assert out.public == {"a": 0, "b": False, "c": "", "d": None}
    assert [e.reader for e in out.reader_errors] == ["d"]                  # null is never silent


def test_a_name_the_bridge_already_publishes_is_not_overwritten():
    out = install(observation(camera={"fov": 65.0}), ReaderSample(values={"camera": 1.0}, errors={}))
    assert out.public == {"camera": {"fov": 65.0}}
    assert "already publishes this name" in out.reader_errors[0].error and out.reader_errors[0].reader == "camera"


def test_an_unread_sample_makes_every_reader_unknown_with_the_reason():
    sample = unread(["a", "b"], "the inspection failed")
    assert sample.values == {"a": None, "b": None} and sample.problem == "the inspection failed"
    out = install(observation(), sample)
    assert out.public == {"a": None, "b": None}
    assert [(e.reader, e.error) for e in out.reader_errors] == [("a", "the inspection failed"), ("b", "the inspection failed")]


def test_readings_read_none_for_a_missing_alias():
    assert readings({"a": 1, "z": 2}, ["a", "b"]) == {"a": 1, "b": None}


# --- the live readers --------------------------------------------------------------------------


def reader(alias: str = "speed", field_id: str = "field", **descriptor: Any) -> ManifestReader:
    base = deepcopy(fixture()["readers"][0])
    base.update(alias=alias, field_id=field_id)
    base["descriptor"] = {**base["descriptor"], "id": field_id, **descriptor}
    return ManifestReader.model_validate(base)


def row(**overrides: Any) -> dict[str, Any]:
    return {"id": "field", "scene_id": "scene", "field_type": "System.Single", "value_kind": "number",
            "value_state": "present", "value": 4.5, "is_null": False, "value_truncated": None, "read_error": None,
            **overrides}


def response(rows: list[dict[str, Any]], *, truncated: bool = False, stage: str = "fields", game_s: Any = 12.5) -> InspectResult:
    cap = InspectionCap(limit=100, observed=len(rows) + (5 if truncated else 0), returned=len(rows), truncated=truncated)
    return InspectResult(protocol="upa/1", session="s", seq=77, game_s=1.0, wall_utc=START, type="inspection", stage=stage,
                         in_reply_to=3, data={"fields": rows, "game_time_s": game_s}, caps={"fields": cap}, errors=[])


class Client:
    """Answers `inspect("fields")` with scripted responses (or raises them)."""

    def __init__(self, *items: Any) -> None:
        self.items = list(items)
        self.stages: list[str] = []

    async def inspect(self, stage: str) -> InspectResult:
        self.stages.append(stage)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def read_with(readers: list[ManifestReader], *items: Any) -> tuple[ReaderSample, FieldReaders]:
    live = FieldReaders(Client(*items), readers)                     # type: ignore[arg-type]
    return asyncio.run(live.read()), live


def test_a_reader_is_read_by_its_field_id_with_the_inspection_seq_and_game_time():
    sample, live = read_with([reader()], response([row(id="other", value=1.0), row()]))
    assert sample.values == {"speed": 4.5} and sample.errors == {} and sample.problem is None
    assert (sample.inspection_seq, sample.game_s) == (77, 12.5)
    assert live.stats.summary() == {"reads": 1, "failed_samples": 0, "unread_by_reader": {}}


def test_a_value_of_false_or_zero_is_a_value():
    sample, _ = read_with([reader(alias="a"), reader(alias="b", field_id="b")],
                          response([row(value=0), row(id="b", value_kind="boolean", value=False)]))
    assert sample.values == {"a": 0, "b": None} and "b" in sample.errors          # b is declared a number: its kind differs
    flag = reader(alias="flag", value_kind="boolean")
    flag = ManifestReader.model_validate({**flag.model_dump(mode="json"), "kind": "bool"})
    sample, _ = read_with([flag], response([row(value_kind="boolean", value=False)]))
    assert sample.values == {"flag": False} and sample.errors == {}


@pytest.mark.parametrize("case, rows, reason", [
    ("not in the scene", [row(id="other")], "the field is not in the live scene"),
    ("another scene", [row(scene_id="elsewhere")], "differs from the manifest's descriptor (scene_id)"),
    ("another type", [row(field_type="System.Int32")], "differs from the manifest's descriptor (field_type)"),
    ("null", [row(value_state="null", value_kind="null", value=None)], "the live value is null"),
    ("destroyed", [row(value_state="destroyed", value=None)], "the live value is destroyed"),
    ("read error", [row(value_state="error", value_kind="error", value=None, read_error="NullReferenceException")],
     "the live value is error (NullReferenceException)"),
    ("unsupported", [row(value_state="unsupported", value_kind="unsupported", value=None)], "the live value is unsupported"),
    ("not finite", [row(value_kind="special_number", value="NaN")], "the live value is special_number"),
    ("another kind", [row(value_kind="string", value="fast")], "the live value is a string value and the manifest declares a number reader"),
])
def test_a_reader_that_cannot_be_read_is_null_with_the_reason(case, rows, reason):
    sample, live = read_with([reader()], response(rows))
    assert sample.values == {"speed": None}, case
    assert reason in sample.errors["speed"], case
    assert live.stats.unread == {"speed": 1}


def test_a_field_missing_from_a_truncated_inspection_says_so():
    sample, _ = read_with([reader()], response([row(id="other")], truncated=True))
    assert sample.errors["speed"] == "the field is not in the live inspection, which was truncated"


def test_a_truncated_value_is_not_a_value():
    sample, _ = read_with([reader(alias="name", value_kind="string")], response([row(value_kind="string", value="abc", value_truncated=True)]))
    assert sample.values == {"name": None}


def test_a_reply_with_no_field_list_leaves_every_reader_unknown():
    sample, live = read_with([reader()], response([row()], stage="transforms"))
    assert sample.values == {"speed": None} and sample.problem is not None and live.stats.failed_samples == 1
    broken = InspectResult(protocol="upa/1", session="s", seq=3, game_s=None, wall_utc=START, type="inspection", stage="fields",
                           in_reply_to=1, data={}, caps={}, errors=[])
    sample, _ = read_with([reader()], broken)
    assert sample.values == {"speed": None} and sample.problem == "the field inspection did not return a field list"


def test_a_game_time_nobody_measured_is_unknown():
    for bad in (None, "soon", True, float("nan")):
        sample, _ = read_with([reader()], response([row()], game_s=bad))
        assert sample.game_s is None


def test_a_lost_bridge_ends_the_run_instead_of_reading_null():
    live = FieldReaders(Client(BridgeError("closed", "connection is closed")), [reader()])      # type: ignore[arg-type]
    with pytest.raises(BridgeError):
        asyncio.run(live.read())


def test_installing_reports_which_readers_resolve_and_refuses_a_scene_that_has_none():
    both = [reader(), reader(alias="gone", field_id="gone")]
    live = FieldReaders(Client(response([row()])), both)                    # type: ignore[arg-type]
    installed = asyncio.run(live.install())
    assert installed.resolved == ["speed"] and installed.unresolved == [
        {"alias": "gone", "reason": "the field is not in the live scene"}]
    assert installed.summary()["inspection_seq"] == 77 and live.installed is installed
    nothing = FieldReaders(Client(response([row(id="other")])), [reader()])           # type: ignore[arg-type]
    with pytest.raises(ValueError, match="none of the manifest's 1 readers can be read"):
        asyncio.run(nothing.install())
    assert FieldReaders(Client(), []).aliases == []


def test_the_live_readers_work_through_the_real_client_against_the_protocol_peer():
    async def main():
        game = TransformsGame(token=TOKEN)
        port = await game.start()
        client = BridgeClient("127.0.0.1", port, TOKEN)
        await client.connect()
        try:
            live = FieldReaders(client, [ManifestReader.model_validate(fixture()["readers"][0])])
            assert (await live.install()).resolved == ["speed"]
            first = await live.read()
            game._pos[0] = 6.0
            second = await live.read()
            return first, second, live
        finally:
            await client.aclose()
            await game.stop()

    first, second, live = asyncio.run(main())
    assert first.values == {"speed": 0.0} and second.values == {"speed": 6.0}
    assert second.inspection_seq > first.inspection_seq and live.stats.reads == 3
