"""The sourceless preflight probe (#121): arms differ only in code evidence, and spending stays bounded."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from playtest.preflight import packet as packet_mod
from playtest.roles import preflight

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probes" / "sourceless_preflight.py"
spec = importlib.util.spec_from_file_location("sourceless_preflight", SCRIPT)
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(probe)


def built(**discovery_overrides) -> packet_mod.Packet:
    discovery = {"fields": [{"id": "a"}], "source_excerpts": [{"path": "A.cs", "text": "class A {}"}],
                 "source_selection": "all", "il_write_sets": [{"field_id": "a", "direct_writers": []}],
                 "il_issues": [], "il_unlisted_writes": [], "il_complete": True, "il_scope": "player"}
    discovery.update(discovery_overrides)
    payload = {"discovery": {k: v for k, v in discovery.items() if v is not None},
               "discovery_caps": {"fields": {}, "source_files": {}, "source_characters": {},
                                  "source_file:A.cs": {}, "il_writes": {}, "il_types": {}},
               "discovery_projection_note": "production note", "offered_fields": [{"id": "field_00000"}]}
    return packet_mod.Packet(json.dumps(payload), preflight.Discovery({"field_00000": "int"}, {"key": "Button"}),
                             {"field_00000": "a"}, frozenset(), None)


def test_neutral_system_only_drops_the_promise_of_source():
    neutral = probe.neutral_system()
    assert probe.SOURCE_PHRASE not in neutral and probe.NEUTRAL_PHRASE in neutral
    assert neutral.replace(probe.NEUTRAL_PHRASE, probe.SOURCE_PHRASE) == preflight.SYSTEM


def test_neutral_system_refuses_a_changed_production_prompt(monkeypatch):
    monkeypatch.setattr(preflight, "SYSTEM", "a prompt without the phrase")
    with pytest.raises(ValueError, match="changed"):
        probe.neutral_system()


def test_arms_differ_only_in_code_evidence():
    base = built()
    arms = {arm: probe.variant(base, arm) for arm in probe.ARMS}
    payloads = {arm: json.loads(p.text) for arm, p in arms.items()}
    assert {"source_excerpts", "il_write_sets"} <= set(payloads["S"]["discovery"])
    assert "source_excerpts" not in payloads["I"]["discovery"] and "il_write_sets" in payloads["I"]["discovery"]
    assert not {"source_excerpts", "source_selection", *probe.IL_KEYS} & set(payloads["N"]["discovery"])
    assert set(payloads["I"]["discovery_caps"]) == {"fields", "il_writes", "il_types"}
    assert set(payloads["N"]["discovery_caps"]) == {"fields"}
    for arm, payload in payloads.items():
        assert payload["discovery_projection_note"] == probe.NOTES[arm]
        assert payload["discovery"]["fields"] == [{"id": "a"}]
        assert payload["offered_fields"] == [{"id": "field_00000"}]
        assert arms[arm].discovery is base.discovery and arms[arm].image is base.image


def test_a_packet_without_both_kinds_of_evidence_is_refused():
    with pytest.raises(ValueError, match="would not differ"):
        probe.variant(built(il_write_sets=None), "I")


def test_citations_are_checked_against_recorded_writers():
    writers = ["Asm:NS.Outer:Update()", "Asm:NS.Mover:set_speed(System.Single)"]
    assert probe.citation_status("Outer.Update", writers) == "consistent"
    assert probe.citation_status("NS.Outer.Update()", writers) == "consistent"
    assert probe.citation_status("Mover::set_speed", writers) == "consistent"
    assert probe.citation_status("Mover.Start", writers) == "not_recorded_writer"
    assert probe.citation_status("Update", writers) == "not_recorded_writer"
    assert probe.citation_status("Outer.Update", None) == "unknown"


def test_freshness_classes_keep_unknown_separate():
    assert probe.classify_freshness({"classification": "input"}) == "live"
    assert probe.classify_freshness({"classification": "both"}) == "live"
    assert probe.classify_freshness({"classification": "no_change_observed"}) == "not_live"
    assert probe.classify_freshness({"classification": "unknown"}) == "unknown"
    assert probe.classify_freshness(None) == "unknown"


def test_schedule_is_seeded_and_runs_every_arm_once_per_slot():
    rows = probe.schedule(121, 10, 1)
    assert rows == probe.schedule(121, 10, 1) and len(rows) == 33
    for phase, slot in {(p, s) for p, s, _ in rows}:
        assert sorted(a for p, s, a in rows if (p, s) == (phase, slot)) == sorted(probe.ARMS)
    assert {s for p, s, _ in rows if p == "pilot"} == {0}


def test_decision_rule_uses_the_predeclared_margin():
    assert probe.decide((-0.2, -0.8, 0.4), 4.0) == "acceptable"
    assert probe.decide((-1.5, -2.0, -1.1), 4.0) == "not_acceptable"
    assert probe.decide((-0.9, -1.6, -0.2), 4.0) == "inconclusive"
    assert probe.decide(None, 4.0) == "unknown"


def test_envelope_refuses_a_call_that_could_exceed_the_limit():
    arms = {arm: 3 for arm in probe.ARMS}
    envelope = probe.Envelope(3, Decimal("1"), arms, {a: Decimal("1") for a in probe.ARMS},
                              {a: Decimal("0.6") for a in probe.ARMS})
    envelope.reserve("S")
    assert envelope.charge("S", {"estimated_cost_usd": 0.5}) == Decimal("0.5")
    with pytest.raises(probe.EnvelopeExhausted, match="cost"):
        envelope.reserve("S")
    assert envelope.charge("I", {"estimated_cost_usd": None}) == Decimal("0.6")


def test_only_the_named_key_is_loaded_and_nothing_is_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("OTHER_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("OTHER_KEY=1\n# comment\nexport META_API_KEY=\"abc\"\n", encoding="utf-8")
    probe.load_key(env)
    assert os.environ["META_API_KEY"] == "abc" and "OTHER_KEY" not in os.environ
    assert capsys.readouterr() == ("", "")
    monkeypatch.delenv("META_API_KEY")


def approval(**changes):
    later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    value = {"approved_by": "owner", "approved_at_utc": "2026-10-29T00:00:00+00:00", "approval_scope": "test",
             "approval_source": "test", "model": "muse-spark-1.3", "max_calls": 10, "max_cost_usd": 2,
             "verified_balance_usd": None, "unverified_balance_override": {"approved_by_user": True, "reason": "test"},
             "arms": {arm: {"max_calls": 4, "per_call_max_usd": 0.3, "max_cost_usd": 1,
                            "price_valid_until_utc": later} for arm in probe.ARMS}}
    value.update(changes)
    return value


def test_approval_needs_limits_price_validity_and_a_balance_or_override():
    probe.check_approval(approval())
    with pytest.raises(ValueError, match="balance"):
        probe.check_approval(approval(unverified_balance_override=None))
    with pytest.raises(ValueError, match="exceeds the verified balance"):
        probe.check_approval(approval(verified_balance_usd=1, balance_checked_at_utc="2026-10-29T00:00:00+00:00"))
    expired = approval()
    expired["arms"]["N"]["price_valid_until_utc"] = "2020-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="expired"):
        probe.check_approval(expired)


def test_review_entries_ignore_free_text_and_resolve_aliases():
    row = {"answers": {
        "readers": {"stage_4": {"readers": [{"alias": "speed", "field_id": "field_00000", "why": "x"}]}},
        "progress": {"stage_6": {"invariants": [
            {"kind": "range", "field": "speed", "min": 0, "max": 10, "direction": None, "source": "A.Move",
             "catches": "one", "when": None, "then": None, "unless": None, "name": "a"},
            {"kind": "range", "field": "speed", "min": 0, "max": 10, "direction": None, "source": "A.Move",
             "catches": "two", "when": None, "then": None, "unless": None, "name": "b"}],
            "progress": [{"field": "speed", "direction": "up", "why": "w"}], "unjudgeable": []}}}}
    found = probe.entries(row, {"field_00000": "Asm:A:speed"})
    assert len(found) == 2
    assert {display["rule"] for _, display in found} == {"Asm:A:speed within [0, 10]",
                                                         "Asm:A:speed goes up as the game progresses"}
