"""The `local-d1-2b` S1 arm's client wiring, and a hunt's credentials for the S1 arm its config names.

Offline: no server runs and no model is called. The server (scripts/local/d1_server.py) needs torch and the model
weights, so it is exercised by a live replay, not here.
"""

from __future__ import annotations

import json

import pytest

from playtest.bench import replay
from playtest.runner import hunt
from playtest.runner.image_transport import IMAGE_TRANSPORT, attach_images
from playtest.runner.pacing import default_tokens_per_minute

ARM = "local-d1-2b"


def test_the_local_arm_posts_liquids_body_to_its_own_url_bills_nothing_and_takes_the_images_array(monkeypatch):
    monkeypatch.setenv("LOCAL_D1_KEY", "token")
    monkeypatch.setenv("LOCAL_D1_URL", "http://127.0.0.1:8765/v1/decide")
    assert replay.credential_variables(ARM) == ("LOCAL_D1_KEY", "LOCAL_D1_URL")
    assert replay.endpoint(ARM) == ("http://127.0.0.1:8765/v1/decide", {"Authorization": "Bearer token"})
    assert replay.provider_of(ARM) == "local"
    assert IMAGE_TRANSPORT[ARM] == "images"
    assert attach_images({"state": {}}, ARM, b"\xff\xd8")["images"] == ["data:image/jpeg;base64,/9g="]
    assert default_tokens_per_minute(ARM) == 2_000_000


def test_a_hunt_asks_for_its_s1_arms_variables_and_reads_the_environment_when_there_is_no_env_file(
        tmp_path, monkeypatch):
    monkeypatch.setattr(hunt, "REPO", tmp_path)
    monkeypatch.setenv("META_API_KEY", "m")
    monkeypatch.setenv("LOCAL_D1_KEY", "k")
    monkeypatch.setenv("LOCAL_D1_URL", "http://127.0.0.1:8765")
    assert hunt.credentials(ARM) == {"META_API_KEY": "m", "LOCAL_D1_KEY": "k", "LOCAL_D1_URL": "http://127.0.0.1:8765"}
    monkeypatch.delenv("LOCAL_D1_URL")
    with pytest.raises(hunt.HuntError, match="LOCAL_D1_URL"):
        hunt.credentials(ARM)


def test_a_hunt_config_may_name_its_s1_arm_and_defaults_to_d1_ultra(tmp_path):
    base = {"project": str(tmp_path), "ref": "main"}
    for key in ("entity", "calibration", "manifest", "discovery", "approval"):
        (tmp_path / f"{key}.json").write_text("{}", encoding="utf-8")
        base[key] = str(tmp_path / f"{key}.json")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**base, "s1_arm": ARM}), encoding="utf-8")
    assert hunt.load_config(path)["s1_arm"] == ARM
    path.write_text(json.dumps(base), encoding="utf-8")
    assert hunt.load_config(path)["s1_arm"] == "liquid-d1-ultra"
    assert hunt._base({**hunt.DEFAULTS, **base, "s1_arm": ARM}, tmp_path)[-2:] == ["-S1Arm", ARM]
