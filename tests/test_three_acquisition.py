import asyncio
import struct
from datetime import datetime, timezone

from playtest.contracts.bridge import InspectionCap, InspectResult, Observation, StopResult
from playtest.preflight import three_session
from playtest.preflight.three import compile_study
from test_input_probe import receipt, record
from test_three_preflight import study


def test_acquisition_records_observations_and_exact_executions(tmp_path, monkeypatch):
    source = study(tmp_path)
    capture = tmp_path / "capture.png"
    capture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 64, 32) + b"frame")

    class Client:
        def __init__(self):
            self.status = type("Status", (), {"session": "s"})()
            self.seq, self.t, self.pos, self.calls, self.stages = 0, 0.0, 0.0, [], []

        async def inspect(self, stage):
            self.stages.append(stage)
            assert stage == "transforms"
            self.t += 0.1
            return InspectResult(protocol="upa/1", session="s", seq=1, game_s=self.t,
                wall_utc=datetime.now(timezone.utc), type="inspection", stage=stage, in_reply_to=1,
                data={"position_space": "world", "position_unit": "three_world_unit",
                      "position_order": ["x", "y", "z"], "time_unit": "seconds", "frame": self.seq,
                      "real_time_s": self.t, "game_time_s": self.t,
                      "game_objects": [{"id": "entity", "name": "body", "parent_id": None,
                                        "position": [self.pos, 0, 0], "basis_state": "present",
                                        "world_forward": [0, 0, 1], "world_right": [1, 0, 0]}]},
                caps={"game_objects": InspectionCap(limit=10, observed=1, returned=1, truncated=False)}, errors=[])

        async def observe_once(self, *, with_frame=False):
            self.seq += 1
            return Observation(session="s", seq=self.seq, game_s=self.t, wall_utc=datetime.now(timezone.utc),
                clock_mode="realtime", scene="main", playing=True, paused=False, time_scale=1, frame=self.seq,
                public={"speed": self.pos, "unknown": None, "bad": None},
                reader_errors=[{"reader": "bad", "error": "failure"}], frame_ref=str(capture) if with_frame else None)

        async def act(self, **request):
            self.calls.append(request)
            self.t += 0.2
            self.pos += 1
            value = record([receipt()], duration=0.201)
            return [value.model_copy(update={"action_id": request["action_id"],
                                             "validated_against_seq": request["based_on_seq"]})]

        async def stop(self):
            return StopResult.model_validate(source.stop)

        async def aclose(self):
            pass

    client = Client()
    async def connect(game):
        return client
    async def no_delay(seconds):
        pass
    monkeypatch.setattr(three_session, "connect", connect)
    monkeypatch.setattr(three_session.asyncio, "sleep", no_delay)
    measured = asyncio.run(three_session.measure(source.declarations, "advance", 0, "", tmp_path))
    assert len(client.calls) == 4 and set(client.stages) == {"transforms"}
    assert all(call["action"] == "advance" and call["lease_s"] == 0.2 for call in client.calls)
    assert all(call["based_on_seq"] > 0 for call in client.calls)
    assert len(measured.attempts) == 2
    assert all(a["before_observation"]["reader_errors"] == [{"reader": "bad", "error": "failure"}]
               and a["after_observation"]["public"]["unknown"] is None for a in measured.attempts)
    compiled = compile_study(measured)
    assert compiled.manifest.entity_id == "entity"
    assert compiled.manifest.actions[0].execution_supported is True
    assert compiled.manifest.actions[0].hold_effect is True
    assert (tmp_path / "session.partial.jsonl").is_file()
