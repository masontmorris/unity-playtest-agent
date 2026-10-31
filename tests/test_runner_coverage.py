from __future__ import annotations

import asyncio
from types import SimpleNamespace

from playtest.runner.coverage import CoverageFacts, coverage_line


def sent(choice_id: str, rejected: bool = False) -> SimpleNamespace:
    return SimpleNamespace(choice=SimpleNamespace(id=choice_id), execution=SimpleNamespace(rejected=rejected))


def report(*items: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(executed=tuple(items))


OFFERED = ["move", "turn", "use", "hop"]


def test_the_line_counts_accepted_inputs_and_names_offered_choices_with_none():
    reports = [report(sent("move")), report(), report(sent("move"), sent("turn")), report(sent("use", rejected=True))]
    assert coverage_line(reports, OFFERED) == (
        "Inputs sent so far this run, by choice: move 2, turn 1. "
        "Offered actions and skills not sent yet this run: use, hop.")


def test_a_run_with_nothing_sent_and_one_with_everything_sent_say_so():
    assert coverage_line([], OFFERED) == ("Inputs sent so far this run, by choice: none. "
                                          "Offered actions and skills not sent yet this run: move, turn, use, hop.")
    everything = [report(sent(c)) for c in OFFERED]
    assert coverage_line(everything, OFFERED).endswith("not sent yet this run: none.")


def test_a_choice_outside_the_offered_set_is_counted_but_never_listed_as_missing():
    line = coverage_line([report(sent("frontier-added"))], ["move"])
    assert "frontier-added 1" in line and line.endswith("not sent yet this run: move.")


def test_the_wrapper_appends_one_line_after_the_inner_facts_and_forwards_the_rest():
    calls = []

    class Inner:
        async def facts(self, reports, observation):
            return ["inner fact"]

        def directive_view(self, observation):
            return "view"

        def finish(self, **values):
            calls.append(("finish", values))

        def rebind(self, **values):
            calls.append(("rebind", values))

    wrapped = CoverageFacts(Inner(), OFFERED)
    lines = asyncio.run(wrapped.facts([report(sent("hop"))], None))
    assert lines[0] == "inner fact" and lines[1].startswith("Inputs sent so far this run, by choice: hop 1.")
    assert len(lines) == 2 and wrapped.directive_view(None) == "view"
    wrapped.finish(a=1)
    wrapped.rebind(b=2)
    assert calls == [("finish", {"a": 1}), ("rebind", {"b": 2})]


def test_the_line_recommends_nothing():
    line = coverage_line([report(sent("move"))], OFFERED).lower()
    assert not any(word in line for word in ("should", "try", "next", "recommend", "consider"))
