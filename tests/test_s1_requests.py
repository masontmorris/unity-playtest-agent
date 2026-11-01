"""The state each S1 request carried, kept in the run folder as the call is made.

The trace's `context` is the payload the core built; the compact menu replaces it with its own shorter
state, and that state appeared nowhere on disk. These tests hold the two claims the files make: the file
holds the body that was posted, and the digest the `s1_call` entry records is that file's own digest — so a
reviewer reading the file is reading what S1 was asked.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loop_fakes import choices
from s1_fakes import Send, approval, reply

from playtest.bench import call_budget
from playtest.bench.record import digest
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import ChoiceSet
from playtest.core.context import build_context, canonical
from playtest.runner import menu, s1_requests
from playtest.runner.options.memory import OutcomeMemory
from playtest.runner.s1 import SystemOneS1

ARM = "liquid-d1-ultra"
CELL = 0.5


def observation(*, position=None, seq: int = 7) -> Observation:
    public: dict[str, Any] = {}
    if position is not None:
        public["controlled"] = {"object_id": "o", "path": "p", "position": list(position),
                                "velocity": None, "source": "rigidbody"}
    return Observation(session="s", seq=seq, game_s=3.0,
                       wall_utc=datetime(2026, 11, 1, tzinfo=timezone.utc), clock_mode=ClockMode.realtime,
                       scene="a", playing=True, paused=False, time_scale=1.0, frame=9, public=public)


def context(**extra: Any) -> dict[str, Any]:
    built = build_context(objective="Exercise the controls.", observation=observation(position=(0.0, 0.0, 0.0)),
                          choices=ChoiceSet(set_version=4, built_from_seq=7, choices=choices()))
    built.update(extra)
    return built


def adapter(tmp_path: Path, *replies: Any, requests_dir: Path | str | None = ..., **options: Any):
    limits = approval()
    limits["arms"] = {ARM: limits["arms"]["mammoth-1"]}
    ledger = call_budget.Ledger(tmp_path, limits, ARM)
    send = Send(tmp_path, *replies)
    options.setdefault("arm", ARM)
    if "menu_memory" not in options and options.get("s1_context") == "compact-menu":
        options["menu_memory"] = OutcomeMemory(cell=CELL)
    if requests_dir is ...:
        requests_dir = tmp_path / "requests" / "s1"
    port = SystemOneS1(ledger, max_calls=options.pop("max_calls", 10), send=send,
                       requests_dir=requests_dir, **options)
    return port, send, ledger


def decide(port: SystemOneS1, ctx: dict[str, Any], **options: Any):
    return asyncio.run(port.decide(ctx, **options))


def stored(tmp_path: Path, turn: int, question: str) -> dict[str, Any]:
    path = s1_requests.request_file(tmp_path / "requests" / "s1", turn, question)
    assert path.is_file()
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_menu_state_s1_saw_is_the_file_not_the_loops_payload(tmp_path: Path):
    port, send, _ = adapter(tmp_path, reply(menu.PRIMARY, "c1"), s1_context="compact-menu")
    ctx = context()
    got = decide(port, ctx)
    body = send.bodies[0]
    record = stored(tmp_path, 0, menu.PRIMARY)
    # The file is the posted body, byte for byte: the menu's own state, which is not the payload the loop built.
    assert {key: record[key] for key in ("model", "state", "questions")} == body
    assert record["state"] != ctx and "observation" not in canonical(record["state"])
    assert record["turn"] == 0 and record["question"] == menu.PRIMARY
    assert record["question_ids"] == list(body["questions"])
    assert record["image_sha256"] is None and record["hashing"] == s1_requests.REPRODUCED
    # And the digest the trace records is this file's own: nothing else has to be kept to check it.
    assert record["sha256"] == got.calls[0].request_sha256 == digest(canonical(body))
    assert s1_requests.recorded_digest(record) == got.calls[0].request_sha256


def test_the_full_layout_keeps_the_body_it_hashed(tmp_path: Path):
    port, send, _ = adapter(tmp_path, reply("next_action", "c1"))
    got = decide(port, context())
    record = stored(tmp_path, 0, "next_action")
    assert record["state"] == send.bodies[0]["state"]
    assert s1_requests.recorded_digest(record) == got.calls[0].request_sha256


def test_an_attached_image_is_named_never_stored(tmp_path: Path):
    raw = b"a 640x480 frame"
    port, send, _ = adapter(tmp_path, reply(menu.PRIMARY, "c1"), s1_context="compact-menu", game_view=True)
    ctx = context(game_view="The attached image is the current game view.",
                  game_view_sha256=digest(raw), game_view_status="sent")
    got = decide(port, ctx, image=raw)
    record = stored(tmp_path, 0, menu.PRIMARY)
    encoded = base64.b64encode(raw).decode("ascii")
    assert record["image_sha256"] == digest(raw) and "images" not in record
    assert encoded not in canonical(record)
    # Its bytes are not in the file, so the digest is reproduced only with the image: the file says so.
    assert record["hashing"] == s1_requests.WITH_IMAGE
    assert s1_requests.recorded_digest(record) != got.calls[0].request_sha256
    assert got.calls[0].request_sha256 == digest(canonical(send.bodies[0]))


def test_a_rendered_request_says_its_digest_needs_the_rendering(tmp_path: Path):
    port, send, _ = adapter(tmp_path, reply("next_action", "c1"), option_order="manifest", order_seed=1)
    got = decide(port, context())
    record = stored(tmp_path, 0, "next_action")
    assert record["hashing"] == s1_requests.RENDERED
    assert send.texts[0] is not None  # the adapter posted the text it hashed
    assert s1_requests.recorded_digest(record) != got.calls[0].request_sha256


def test_a_run_that_cannot_store_the_request_still_makes_it_and_says_so(tmp_path: Path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    port, send, _ = adapter(tmp_path, reply("next_action", "c1"), requests_dir=blocked / "s1")
    got = decide(port, context())
    assert len(send.bodies) == 1 and got.answer["choice_id"] == "c1"
    assert "not stored" in got.calls[0].problem


def test_no_file_is_kept_for_a_call_that_was_never_made(tmp_path: Path):
    port, send, _ = adapter(tmp_path, reply("next_action", "c1"), max_calls=1)
    decide(port, context())
    refused = decide(port, context())
    assert refused.failure is not None and len(send.bodies) == 1
    assert not s1_requests.request_file(tmp_path / "requests" / "s1", 1, "next_action").exists()
