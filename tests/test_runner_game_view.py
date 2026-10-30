"""Freshness and provenance of the existing bridge JPEG window."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import hashlib
import os

from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.frontier import media
from playtest.runner.game_view import BridgeView, MAX_VIEW_AGE_S, jpeg_size

# A minimal marker stream for the dimension reader, with an SOF for 640 x 360.
JPEG = (b"\xff\xd8\xff\xe0\x00\x04ab\xff\xc0\x00\x0b\x08\x01\x68\x02\x80"
        b"\x01\x01\x11\x00\xff\xda\x00\x02\xff\xd9")
NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)
STATUS = SimpleNamespace(capture=True, capture_self_test="passed")


def observation(at=NOW):
    return Observation(session="s", seq=1, game_s=1.0, wall_utc=at, clock_mode=ClockMode.realtime,
                       scene="a", playing=True, paused=False, time_scale=1.0, frame=1)


def write_frame(project: Path, name: str, when: datetime, data=JPEG):
    directory = project / media.FRAME_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    ns = int(when.timestamp() * 1_000_000_000)
    os.utime(path, ns=(ns, ns))
    return path


def test_latest_pre_observation_frame_is_measured_and_hashed(tmp_path):
    write_frame(tmp_path, "f-000001.jpg", NOW - timedelta(seconds=.5))
    write_frame(tmp_path, "f-000002.jpg", NOW - timedelta(seconds=.1))
    write_frame(tmp_path, "f-000003.jpg", NOW + timedelta(seconds=.1))  # not yet observed
    view = BridgeView(tmp_path, STATUS, started_utc=NOW - timedelta(seconds=5)).sample(observation())
    assert view.image == JPEG and view.sha256 == hashlib.sha256(JPEG).hexdigest() and view.status == "sent"
    assert "640x360" in view.statement and "0.100000 s before" in view.statement
    assert "Capture time unknown" in view.statement and "write completed" in view.statement


def test_old_missing_invalid_and_unmeasurable_frames_are_not_sent(tmp_path):
    source = BridgeView(tmp_path, STATUS, started_utc=NOW - timedelta(seconds=10))
    assert source.sample(observation()).status == "missing"
    path = write_frame(tmp_path, "f-000001.jpg", NOW - timedelta(seconds=MAX_VIEW_AGE_S + .1))
    stale = source.sample(observation())
    assert stale.status == "stale" and stale.image is None and stale.sha256 is None and "too old" in stale.statement
    write_frame(tmp_path, "f-000002.jpg", NOW - timedelta(seconds=.2), b"not jpeg")
    missing = source.sample(observation())
    assert missing.status == "missing" and missing.image is None and "unknown" in missing.statement.lower()
    assert BridgeView(tmp_path, SimpleNamespace(capture=False), started_utc=NOW - timedelta(seconds=10)).sample(observation()).status == "missing"
    assert source.sample(observation().model_copy(update={"wall_utc": None})).status == "missing"
    path.unlink()
    assert jpeg_size(JPEG[:-2]) is None and jpeg_size(b"\xff\xd8\xff\xc0\x00\xff\xff\xd9") is None


def test_bound_is_inclusive_and_previous_run_is_excluded(tmp_path):
    write_frame(tmp_path, "f-000001.jpg", NOW - timedelta(seconds=1))
    assert BridgeView(tmp_path, STATUS, started_utc=NOW - timedelta(seconds=5)).sample(observation()).status == "sent"
    assert BridgeView(tmp_path, STATUS, started_utc=NOW - timedelta(seconds=.5)).sample(observation()).status == "missing"
