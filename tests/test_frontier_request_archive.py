from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import pytest

from playtest.core.context import canonical
from playtest.frontier import request_archive
from playtest.frontier.request_archive import preserve_request
from playtest.runner import frontier_help
from test_frontier_help import _escalation_request
from test_frontier_publication import _helper, _send


def test_saved_body_is_the_exact_canonical_request_including_inline_media(tmp_path):
    body = {"model": "test", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Original objective \u2192 measured evidence."},
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AQID"}}]}]}
    before = copy.deepcopy(body)
    result = preserve_request(tmp_path, body)
    data = (tmp_path / result.relative_path).read_bytes()
    assert data == canonical(body).encode("utf-8")
    assert hashlib.sha256(data).hexdigest() == result.sha256
    assert json.loads(data) == before == body
    assert list((tmp_path / "requests/frontier").glob("*.tmp")) == []


def test_identical_body_reuses_the_artifact_without_rewriting_it(tmp_path):
    first = preserve_request(tmp_path, {"messages": ["same"]})
    path = tmp_path / first.relative_path
    before = path.stat().st_mtime_ns
    second = preserve_request(tmp_path, {"messages": ["same"]})
    assert first == second and path.stat().st_mtime_ns == before


def test_damaged_evidence_is_not_overwritten(tmp_path):
    result = preserve_request(tmp_path, {"messages": ["original"]})
    path = tmp_path / result.relative_path
    path.write_bytes(b"damaged")
    with pytest.raises(ValueError, match="content hash"):
        preserve_request(tmp_path, {"messages": ["original"]})
    assert path.read_bytes() == b"damaged"


def test_nonfinite_body_creates_no_artifact(tmp_path):
    with pytest.raises(ValueError):
        preserve_request(tmp_path, {"value": float("nan")})
    assert not (tmp_path / "requests").exists()


def test_failed_atomic_publication_leaves_no_partial_body(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(request_archive.os, "replace", fail)
    with pytest.raises(OSError, match="disk failure"):
        preserve_request(tmp_path, {"messages": ["original"]})
    assert list((tmp_path / "requests/frontier").iterdir()) == []


def test_the_actual_frontier_body_is_preserved_before_send_and_bound_to_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("META_API_KEY", "fake-header-secret-not-part-of-body")
    captured = []
    helper = None

    def send(body):
        captured.append(copy.deepcopy(body))
        sha = hashlib.sha256(canonical(body).encode()).hexdigest()
        path = helper.directory / "requests/frontier" / f"{sha}.json"
        assert path.read_bytes() == canonical(body).encode()
        return _send(body)

    helper, _, _ = _helper(tmp_path, send)
    asyncio.run(helper.run(_escalation_request(1, counter=0)))
    (row,) = helper.ledger.arm_rows()
    data = (helper.directory / row["request_body_path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == row["request_sha256"]
    assert json.loads(data) == captured[0]
    assert b"fake-header-secret-not-part-of-body" not in data


def test_archive_failure_prevents_dispatch_and_reservation(tmp_path, monkeypatch):
    sent = []
    helper, _, _ = _helper(tmp_path, lambda body: sent.append(body))

    def fail(*args):
        raise OSError("archive unavailable")

    monkeypatch.setattr(frontier_help, "preserve_request", fail)
    with pytest.raises(OSError, match="archive unavailable"):
        asyncio.run(helper.run(_escalation_request(1, counter=0)))
    assert sent == [] and helper.ledger.arm_rows() == []
