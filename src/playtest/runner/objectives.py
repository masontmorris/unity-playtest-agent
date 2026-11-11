"""Objective measurements from the same trace used by the play scorer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from playtest.contracts.investigation import Directive, DirectiveOutcome
from playtest.contracts.objective import ObjectiveItem, Postcondition, PressCount
from playtest.core.ports import DirectiveView, FactsSource, TurnReport
from playtest.runner.expectations import (action_events, plan_steps, play_presses, play_readings,
                                          situation_presses, situation_qualified_presses)
from playtest.runner.frontier_help import trace_rows


def _press_item(number: int, text: str, rules: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]],
                controls: Mapping[str, frozenset]) -> ObjectiveItem:
    rule = rules[0]
    action = rule["action"]
    windows = [candidate.get("within_s") for candidate in rules]
    window = max(windows) if all(isinstance(value, (int, float)) for value in windows) else None
    region = rule.get("region_id")
    criterion = PressCount(action=action, required=4, window_s=window, region_id=region,
                           within=rule.get("within") if region else None)
    readings, presses = play_readings(rows), play_presses(rows)
    effects = situation_presses(rows)
    counts, evidence = [], []
    measurable = bool(readings and controls.get(action))
    for candidate in rules:
        required = ("then", "then_expect", "within_s")
        if any(candidate.get(key) is None for key in required):
            measurable = False
            continue
        reader = candidate["then"]
        condition = candidate.get("condition_observable")
        if not any(values.get(reader) is not None and (condition is None or values.get(condition) is not None)
                   for _, _, values in readings):
            measurable = False
        selected = presses
        if candidate.get("situation"):
            if not candidate.get("target_object") or not candidate.get("region_id") or not candidate.get("within"):
                measurable = False
                continue
            selected, _, unread = situation_qualified_presses(candidate, presses, effects, controls)
            if unread or not effects:
                measurable = False
        own = controls.get(action)
        if condition is not None and own:
            for start, _, pressed in selected:
                if not own <= controls.get(pressed, frozenset()):
                    continue
                before = next((values for at, _, values in reversed(readings) if at <= start), None)
                if before is None or before.get(condition) is None:
                    measurable = False
        skipped: dict[str, int] = {}
        events = action_events(candidate, [readings], [selected], [controls], skipped)
        if skipped.get("unread") or skipped.get("unobserved_gap") or skipped.get("window_cut"):
            measurable = False
        counts.append(len(events))
        evidence.extend(event["seq"] for event in events if isinstance(event["seq"], int))
    same_count = len(counts) == len(rules) and len(set(counts)) == 1
    count = counts[0] if same_count and (measurable or counts[0] > 0) else None
    status = (DirectiveOutcome.met if count is not None and count >= criterion.required else
              DirectiveOutcome.open if count is not None and measurable else DirectiveOutcome.unknown)
    return ObjectiveItem(item_id=f"hunt-plan-{number}", source="hunt_plan", text=text, criterion=criterion,
                         count=count, status=status, evidence_seq=sorted(set(evidence)))


def _directives(rows: Sequence[Mapping[str, Any]], view: DirectiveView | None) -> list[Directive]:
    found: dict[str, Directive] = {}
    for row in rows:
        body = row.get("body") or {}
        values = []
        if row.get("kind") == "investigation" and body.get("event") in ("directive", "outcome"):
            values = [body.get("directive")]
        elif row.get("kind") == "context":
            payload = body.get("payload") or {}
            values = [*(payload.get("directive_history") or ()), payload.get("directive")]
        for value in values:
            if isinstance(value, Mapping):
                directive = Directive.model_validate(value)
                found[directive.directive_id] = directive
    if view is not None:
        for directive in (*view.closed, *([view.current] if view.current is not None else [])):
            found[directive.directive_id] = directive
    return list(found.values())


def objective_items(expectations: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
                    controls: Mapping[str, frozenset], view: DirectiveView | None = None) -> list[ObjectiveItem]:
    items = [_press_item(n, text, rules, rows, controls)
             for n, (text, rules) in enumerate(plan_steps(expectations), 1)]
    for directive in _directives(rows, view):
        status = directive.outcome if directive.expected is not None else DirectiveOutcome.unknown
        items.append(ObjectiveItem(item_id=directive.directive_id, source="frontier_directive", text=directive.text,
                                   criterion=Postcondition(expected=directive.expected,
                                                           deadline_game_s=directive.deadline_game_s),
                                   status=status, evidence_seq=directive.evidence_seq))
    return items


def objective_line(items: Sequence[ObjectiveItem], game_s: float | None) -> str:
    parts = []
    for number, item in enumerate(items, 1):
        progress = ""
        if isinstance(item.criterion, PressCount) and item.count is not None:
            progress = f": {item.count} of {item.criterion.required} presses"
        suffix = f", {item.status.value}" if progress else f": {item.status.value}"
        criterion = item.criterion
        if (isinstance(criterion, Postcondition) and item.status is DirectiveOutcome.open
                and criterion.deadline_game_s is not None and game_s is not None):
            suffix += f", deadline in {max(0.0, criterion.deadline_game_s - game_s):g} s"
        parts.append(f"({number}) [{item.source}] {item.text}{progress}{suffix}")
    return "Objective items (measured by code): " + ("; ".join(parts) if parts else "none") + "."


def objective_summary(items: Sequence[ObjectiveItem]) -> dict[str, Any]:
    sources = dict.fromkeys(item.source for item in items)
    by_source = {source: {"met": sum(item.status is DirectiveOutcome.met for item in items if item.source == source),
                          "total": sum(item.source == source for item in items)} for source in sources}
    return {"met": sum(item.status is DirectiveOutcome.met for item in items), "total": len(items),
            "by_source": by_source, "items": [item.model_dump(mode="json") for item in items]}


class ObjectiveFacts:
    def __init__(self, inner: FactsSource, sink: Any, expectations: Mapping[str, Any],
                 controls: Mapping[str, frozenset]) -> None:
        self._inner, self._sink, self._expectations, self._controls = inner, sink, expectations, controls

    async def facts(self, reports: Sequence[TurnReport], observation: Any) -> list[str]:
        lines = list(await self._inner.facts(reports, observation))
        view = self.directive_view(observation)
        rows = trace_rows(self._sink, ("observation", "effect", "execution", "option", "investigation", "context"))
        lines.append(objective_line(objective_items(self._expectations, rows, self._controls, view), observation.game_s))
        return lines

    def directive_view(self, observation: Any) -> DirectiveView | None:
        view = getattr(self._inner, "directive_view", None)
        return view(observation) if view is not None else None

    def finish(self, **values: Any) -> None:
        finish = getattr(self._inner, "finish", None)
        if finish is not None:
            finish(**values)

    def rebind(self, **values: Any) -> None:
        rebind = getattr(self._inner, "rebind", None)
        if rebind is not None:
            rebind(**values)


def with_objective_facts(inner: FactsSource, enabled: bool, sink: Any, expectations: Mapping[str, Any],
                         controls: Mapping[str, frozenset]) -> FactsSource:
    return ObjectiveFacts(inner, sink, expectations, controls) if enabled else inner
