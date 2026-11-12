import json
import os

import pytest

from catalog_game import CatalogGame
from loop_fakes import GameThread
from s1_fakes import Send, reply
from test_runner_session import Project, two_actions, with_fake_send

from playtest.contracts.bridge import Engine, ReaderError
from playtest.core.trace import read_trace
from playtest.preflight.catalog import export
from playtest.runner import cli


class PublicGame(CatalogGame):
    def __init__(self, *, publication, errors=(), **kwargs):
        super().__init__(**kwargs)
        self.publication = publication
        self.errors = errors

    def _status(self, seq, session):
        return super()._status(seq, session).model_copy(update={"engine": Engine.three, "unity_version": None})

    def _public(self):
        return {**super()._public(), **self.publication}

    def _observation(self, seq, session):
        return super()._observation(seq, session).model_copy(update={
            "reader_errors": [ReaderError(reader=name, error=reason) for name, reason in self.errors]
        })

    def _inspection(self, request, seq, session):
        assert request.stage == "transforms"
        return super()._inspection(request, seq, session)


@pytest.mark.parametrize("publication,errors,success", [
    ({"speed": None}, (), True),
    ({"speed": None}, (("speed", "failed"),), False),
    ({}, (), False),
])
def test_three_runner_uses_bridge_publication_before_provider(tmp_path, monkeypatch, publication, errors, success):
    for name in [key for key in os.environ if key.endswith("_API_KEY")]:
        monkeypatch.delenv(name)
    for name in ("UPA_SESSION_CATALOG", "UPA_SESSION_CATALOG_SHA256", "UPA_RUN_MANIFEST_ORIGIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BASETEN_API_KEY", "fake-test-key")
    with GameThread(PublicGame, catalog=export(two_actions()), publication=publication, errors=errors) as thread:
        files = Project(tmp_path, thread.port, calls=1)
        send = Send(files.out, reply("next_action", "finish"))
        with_fake_send(monkeypatch, send)
        code = cli.main(files.args(max_calls=1, tokens_per_minute=600000))
        assert all(req.stage == "transforms" for req in thread.game.inspect_requests)
    summary = json.loads(files.summary.read_text(encoding="utf-8"))
    if success:
        assert code == 0 and len(send.bodies) == 1
        assert summary["readers"]["installed"]["resolved"] == ["speed"]
        observed = [entry.body for entry in read_trace(files.out / "trace.jsonl") if entry.kind.value == "observation"]
        assert observed[0]["public"]["speed"] is None and observed[0]["reader_errors"] == []
    else:
        assert code == 1 and not send.bodies
        assert "reader" in summary["error"]
