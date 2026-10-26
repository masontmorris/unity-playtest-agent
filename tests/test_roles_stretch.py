"""Stretch roles: the video judge and the scheduled auditor (#73).

Both answer shapes are rendered into the strict subset, and both reviews report problems instead of
changing an answer: the judge must answer each question exactly once inside the clip, and an audit
note may cite only sequence numbers in the record it was shown.
"""

from __future__ import annotations

import json

import pytest

from playtest.roles import audit, judge, strict


def judge_item(question_id="q1", seen_at=(), answer="yes", note="visible"):
    return {"question_id": question_id, "answer": answer, "seen_at_s": list(seen_at), "note": note}


def judge_answer(answers=(), unexpected=()):
    return judge.JudgeAnswer.model_validate({"answers": list(answers), "unexpected": list(unexpected)})


def test_judge_schema_is_strict():
    assert strict.check(judge.schema(["q1", "q2"])) == []


def test_correct_judge_answer_is_clean():
    a = judge_answer([judge_item("q1", [0.5]), judge_item("q2", [2.0])])
    assert judge.review(a, ["q1", "q2"], 3.0) == []


JUDGE_CASES = [
    ("unanswered", judge_answer([judge_item("q2", [2.0])]), "answered 0 times"),
    (
        "answered_twice",
        judge_answer([judge_item("q1", [0.5]), judge_item("q1", [1.0]), judge_item("q2", [2.0])]),
        "answered 2 times",
    ),
    ("seen_outside_clip", judge_answer([judge_item("q1", [4.0]), judge_item("q2", [2.0])]), "outside the 3 s clip"),
    (
        "unexpected_outside_clip",
        judge_answer(
            [judge_item("q1", [0.5]), judge_item("q2", [2.0])],
            [{"description": "something odd", "at_s": 9.0}],
        ),
        "at_s is outside the 3 s clip",
    ),
]


@pytest.mark.parametrize(
    "answer, needle",
    [(answer, needle) for _, answer, needle in JUDGE_CASES],
    ids=[name for name, _, _ in JUDGE_CASES],
)
def test_judge_problems_are_reported(answer, needle):
    problems = judge.review(answer, ["q1", "q2"], 3.0)
    assert any(needle in p for p in problems)


def test_empty_unexpected_description_parses_then_is_reported():
    payload = {"answers": [judge_item("q1", [0.5])], "unexpected": [{"description": "", "at_s": None}]}
    value, errors = strict.parse(judge.JudgeAnswer, json.dumps(payload))
    assert errors == []
    assert isinstance(value, judge.JudgeAnswer)
    assert judge.review(value, ["q1"], 3.0) == ["unexpected[0]: description is empty"]


def test_audit_schema_is_strict():
    assert strict.check(audit.schema()) == []


def test_audit_note_must_cite_known_sequences():
    a = audit.AuditAnswer.model_validate(
        {"notes": [{"kind": "other", "text": "a fact about the record", "evidence_seq": [1, 2]}]}
    )
    assert audit.review(a, {1, 2, 3}) == []


def test_audit_note_without_evidence_parses_then_is_reported():
    payload = {"notes": [{"kind": "other", "text": "a fact about the record", "evidence_seq": []}]}
    value, errors = strict.parse(audit.AuditAnswer, json.dumps(payload))
    assert errors == []
    assert isinstance(value, audit.AuditAnswer)
    assert audit.review(value, {1, 2, 3}) == ["notes[0]: cites no sequence numbers"]


def test_audit_empty_text_is_reported():
    a = audit.AuditAnswer.model_validate({"notes": [{"kind": "other", "text": " ", "evidence_seq": [1]}]})
    assert audit.review(a, {1, 2, 3}) == ["notes[0]: text is empty"]


def test_audit_reports_an_unknown_sequence():
    a = audit.AuditAnswer.model_validate(
        {"notes": [{"kind": "other", "text": "a fact about the record", "evidence_seq": [9]}]}
    )
    problems = audit.review(a, {1, 2, 3})
    assert len(problems) == 1
    assert "not in the record" in problems[0]
    assert "9" in problems[0]
