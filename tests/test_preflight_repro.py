"""The reproduction probe counts k of n valid attempts and never turns missing evidence into a result."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from playtest.core.bridge_client import BridgeError
from playtest.preflight import repro
from test_bridge_generic import BANNED, words


REAL_SLEEP = asyncio.sleep
STACK = "Widget.Refresh () (at Assets/Widget.cs:12)\nWidget.Update () (at Assets/Widget.cs:30)"
HIT = ("Exception", "NullReferenceException: Object reference not set to an instance of an object", STACK)
OTHER = ("Error", "unrelated failure", "Other.Method () (at Assets/Other.cs:3)")
PLAN = {
    "schema_version": 1, "control": "digit1", "hold_s": 0.1, "attempts": 3, "presses": 2,
    "settle_s": 1.0, "press_interval_s": 0.5, "window_s": 0.5,
    "predicate": {"type": "Exception", "message_contains": "NullReferenceException",
                  "stack_contains": "Widget.Refresh"},
}
PREDICATE = repro.Predicate("Exception", "NullReferenceException", "Widget.Refresh")


def document(**changes):
    value = json.loads(json.dumps(PLAN))
    value.update(changes)
    return value


def make_plan(**changes):
    return repro.parse_plan(json.dumps(document(**changes)))


@pytest.fixture
def fast(monkeypatch):
    """Every wait yields once and returns, so a run takes no wall-clock time but other tasks still run."""
    async def quick(_delay):
        await REAL_SLEEP(0)
    monkeypatch.setattr(repro.asyncio, "sleep", quick)


# --- plan ---------------------------------------------------------------------------------


def test_a_complete_plan_is_accepted_and_hashed_from_its_exact_bytes(tmp_path):
    raw = (json.dumps(PLAN, indent=2) + "\n").encode("utf-8")
    path = tmp_path / "plan.json"
    path.write_bytes(raw)
    plan, digest = repro.read_plan(path)
    assert digest == hashlib.sha256(raw).hexdigest()
    assert plan.action == "repro_digit1" and plan.control == "digit1"
    assert (plan.hold_s, plan.attempts, plan.presses) == (0.1, 3, 2)
    assert (plan.settle_s, plan.press_interval_s, plan.window_s) == (1.0, 0.5, 0.5)
    assert plan.as_dict() == PLAN
    # A byte-order mark from a Windows editor is tolerated and still part of the hashed bytes.
    path.write_bytes(b"\xef\xbb\xbf" + raw)
    assert repro.read_plan(path)[0] == plan
    assert repro.read_plan(path)[1] == hashlib.sha256(b"\xef\xbb\xbf" + raw).hexdigest()
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match="UTF-8"):
        repro.read_plan(path)


@pytest.mark.parametrize("key", list(PLAN))
def test_a_plan_missing_any_key_is_rejected(key):
    value = document()
    del value[key]
    with pytest.raises(ValueError, match="missing: " + key):
        repro.parse_plan(json.dumps(value))


def test_unknown_keys_are_rejected_at_both_levels():
    with pytest.raises(ValueError, match="plan has unknown keys: extra"):
        repro.parse_plan(json.dumps(document(extra=1)))
    with pytest.raises(ValueError, match="predicate has unknown keys: regex"):
        repro.parse_plan(json.dumps(document(predicate=dict(PLAN["predicate"], regex="x"))))
    with pytest.raises(ValueError, match="predicate is missing: stack_contains"):
        repro.parse_plan(json.dumps(document(predicate={"type": "Exception", "message_contains": "x"})))
    with pytest.raises(ValueError, match="predicate must be a JSON object"):
        repro.parse_plan(json.dumps(document(predicate="Exception")))


@pytest.mark.parametrize(("change", "message"), [
    ({"schema_version": 2}, "schema_version"),
    ({"schema_version": True}, "schema_version"),
    ({"schema_version": 1.0}, "schema_version"),
    ({"control": "Digit1"}, "control"),
    ({"control": "digit-1"}, "control"),
    ({"control": ""}, "control"),
    ({"control": "digit1 "}, "control"),
    ({"control": 1}, "control"),
    ({"control": "1"}, "bare number"),
    ({"control": "12"}, "bare number"),
    ({"hold_s": 0.01}, "hold_s must be between 0.02 and 2"),
    ({"hold_s": 2.5}, "hold_s must be between 0.02 and 2"),
    ({"hold_s": True}, "hold_s must be a finite number"),
    ({"hold_s": "0.1"}, "hold_s must be a finite number"),
    ({"hold_s": None}, "hold_s must be a finite number"),
    ({"attempts": 0}, "attempts must be between 1 and 20"),
    ({"attempts": 21}, "attempts must be between 1 and 20"),
    ({"attempts": 2.0}, "attempts must be a whole number"),
    ({"attempts": True}, "attempts must be a whole number"),
    ({"attempts": "2"}, "attempts must be a whole number"),
    ({"presses": 0}, "presses must be between 1 and 10"),
    ({"presses": 11}, "presses must be between 1 and 10"),
    ({"settle_s": 0.4}, "settle_s must be between 0.5"),
    ({"settle_s": 61}, "settle_s must be between 0.5"),
    ({"press_interval_s": 0.1}, "press_interval_s must be between 0.2"),
    ({"window_s": 0.05}, "window_s must be between 0.1"),
    ({"window_s": 61}, "window_s must be between 0.1"),
    ({"predicate": dict(PLAN["predicate"], type="")}, "predicate.type"),
    ({"predicate": dict(PLAN["predicate"], message_contains="   ")}, "predicate.message_contains"),
    ({"predicate": dict(PLAN["predicate"], stack_contains=None)}, "predicate.stack_contains"),
    ({"predicate": dict(PLAN["predicate"], type=7)}, "predicate.type"),
])
def test_out_of_range_or_mistyped_values_are_rejected(change, message):
    with pytest.raises(ValueError, match=message):
        repro.parse_plan(json.dumps(document(**change)))


def test_the_plan_text_itself_must_be_strict_json():
    text = json.dumps(PLAN)
    with pytest.raises(ValueError, match="repeats a key: control"):
        repro.parse_plan(text[:-1] + ', "control": "digit2"}')
    for constant in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError, match="not JSON"):
            repro.parse_plan(text.replace('"hold_s": 0.1', '"hold_s": ' + constant))
    with pytest.raises(ValueError, match="finite number"):
        repro.parse_plan(text.replace('"hold_s": 0.1', '"hold_s": 1e999'))
    with pytest.raises(ValueError, match="not valid JSON"):
        repro.parse_plan("{")
    with pytest.raises(ValueError, match="plan must be a JSON object"):
        repro.parse_plan("[]")


# --- windows and matching ------------------------------------------------------------------


def event(first, last=None, *, kind="Exception", message=HIT[1], stack=STACK, count=None):
    last = first if last is None else last
    return {"type": kind, "message": message, "stack": stack, "first_sequence": first, "last_sequence": last,
            "count": last - first + 1 if count is None else count}


def monitor(observed, events, *, limit=256, truncated=False, active=True):
    return {"active": active, "limit": limit, "truncated": truncated, "observed": observed, "events": events}


def test_a_matching_event_makes_the_window_matched_and_keeps_it_readable():
    window = repro.error_window(monitor(6, [event(5, 6)]), 4, PREDICATE)
    assert window["available"] is True
    assert (window["count"], window["sampled"], window["complete"]) == (2, 2, True)
    assert (window["matching_events"], window["matching_count"], window["non_matching_count"]) == (1, 2, 0)
    assert repro.press_match(window) is True
    kept = window["events"][0]
    assert (kept["type"], kept["count"], kept["first_sequence"], kept["last_sequence"]) == ("Exception", 2, 5, 6)
    assert kept["message"].startswith("NullReferenceException") and kept["matches"] is True
    assert kept["stack_lines"] == STACK.splitlines() and kept["stack_lines_total"] == 2


def test_a_complete_window_without_a_match_is_unmatched_and_counts_what_it_saw():
    quiet = repro.error_window(monitor(4, [event(1, 4)]), 4, PREDICATE)
    assert (quiet["count"], quiet["complete"], quiet["events"]) == (0, True, [])
    assert repro.press_match(quiet) is False
    other = repro.error_window(monitor(7, [event(5, 7, kind="Error", message="x", stack="Other.Method ()")]), 4,
                               PREDICATE)
    assert (other["count"], other["matching_count"], other["non_matching_count"]) == (3, 0, 3)
    assert repro.press_match(other) is False


@pytest.mark.parametrize("changes", [
    {"kind": "Error"},
    {"message": "InvalidOperationException: something else"},
    {"stack": "Other.Method () (at Assets/Other.cs:3)"},
    {"stack": None},
])
def test_every_part_of_the_predicate_must_hold_for_one_event(changes):
    window = repro.error_window(monitor(1, [event(1, **changes)]), 0, PREDICATE)
    assert window["events"][0]["matches"] is False
    assert repro.press_match(window) is False


def test_matching_is_by_exact_case_sensitive_substring():
    assert repro.event_matches(PREDICATE, event(1))
    assert not repro.event_matches(PREDICATE, event(1, message="nullreferenceexception"))
    assert not repro.event_matches(PREDICATE, event(1, kind="exception"))


def test_an_incomplete_window_with_no_retained_match_is_unknown_not_unmatched():
    # 300 errors were counted, but the monitor only kept the newest 256 distinct ones.
    kept = [event(45 + index, kind="Error", message=f"m{index}", stack="Other.Method ()") for index in range(256)]
    window = repro.error_window(monitor(300, kept, truncated=True), 0, PREDICATE)
    assert (window["count"], window["sampled"], window["complete"]) == (300, 256, False)
    assert (window["unretained_count"], window["monitor_truncated"]) == (44, True)
    assert "truncated" in window["note"]
    assert repro.press_match(window) is None


def test_an_incomplete_window_with_a_retained_match_is_matched():
    kept = [event(45 + index, kind="Error", message=f"m{index}", stack="Other.Method ()") for index in range(255)]
    kept.append(event(300))
    window = repro.error_window(monitor(300, kept, truncated=True), 0, PREDICATE)
    assert window["complete"] is False and window["matching_events"] == 1
    assert repro.press_match(window) is True


def test_a_truncated_monitor_still_measures_a_later_window_its_events_cover():
    kept = [event(45 + index, kind="Error", message=f"m{index}", stack="x") for index in range(256)]
    window = repro.error_window(monitor(300, kept, truncated=True), 299, PREDICATE)
    assert (window["count"], window["sampled"], window["complete"]) == (1, 1, True)
    assert repro.press_match(window) is False


@pytest.mark.parametrize(("bad", "previous"), [
    (monitor(5, [], active=False), 0),
    (None, 0),
    ("monitor", 0),
    (monitor(3, []), 5),
    (monitor(5, [event(0)]), 0),
    (monitor(1, [event(1, 3)]), 0),
    (monitor(5, []), None),
])
def test_a_monitor_that_is_off_or_inconsistent_gives_an_unknown_window_never_zero(bad, previous):
    window = repro.error_window(bad, previous, PREDICATE)
    assert window["available"] is False and window["reason"]
    assert window["count"] is None and window["complete"] is None and window["events"] == []
    assert (window["matching_count"], window["non_matching_count"], window["unretained_count"]) == (None,) * 3
    assert repro.press_match(window) is None


def test_a_run_that_began_before_the_window_only_contributes_its_own_occurrences():
    window = repro.error_window(monitor(6, [event(1, 6)]), 3, PREDICATE)
    assert (window["count"], window["sampled"], window["complete"]) == (3, 3, True)
    kept = window["events"][0]
    # The sequences are the window's own (it starts after error 3); the monitor's run is kept beside them.
    assert (kept["count"], kept["first_sequence"], kept["last_sequence"], kept["run_first_sequence"]) == (3, 4, 6, 1)


def test_stack_matching_uses_the_whole_stack_but_the_report_keeps_eight_lines():
    lines = [f"Frame{index} ()" for index in range(12)]
    deep = event(1, stack="\n".join(lines))
    predicate = repro.Predicate("Exception", "NullReferenceException", "Frame11")
    window = repro.error_window(monitor(1, [deep]), 0, predicate)
    assert repro.press_match(window) is True
    assert window["events"][0]["stack_lines"] == lines[:repro.STACK_LINES]
    assert window["events"][0]["stack_lines_total"] == 12


def test_listed_events_are_capped_but_the_matching_one_and_the_counts_survive():
    many = [event(1 + index, kind="Error", message=f"m{index}", stack="x") for index in range(40)]
    many.append(event(41))
    window = repro.error_window(monitor(41, many), 0, PREDICATE)
    assert len(window["events"]) == repro.LISTED_EVENTS and window["events_total"] == 41
    assert any(item["matches"] for item in window["events"])
    assert (window["count"], window["matching_count"], window["non_matching_count"]) == (41, 1, 40)
    assert [item["first_sequence"] for item in window["events"]] == sorted(
        item["first_sequence"] for item in window["events"])


# --- a fake game ---------------------------------------------------------------------------


def record(status, release_verified, reject_reason=None):
    payload = {"status": status, "release_verified": release_verified, "reject_reason": reject_reason}
    return SimpleNamespace(
        status=SimpleNamespace(value=status), release_verified=release_verified,
        reject_reason=SimpleNamespace(value=reject_reason) if reject_reason else None,
        applied_duration_s=0.11 if status == "completed" else None, input_start_wall_s=1000.0,
        input_end_wall_s=1000.11, model_dump=lambda **_: payload)


class FakeGame:
    """A scene that logs scripted errors, and a monitor that counts them the way the Editor's does."""

    def __init__(self, *, idle=None, press=None, outcome=None, limit=256, snapshot=None):
        self.idle = idle or (lambda reload: [])
        self.press = press or (lambda reload, number: [])
        self.outcome = outcome or (lambda reload, number: "completed")
        self.limit = limit
        self.snapshot = snapshot
        self.observed = 0
        self.events = []
        self.truncated = False
        self.reloads = 0
        self.presses = 0
        self.reads = 0
        self.connections = 0
        self.closed = 0
        self.act_calls = []

    def log(self, kind, message, stack):
        self.observed += 1
        last = self.events[-1] if self.events else None
        if last is not None and (last["type"], last["message"], last["stack"]) == (kind, message, stack):
            last["last_sequence"], last["count"] = self.observed, last["count"] + 1
            return
        if len(self.events) >= self.limit:
            self.events.pop(0)
            self.truncated = True
        self.events.append({"type": kind, "message": message, "stack": stack, "first_sequence": self.observed,
                            "last_sequence": self.observed, "count": 1})

    def monitor(self):
        if self.snapshot is not None:
            return self.snapshot
        return {"active": True, "observed": self.observed, "limit": self.limit, "truncated": self.truncated,
                "events": [dict(item) for item in self.events]}

    def reload(self):
        self.reloads += 1
        self.presses = 0


class FakeClient:
    def __init__(self, game):
        self.game = game
        self.session = f"session-{game.connections}"

    async def inspect(self, stage):
        assert stage == "transforms"
        game = self.game
        game.reads += 1
        if game.reads == 2:
            # The idle window is the settle between the first read after the reload and the second.
            for item in game.idle(game.reloads):
                game.log(*item)
        return SimpleNamespace(
            stage=stage, errors=[], wall_utc=datetime(2026, 1, 1, tzinfo=timezone.utc), caps={},
            data={"frame": 10 + game.reads, "real_time_s": 1.5, "game_time_s": 1.5,
                  "runtime_errors": game.monitor()})

    async def observe_once(self):
        return SimpleNamespace(seq=7, scene="Main", playing=True, paused=False, time_scale=1.0, frame=12,
                               clock_mode=SimpleNamespace(value="realtime"))

    async def act(self, **kwargs):
        game = self.game
        game.act_calls.append(kwargs)
        game.presses += 1
        outcome = game.outcome(game.reloads, game.presses)
        if outcome == "rejected":
            return [record("rejected", None, reject_reason="busy")]
        for item in game.press(game.reloads, game.presses):
            game.log(*item)
        return [record("completed", outcome != "unverified")]

    async def aclose(self):
        self.game.closed += 1
        return SimpleNamespace(release_verified=True, model_dump=lambda **_: {"release_verified": True})


def done_document(plan, reload_number, **changes):
    value = {"method": "scene_reload", "reloaded": True, "number": reload_number,
             "scene_path": "Assets/Scenes/Main.unity",
             "action": {"name": plan.action, "control": plan.control, "default_lease_s": plan.hold_s},
             "requested_at_utc": "2026-01-01T00:00:00Z", "completed_at_utc": "2026-01-01T00:00:01Z",
             "game_time_s": 1.0, "real_time_s": 2.0, "frame": 30}
    value.update(changes)
    return value


def hooks(game, plan):
    async def reload(number):
        game.reload()
        return repro._host_evidence(done_document(plan, number), plan, number)

    async def connect():
        game.connections += 1
        game.reads = 0
        return FakeClient(game)

    return reload, connect


def run(plan, game, *, reload=None, connect=None):
    default_reload, default_connect = hooks(game, plan)
    return asyncio.run(repro.measure(plan, "0" * 64, reload or default_reload, connect or default_connect))


# --- a run ----------------------------------------------------------------------------------


def test_a_run_through_the_reload_handshake_counts_k_of_n_and_presses_once_each(fast, tmp_path):
    plan = make_plan()
    # The first press reproduces the error after reloads 1 and 3; every second press logs something unrelated.
    game = FakeGame(press=lambda reload, number: ([HIT] if number == 1 and reload in (1, 3) else [])
                    + ([OTHER] if number == 2 else []))
    requests = []

    async def host():
        served = 0
        while served < plan.attempts:
            request_path, done_path = repro.reload_paths(tmp_path, served + 1)
            if request_path.exists():
                requests.append(json.loads(request_path.read_text(encoding="utf-8")))
                game.reload()
                pending = done_path.with_name(done_path.name + ".writing")
                pending.write_text(json.dumps(done_document(plan, served + 1)), encoding="utf-8")
                pending.replace(done_path)
                served += 1
            await REAL_SLEEP(0)

    async def reload(number):
        return await repro.request_reload(tmp_path, plan, number, 10.0)

    async def connect():
        game.connections += 1
        game.reads = 0
        return FakeClient(game)

    async def scenario():
        return await asyncio.gather(repro.measure(plan, "a" * 64, reload, connect), host())

    report, _ = asyncio.run(scenario())

    assert report["status"] == "measured", report["status_reason"]
    summary = report["summary"]
    assert (summary["planned"], summary["attempted"], summary["valid"]) == (3, 3, 3)
    assert (summary["k"], summary["n"]) == (2, 3)
    assert summary["presses"] == {"scope": "presses in valid attempts", "reported": 6, "valid": 6, "matched": 2,
                                  "unmatched": 4, "unknown": 0}
    assert (summary["non_matching_errors"], summary["unretained_errors"]) == (3, 0)
    assert summary["invalid_attempts"] == []
    assert [item["number"] for item in requests] == [1, 2, 3]
    assert all(item["action"] == {"name": "repro_digit1", "control": "digit1", "requested_hold_s": 0.1}
               for item in requests)
    # Every attempt reloaded, reconnected, and closed its own session.
    assert (game.reloads, game.connections, game.closed) == (3, 3, 3)
    assert len({attempt["session"] for attempt in report["attempts"]}) == 3
    assert all(attempt["stop"] == {"release_verified": True} for attempt in report["attempts"])
    assert [attempt["first_press_matched"] for attempt in report["attempts"]] == [True, False, True]
    # Exactly one act per press, with the plan's action, lease and the observation it was built from.
    assert len(game.act_calls) == 6
    for call in game.act_calls:
        assert call["action"] == "repro_digit1" and call["lease_s"] == 0.1
        assert call["based_on_seq"] == 7 and call["max_age_s"] == 2.0 and call["choice_id"] == call["action_id"]
    assert len({call["action_id"] for call in game.act_calls}) == 6
    first = report["attempts"][0]
    assert first["reload"]["number"] == 1 and first["reload"]["scene_path"] == "Assets/Scenes/Main.unity"
    assert first["idle"]["count"] == 0 and first["idle"]["complete"] is True
    hit = first["presses"][0]
    assert hit["matched"] is True and hit["window"]["events"][0]["message"].startswith("NullReferenceException")
    assert hit["status"] == "completed" and hit["release_verified"] is True
    assert hit["observation"]["paused"] is False
    # The report states what it measured under.
    assert report["plan"] == plan.as_dict() and report["plan_sha256"] == "a" * 64
    assert report["reset"] == "scene_reload" and report["is_s1_turn"] is False and report["model_calls"] == 0
    assert report["clock"]["stated"]["mode"] == "real_time"
    assert report["clock"]["observed"]["paused"] == [False] and report["clock"]["observed"]["time_scale"] == [1.0]
    assert report["error"] is None
    for key in ("started_wall_utc", "finished_wall_utc"):
        assert isinstance(report["timestamps"][key], str)
    json.dumps(report, allow_nan=False)


def test_idle_errors_make_the_attempt_invalid_keep_their_events_and_send_no_input(fast):
    plan = make_plan(presses=1)
    game = FakeGame(idle=lambda reload: [OTHER] if reload == 2 else [], press=lambda reload, number: [HIT])
    report = run(plan, game)
    second = report["attempts"][1]
    assert second["valid"] is False and second["presses"] == []
    assert second["idle"]["count"] == 1 and second["idle"]["events"][0]["message"] == "unrelated failure"
    assert second["reasons"] == ["the game logged 1 runtime error(s) in the idle window, before any input"]
    assert second["first_press_matched"] is None
    assert len(game.act_calls) == 2
    summary = report["summary"]
    assert (summary["valid"], summary["n"], summary["k"]) == (2, 2, 2)
    assert summary["invalid_attempts"] == [{"number": 2, "reasons": second["reasons"]}]
    assert report["status"] == "measured_with_gaps"


def test_an_unmeasurable_idle_window_makes_the_attempt_invalid_not_clean(fast):
    plan = make_plan(attempts=2)
    game = FakeGame(snapshot={"active": False, "observed": None, "limit": 256, "truncated": None, "events": []})
    report = run(plan, game)
    assert report["status"] == "inconclusive"
    assert all(attempt["valid"] is False and attempt["presses"] == [] for attempt in report["attempts"])
    assert all(attempt["idle"]["count"] is None for attempt in report["attempts"])
    assert "idle window is unknown" in report["attempts"][0]["reasons"][0]
    assert report["summary"]["k"] is None and report["summary"]["n"] == 0
    assert report["summary"]["non_matching_errors"] is None
    assert game.act_calls == []


def test_a_first_press_window_that_cannot_say_whether_the_error_appeared_is_not_imputed(fast):
    plan = make_plan(attempts=2, presses=1)
    flood = [("Error", f"flood {index}", "x") for index in range(300)]
    game = FakeGame(press=lambda reload, number: flood if reload == 1 else [HIT])
    report = run(plan, game)
    first = report["attempts"][0]
    assert first["presses"][0]["window"]["complete"] is False and first["presses"][0]["matched"] is None
    assert first["valid"] is False and first["first_press_matched"] is None
    assert "unknown" in first["reasons"][0]
    assert (report["summary"]["k"], report["summary"]["n"]) == (1, 1)
    assert report["status"] == "measured_with_gaps"


def test_a_rejected_press_is_recorded_once_and_never_retried_or_replaced(fast):
    plan = make_plan(attempts=2, presses=3)
    game = FakeGame(outcome=lambda reload, number: "rejected" if reload == 1 else "completed")
    report = run(plan, game)
    first = report["attempts"][0]
    assert first["valid"] is False
    assert [press["status"] for press in first["presses"]] == ["rejected"]
    assert first["presses"][0]["reject_reason"] == "busy" and first["presses"][0]["executed"] is False
    assert "not retried or replaced" in first["presses"][0]["reason"]
    assert first["reasons"][0].startswith("the first press is not a measurement")
    # One act in the rejected attempt, three in the other: nothing was resent or replaced.
    assert len(game.act_calls) == 1 + 3
    assert {call["action"] for call in game.act_calls} == {"repro_digit1"}
    assert (report["summary"]["valid"], report["summary"]["n"]) == (1, 1)


def test_an_unverified_release_makes_the_attempt_invalid_and_stops_its_pressing(fast):
    plan = make_plan(attempts=2, presses=3)
    game = FakeGame(outcome=lambda reload, number: "unverified" if reload == 1 else "completed",
                    press=lambda reload, number: [HIT])
    report = run(plan, game)
    first = report["attempts"][0]
    assert first["valid"] is False and len(first["presses"]) == 1
    press = first["presses"][0]
    assert press["status"] == "completed" and press["release_verified"] is False and press["executed"] is False
    assert press["matched"] is None, "a press that was not verified cannot count, even if the error appeared"
    assert press["window"]["matching_events"] == 1, "the window is still shown"
    assert "release was not verified" in press["reason"]
    assert len(game.act_calls) == 1 + 3
    assert report["summary"]["k"] == 1 and report["summary"]["n"] == 1


def test_a_later_rejected_press_stops_pressing_but_leaves_the_attempt_valid(fast):
    plan = make_plan(attempts=1, presses=3)
    game = FakeGame(outcome=lambda reload, number: "rejected" if number == 2 else "completed",
                    press=lambda reload, number: [HIT])
    report = run(plan, game)
    attempt = report["attempts"][0]
    assert attempt["valid"] is True and attempt["first_press_matched"] is True
    assert [press["status"] for press in attempt["presses"]] == ["completed", "rejected"]
    assert len(game.act_calls) == 2
    assert report["summary"]["presses"]["reported"] == 2 and report["summary"]["presses"]["valid"] == 1


def test_a_failed_reload_ends_the_run_and_keeps_the_attempts_already_made(fast):
    plan = make_plan()
    game = FakeGame()
    default_reload, connect = hooks(game, plan)

    async def reload(number):
        if number == 2:
            raise TimeoutError("scene reload 2 did not complete within 1 s")
        return await default_reload(number)

    report = run(plan, game, reload=reload, connect=connect)
    assert [attempt["number"] for attempt in report["attempts"]] == [1, 2]
    second = report["attempts"][1]
    assert second["valid"] is False and second["reload"] is None
    assert second["reasons"][0] == "the scene reload did not complete"
    assert "scene reload 2 did not complete" in second["error"]
    assert report["error"].startswith("attempt 2: ")
    assert report["status"] == "measured_with_gaps" and report["summary"]["attempted"] == 2
    assert game.connections == 1 and game.closed == 1


def fail_on_call(client, method, call_number, message="the bridge went away"):
    """Make the `call_number`th call of one client method raise, like a bridge that drops mid-run."""
    real = getattr(client, method)
    calls = []

    async def wrapper(*args, **kwargs):
        calls.append(1)
        if len(calls) == call_number:
            raise BridgeError("closed", message)
        return await real(*args, **kwargs)

    setattr(client, method, wrapper)


def test_a_bridge_failure_mid_press_ends_the_run_with_a_report_and_closes_the_session(fast):
    plan = make_plan(attempts=3)
    game = FakeGame()
    reload, connect = hooks(game, plan)

    async def broken_connect():
        client = await connect()
        if game.connections == 2:
            fail_on_call(client, "act", 1)
        return client

    report = run(plan, game, reload=reload, connect=broken_connect)
    assert [attempt["number"] for attempt in report["attempts"]] == [1, 2]
    second = report["attempts"][1]
    assert second["valid"] is False and "the bridge went away" in second["error"]
    assert second["presses"][0]["reason"] == "the press did not finish" and second["presses"][0]["valid"] is False
    assert second["reasons"][0].startswith("the first press is not a measurement")
    assert second["reasons"][-1].startswith("the probe stopped: ")
    assert game.closed == 2, "the failed attempt's session was still closed"
    assert report["status"] == "measured_with_gaps"
    json.dumps(report, allow_nan=False)


def test_a_failure_after_the_first_press_ends_the_run_but_keeps_that_measurement(fast):
    plan = make_plan(attempts=3, presses=2)
    game = FakeGame(press=lambda reload, number: [HIT])
    reload, connect = hooks(game, plan)

    async def broken_connect():
        client = await connect()
        if game.connections == 2:
            # The first press is sent and measured; the bridge goes away before the second can be observed.
            fail_on_call(client, "observe_once", 2)
        return client

    report = run(plan, game, reload=reload, connect=broken_connect)
    assert [attempt["number"] for attempt in report["attempts"]] == [1, 2]
    second = report["attempts"][1]
    assert second["valid"] is True and second["reasons"] == [] and second["first_press_matched"] is True
    assert "the bridge went away" in second["error"]
    assert [press["valid"] for press in second["presses"]] == [True, False]
    assert second["presses"][1]["status"] is None and second["presses"][1]["reason"] == "the press did not finish"
    assert game.closed == 2, "the session that failed was still closed"
    summary = report["summary"]
    assert (summary["k"], summary["n"], summary["presses"]["reported"], summary["presses"]["valid"]) == (2, 2, 3, 3)
    assert report["status"] == "measured_with_gaps"
    assert report["error"].startswith("attempt 2: ") and "ended early" in report["status_reason"]


def test_an_error_in_the_last_attempt_is_still_a_gap_not_a_clean_measurement(fast):
    plan = make_plan(attempts=1, presses=2)
    game = FakeGame(press=lambda reload, number: [HIT])
    reload, connect = hooks(game, plan)

    async def broken_connect():
        client = await connect()
        fail_on_call(client, "act", 2, "no execution record")
        return client

    report = run(plan, game, reload=reload, connect=broken_connect)
    assert report["summary"]["valid"] == 1 and report["summary"]["n"] == report["summary"]["planned"]
    assert report["status"] == "measured_with_gaps", "every planned attempt is valid, but the run hit an error"
    assert "no execution record" in report["error"] and "ended early" in report["status_reason"]


def test_a_session_that_cannot_be_closed_cannot_lose_the_report(fast):
    plan = make_plan(attempts=2, presses=1)
    game = FakeGame(press=lambda reload, number: [HIT])
    reload, connect = hooks(game, plan)

    async def failing_close():
        client = await connect()

        async def aclose():
            raise RuntimeError("socket already gone")
        client.aclose = aclose
        return client

    report = run(plan, game, reload=reload, connect=failing_close)
    assert report["status"] == "measured" and report["summary"]["n"] == 2
    assert all(attempt["stop"] is None for attempt in report["attempts"])
    assert report["attempts"][0]["stop_error"] == "RuntimeError: socket already gone"
    json.dumps(report, allow_nan=False)


def test_every_failure_leaves_a_report_with_no_attempt_valid_as_inconclusive(fast):
    plan = make_plan(attempts=2)
    game = FakeGame()

    async def reload(number):
        raise ValueError("the Editor harness did not report a completed scene reload")

    report = run(plan, game, reload=reload)
    assert report["status"] == "inconclusive" and report["summary"]["n"] == 0 and report["summary"]["k"] is None
    assert len(report["attempts"]) == 1 and game.act_calls == []


# --- the handshake ----------------------------------------------------------------------------


def test_the_handshake_is_published_whole_and_refuses_files_that_already_exist(fast, tmp_path):
    plan = make_plan()
    request_path, done_path = repro.reload_paths(tmp_path, 4)
    assert request_path.name == "reload-4.request.json" and done_path.name == "reload-4.done.json"
    done_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="already exist"):
        asyncio.run(repro.request_reload(tmp_path, plan, 4, 1.0))
    assert not request_path.exists(), "a refused request is never published"


def test_a_reload_that_never_completes_times_out_once(fast, tmp_path):
    with pytest.raises(TimeoutError, match="scene reload 1 did not complete"):
        asyncio.run(repro.request_reload(tmp_path, make_plan(), 1, 0.05))
    request_path, _ = repro.reload_paths(tmp_path, 1)
    assert json.loads(request_path.read_text(encoding="utf-8"))["number"] == 1


@pytest.mark.parametrize(("changes", "message"), [
    ({"method": "other"}, "completed scene reload"),
    ({"reloaded": False}, "completed scene reload"),
    ({"number": 2}, "answered reload 2, not reload 1"),
    ({"number": True}, "answered reload True"),
    ({"action": {"name": "repro_digit2", "control": "digit1", "default_lease_s": 0.1}}, "action name or physical"),
    ({"action": {"name": "repro_digit1", "control": "digit2", "default_lease_s": 0.1}}, "action name or physical"),
    ({"action": {"name": "repro_digit1", "control": "digit1", "default_lease_s": 0.3}}, "lease does not match"),
    ({"action": {"name": "repro_digit1", "control": "digit1"}}, "lease does not match"),
    ({"action": None}, "action name or physical"),
    ({"scene_path": ""}, "did not report the reloaded scene"),
])
def test_the_completion_must_name_this_reload_and_the_hosted_action(changes, message):
    plan = make_plan()
    assert repro._host_evidence(done_document(plan, 1), plan, 1)["completed"] is True
    with pytest.raises(ValueError, match=message):
        repro._host_evidence(done_document(plan, 1, **changes), plan, 1)
    with pytest.raises(ValueError, match="not a JSON object"):
        repro._host_evidence([], plan, 1)


def test_a_completion_that_is_not_readable_json_is_an_error_not_a_guess(fast, tmp_path):
    plan = make_plan()

    async def host():
        _, done_path = repro.reload_paths(tmp_path, 1)
        while not repro.reload_paths(tmp_path, 1)[0].exists():
            await REAL_SLEEP(0)
        done_path.write_text("{ not json", encoding="utf-8")

    async def scenario():
        return await asyncio.gather(repro.request_reload(tmp_path, plan, 1, 5.0), host())

    with pytest.raises(ValueError, match="not readable JSON"):
        asyncio.run(scenario())


# --- status and the report ----------------------------------------------------------------------


def test_status_is_measured_only_when_every_planned_attempt_is_valid(fast):
    plan = make_plan(attempts=2, presses=1)
    assert run(plan, FakeGame())["status"] == "measured"
    gaps = run(plan, FakeGame(idle=lambda reload: [OTHER] if reload == 1 else []))
    assert gaps["status"] == "measured_with_gaps" and gaps["summary"]["n"] == 1
    none = run(plan, FakeGame(idle=lambda reload: [OTHER]))
    assert none["status"] == "inconclusive" and none["summary"]["valid"] == 0
    assert none["summary"]["invalid_attempts"][0]["number"] == 1
    # No valid attempt means no measured rate: k is unknown rather than zero.
    assert none["summary"]["k"] is None and none["summary"]["n"] == 0


def test_a_non_finite_number_becomes_null_so_the_report_is_always_valid_json():
    cleaned = repro._json_safe({"a": float("nan"), "b": [float("inf"), 1.5], "c": {"d": float("-inf")}})
    assert cleaned == {"a": None, "b": [None, 1.5], "c": {"d": None}}
    json.dumps(cleaned, allow_nan=False)


# --- the command line ---------------------------------------------------------------------------


def cli_paths(tmp_path):
    project = tmp_path / "game"
    project.mkdir()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(PLAN), encoding="utf-8")
    return project, plan, tmp_path / "out" / "repro.json", tmp_path / "out" / "handshake"


def arguments(project, plan, output, handshake):
    return ["--project", str(project), "--plan", str(plan), "--output", str(output),
            "--handshake-dir", str(handshake)]


def test_the_command_line_refuses_existing_paths_and_paths_inside_the_project(tmp_path):
    project, plan, output, handshake = cli_paths(tmp_path)
    assert repro._parse_args(arguments(project, plan, output, handshake)).output == output

    def refused(*args):
        with pytest.raises(SystemExit) as raised:
            repro._parse_args(args)
        assert raised.value.code == 2

    refused(*arguments(project, plan, project / "repro.json", handshake))
    refused(*arguments(project, plan, output, project / "handshake"))
    refused(*arguments(project, plan, project, handshake))
    refused(*arguments(project, tmp_path / "missing.json", output, handshake))
    refused(*arguments(tmp_path / "no-such-project", plan, output, handshake))
    refused(*arguments(project, plan, output, handshake), "--reload-timeout", "0")
    refused(*arguments(project, plan, output, handshake), "--reload-timeout", "nan")

    existing = tmp_path / "existing.json"
    existing.write_text("{}", encoding="utf-8")
    refused(*arguments(project, plan, existing, handshake))

    full = tmp_path / "full"
    full.mkdir()
    (full / "reload-1.request.json").write_text("{}", encoding="utf-8")
    refused(*arguments(project, plan, output, full))
    a_file = tmp_path / "a-file"
    a_file.write_text("", encoding="utf-8")
    refused(*arguments(project, plan, output, a_file))
    refused(*arguments(project, plan, handshake / "repro.json", handshake))

    empty = tmp_path / "empty"
    empty.mkdir()
    assert repro._parse_args(arguments(project, plan, output, empty)).handshake_dir == empty


def test_the_command_line_rejects_a_bad_plan_and_a_missing_bridge_before_running(tmp_path, monkeypatch):
    project, plan, output, handshake = cli_paths(tmp_path)

    async def forbidden(*_):
        raise AssertionError("nothing may run when setup fails")

    monkeypatch.setattr(repro, "_run", forbidden)
    plan.write_text(json.dumps(document(control="1")), encoding="utf-8")
    assert repro.main(arguments(project, plan, output, handshake)) == 1
    assert not output.exists() and not handshake.exists()

    plan.write_text(json.dumps(PLAN), encoding="utf-8")
    assert repro.main(arguments(project, plan, output, handshake)) == 1, "there is no bridge lock in the project"
    assert not output.exists()


def test_the_command_line_exits_zero_once_the_report_is_written_even_when_inconclusive(tmp_path, monkeypatch, fast):
    project, plan, output, handshake = cli_paths(tmp_path)
    monkeypatch.setattr(repro, "read_project_lock", lambda path: None)

    async def failing_run(project, plan, plan_sha256, handshake, timeout):
        async def reload(number):
            raise TimeoutError("scene reload 1 did not complete within 1 s")

        async def connect():
            raise AssertionError("no session without a reload")

        return await repro.measure(plan, plan_sha256, reload, connect)

    monkeypatch.setattr(repro, "_run", failing_run)
    assert repro.main(arguments(project, plan, output, handshake)) == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["status"] == "inconclusive" and saved["summary"]["attempted"] == 1
    assert saved["plan_sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()
    assert saved["sources"] == {"project": str(project.resolve()), "plan": str(plan.resolve())}
    assert handshake.is_dir()
    # The report path is new: a second run cannot overwrite it.
    with pytest.raises(SystemExit) as raised:
        repro.main(arguments(project, plan, output, tmp_path / "other"))
    assert raised.value.code == 2


def test_the_probe_names_no_game():
    """The probe is generic: its code, its tests and its script carry no game vocabulary."""
    root = Path(__file__).resolve().parents[1]
    found = []
    for path in (root / "src" / "playtest" / "preflight" / "repro.py", Path(__file__),
                 root / "scripts" / "unity" / "repro-runtime-error.ps1",
                 root / "bridge" / "Tests" / "PlayMode" / "ReproPlayModeTests.cs"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            hits = words(line) & BANNED
            if hits:
                found.append(f"{path.name}:{number}: {sorted(hits)}")
    assert not found, "game-specific words in the reproduction probe:\n" + "\n".join(found)
