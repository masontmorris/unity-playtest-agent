from __future__ import annotations

from playtest.runner.frontier_help import FOLLOW_THROUGH_ATTEMPTS, recorded_suspicions


def probe(height: float, low_hit: bool) -> dict:
    return {"anchor": {"position": [0.0, height, 0.0]},
            "blockers": [{"height": 0.15, "hit": low_hit, "distance": 0.4 if low_hit else None, "max_distance": 1.8},
                         {"height": 0.9, "hit": False, "distance": None, "max_distance": 1.8}],
            "samples": [{"forward": 0.6, "delta_y": 0.3}]}


class Sink:
    def __init__(self, entries):
        self.entries = entries

    def of_kind(self, kind):
        return [e for e in self.entries if e["kind"] == kind]


def entries():
    out, seq = [], 0

    def add(kind, turn, body):
        nonlocal seq
        out.append({"trace_seq": seq, "kind": kind, "turn": turn, "body": body}); seq += 1

    heights = {1: 1.0, 2: 1.0, 3: 1.0, 4: 2.2, 5: 3.0}
    for turn in range(1, 6):
        add("observation", turn, {"public": {"probe": probe(heights[turn], turn <= 3)}})
    shortfall = {"suspicion_id": "s1", "basis": "effect_mismatch", "turn": 1, "based_on_seq": 0, "game_s": 1.0,
                 "subject": "walk", "statement": "walk moved 0", "detail": {}, "evidence_seq": [0]}
    add("suspicion", 1, shortfall)
    for turn, action, moved, calibrated in ((2, "walk", 0.0, 2.0), (3, "hop", 0.5, 1.1), (4, "walk", 2.0, 2.0)):
        add("effect", turn, {"choice_id": action, "action": action, "expected": {"net_displacement": calibrated},
                             "observed": {"net_displacement": moved}})
    add("suspicion", 2, {**shortfall, "suspicion_id": "s2", "basis": "range_exit", "turn": 2, "subject": "r"})
    return out


def test_a_shortfall_suspicion_lists_the_measured_attempts_after_it_in_order():
    rows = recorded_suspicions(Sink(entries()))
    lines = rows[0]["followed_by"]
    assert [line.split(":")[0] for line in lines] == ["turn 2 walk", "turn 3 hop", "turn 4 walk"]
    assert "moved 0.000 world units (calibrated 2.000)" in lines[0] and "a cast 0.15 above the feet hit a surface 0.40 ahead" in lines[0]
    assert "anchor height +1.20 by the next observation" in lines[1]
    assert "followed_by" not in rows[1]                         # only shortfalls carry the attempts after them
    assert FOLLOW_THROUGH_ATTEMPTS >= 3
