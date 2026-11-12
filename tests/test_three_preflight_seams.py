import asyncio

import pytest

from playtest.contracts.bridge import ReaderError
from playtest.preflight.calibrate import summarize_action
from playtest.preflight.input_probe import ProbePlan, measure
from playtest.runner.readers import published_install
from test_input_probe import FakeProbeClient, action_declaration
from test_readers import observation, reader


def test_published_install_distinguishes_null_errors_and_absent_values():
    sample = observation(null_value=None, broken=None, flag=False).model_copy(update={
        "reader_errors": [ReaderError(reader="broken", error="unavailable")]
    })
    result = published_install(sample, [reader("null_value"), reader("broken"), reader("flag")])
    assert result.resolved == ["null_value", "flag"]
    assert result.unresolved == [{"alias": "broken", "reason": "unavailable"}]
    assert sample.public == {"null_value": None, "broken": None, "flag": False}
    with pytest.raises(ValueError, match="absent"):
        published_install(sample, [reader("null_value"), reader("missing")])
    with pytest.raises(ValueError, match="none"):
        published_install(sample, [reader("broken")])


def test_requested_hold_summary_requires_two_valid_repeats():
    action = {"control": "stick", "resolution": "declared", "bindings": []}
    def trial(delta):
        return {"requested_hold_s": 0.6, "valid": True, "effect_magnitude": abs(delta),
                "effect_delta": [delta, 0, 0], "execution": [{"applied_duration_s": 0.6}]}

    one = summarize_action(action, [trial(1)], holds=(0.6,))["holds"][0]
    assert one["effective_in_all_repeats"] is None
    two = summarize_action(action, [trial(1), trial(2)], holds=(0.6,))["holds"][0]
    assert two["mean_delta"] == [1.5, 0, 0]
    assert two["sample_variance"] == pytest.approx(0.5)
    assert two["effective_in_all_repeats"] is True
    empty = summarize_action(action, [], holds=(0.6,))["holds"][0]
    assert empty["mean_delta"] is None and empty["effective_in_all_repeats"] is None


def test_probe_archives_full_before_and_after_observations_and_streams_readings():
    class Client(FakeProbeClient):
        async def observe_once(self, *, with_frame=False):
            self.observations += 1
            return observation(value=None if self.observations == 1 else 2).model_copy(update={
                "seq": self.observations,
                "reader_errors": [] if self.observations == 1 else [
                    ReaderError(reader="broken", error="failed")],
            })

    plan = ProbePlan(schema_version=1, actions=[{"name": "move", "repeats": 1}], settle_s=0)
    readings = []
    result = asyncio.run(measure(Client(), plan, [action_declaration()], entity_id=None,
                                 scene_id=None, readers=[], on_reading=lambda kind, value: readings.append((kind, value))))
    attempt = result[0]["attempts"][0]
    assert attempt["before_observation"]["public"] == {"value": None}
    assert attempt["after_observation"]["reader_errors"] == [{"reader": "broken", "error": "failed"}]
    assert [kind for kind, _ in readings] == ["before_transform", "before_observation", "execution",
                                             "after_transform", "after_observation"]
