"""Put one S1 in the production turn loop on a live game, in real time, for a few short episodes.

This is a smoke test of expected use, not a benchmark. The loop, executor, freshness window, answer
validation and adherence check are the product's. The facts, the escalate and delegate wording and the
provider adapter are named stand-ins until the product has them. Nothing here chooses an action: the
loop executes exactly what S1 answered, and a failed provider call ends the run without a retry.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as dt
import json
import math
import os
import re
import shutil
import statistics
import sys
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from playtest.bench import call_budget, frontier_live
from playtest.bench import record as recording
from playtest.bench import replay
from playtest.bench.record import HOLD_S, OBJECTIVE, calibrated_choices, digest, pass_plan
from playtest.contracts.bridge import ExecStatus, Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSet, ChoiceSource, TurnOutcome, TurnRecord
from playtest.core.adherence import adherence_rate, check_adherence
from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.bridge_lock import BridgeLockError
from playtest.core.context import INSTRUCTIONS, build_context, canonical
from playtest.core.turn_loop import TurnLoop
from playtest.frontier import media
from playtest.preflight.calibrate import _entity_snapshot, _new_runtime_errors
from playtest.preflight.entity import _complete, _norm, _sub, _vector

ARM = "mammoth-1"
EPISODES, MAX_TURNS, SEED = 3, 16, 2410
COMPLETED = re.compile(r"^(c\d+) \([^)]*\) held for ")
EXPECTED = re.compile(r"calibrated net displacement ([0-9.]+) world units")
SOURCES = {"live_source_sha256": Path(__file__), "replay_source_sha256": Path(replay.__file__),
           "record_source_sha256": Path(recording.__file__)}

Snapshot = Callable[[], Any]


class ProviderStop(Exception):
    """The provider failed in a way that ends the run; nothing is retried and no other model is tried."""


def core_choices() -> list[Choice]:
    """The choices every game has. The escalate and delegate wording is a stand-in: no manifest supplies it."""
    return [Choice(id="wait", kind=ChoiceKind.wait, label="Wait", source=ChoiceSource.core),
            Choice(id="escalate", kind=ChoiceKind.escalate, label="Ask the frontier model for help",
                   role="frontier", source=ChoiceSource.core),
            Choice(id="delegate", kind=ChoiceKind.delegate, label="Hand a bounded task to a helper",
                   role="helper", source=ChoiceSource.core),
            Choice(id="finish", kind=ChoiceKind.finish, label="Finish", source=ChoiceSource.core)]


class Capturing:
    """Hands the harness each ExecutionRecord the loop discards. It forwards every call unchanged."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.executions: list[Any] = []

    async def act(self, **kwargs: Any) -> Any:
        records = await self._inner.act(**kwargs)
        self.executions.append(records[-1])
        return records

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class Facts:
    """Measured facts in the replays' wording, written from the loop's own records.

    A stand-in for the facts builder. It reports what the bridge returned and what an inspection
    measured; it never advises. Every line has a side-by-side `{text, seqs}` row whose citations
    come from explicit record fields, not from parsing the sentence. S1 turn notes are kept apart.
    """

    def __init__(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot
        self.lines: list[str] = []
        self.position_line: str | None = None
        self._evidence: list[dict[str, Any]] = []
        self._position_evidence: dict[str, Any] | None = None
        self._previous: dict[str, Any] | None = None
        self._held: dict[str, Any] | None = None

    async def before_decision(self) -> tuple[float | None, bool]:
        """Take a fresh position and settle the hold the last turn executed: (displacement, settled)."""
        now = await self._snapshot()
        observed, settled = None, False
        if self._held is not None and self._previous is not None:
            p0, p1 = _vector(self._previous.get("position")), _vector(now.get("position"))
            observed = _norm(_sub(p1, p0)) if p0 is not None and p1 is not None else None
            held = self._held
            line = (f"{held['id']} ({held['action']}) held for {held['duration']} seconds with verified release; "
                    f"net displacement between inspection seq {self._previous['inspection_seq']} and "
                    f"{now['inspection_seq']}: {observed} world units.")
            self.lines.append(line)
            self._evidence.append({"text": line,
                                   "seqs": [self._previous["inspection_seq"], now["inspection_seq"]]})
            self._held, settled = None, True
        self._previous = now
        self.position_line = (f"Entity position from inspection seq {now['inspection_seq']} at game time "
                              f"{now['game_time_s']}: {now['position']} in Unity world units.")
        self._position_evidence = {"text": self.position_line, "seqs": [now["inspection_seq"]]}
        return observed, settled

    def context(self) -> list[str]:
        return [*self.lines, self.position_line] if self.position_line else list(self.lines)

    def evidence(self) -> list[dict[str, Any]]:
        """The same lines with explicit citation lists; turn notes about S1 are not game evidence."""
        rows = [copy.deepcopy(row) for row in self._evidence]
        if self._position_evidence is not None:
            rows.append(copy.deepcopy(self._position_evidence))
        return rows

    def note_turn(self, choice: Choice | None, record: TurnRecord, execution: Any | None) -> None:
        seqs = [record.based_on_seq]
        execution_seq = getattr(execution, "seq", None)
        if isinstance(execution_seq, int) and not isinstance(execution_seq, bool):
            seqs.append(execution_seq)
        if record.outcome is TurnOutcome.rejected_invalid:
            # This is a note about S1's answer, not a game observation: it is not citable evidence.
            self.lines.append("The last answer did not name an offered choice; no input was sent.")
        elif record.outcome is TurnOutcome.rejected_stale and choice is not None:
            reason = execution.reject_reason.value if execution is not None and execution.reject_reason is not None else "unknown"
            line = f"{choice.id} ({choice.action}) was rejected by the bridge ({reason}); no input was sent."
            self.lines.append(line)
            self._evidence.append({"text": line, "seqs": seqs})
        elif record.executed_action is not None and choice is not None and execution is not None:
            if execution.status is ExecStatus.completed and execution.release_verified is True:
                self._held = {"id": choice.id, "action": choice.action, "duration": execution.applied_duration_s}
            else:
                line = (f"{choice.id} ({choice.action}) ended as {execution.status.value} "
                        "without a verified release.")
                self.lines.append(line)
                self._evidence.append({"text": line, "seqs": seqs})


def accepted_ids(controls: list[str], facts: list[str]) -> tuple[set[str], set[str]]:
    """The replays' mechanical rule on the facts S1 saw: the unchecked controls, else `finish`."""
    done = {m.group(1) for fact in facts if (m := COMPLETED.match(fact))}
    unchecked = {item for item in controls if item not in done}
    return (unchecked or {"finish"}), done


def miss_kind(choice: str | None, accept: set[str], done: set[str]) -> str | None:
    if choice is None or choice in accept:
        return None
    if choice in done:
        return "re-check"
    return {"finish": "premature-finish", "wait": "wait", "escalate": "escalate",
            "delegate": "delegate"}.get(choice, "other")


class LiveS1:
    """One blocking systemone call per turn, ledgered before it is made. The adapter holds no policy."""

    def __init__(self, directory: Path, approval: dict[str, Any], *, send: replay.Send = replay.send_systemone,
                 directive_source: Callable[[], tuple[Any | None, list[Any]]] | None = None,
                 objective: str = OBJECTIVE) -> None:
        self.ledger = call_budget.Ledger(directory, approval, ARM)
        self.call_dir = self.ledger.call_dir
        self.approval, self.limit, self.send = approval, approval["arms"][ARM], send
        self.directive_source = directive_source or (lambda: (None, []))
        self.objective = objective
        self.episode, self.loop, self.facts = 0, None, None
        self.help_since_game_s: float | None = None
        self.last: dict[str, Any] = {}
        self.binding: dict[str, Any] = {}

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.ledger.arm_rows()

    def bind(self, episode: int, loop: TurnLoop, facts: Facts) -> None:
        self.episode, self.loop, self.facts, self.help_since_game_s = episode, loop, facts, None
        self.last, self.binding = {}, {}

    def pending_help(self, observation: Observation) -> dict[str, Any] | None:
        pending = self.loop.pending_helper
        if pending is None:
            return None
        since = self.help_since_game_s
        age = observation.game_s - since if since is not None and observation.game_s is not None else None
        return {"choice_id": pending["choice"], "kind": pending["kind"], "since_game_s": since, "age_game_s": age}

    def decide(self, choice_set: ChoiceSet, observation: Observation | None) -> Any:
        if observation is None:
            raise ProviderStop("the loop gave no observation")
        if observation.clock_mode is not ClockMode.realtime or not observation.playing or observation.paused \
                or observation.time_scale != 1.0:
            raise ValueError("a decision observation was not in unpaused real time at scale 1")
        age_s = self.loop.store.age_s
        facts = self.facts.context()
        directive, history = self.directive_source()
        payload = build_context(objective=self.objective, observation=observation, choices=choice_set, facts=facts,
                                directive=directive, directive_history=history,
                                pending_help=self.pending_help(observation), age_s=age_s)
        self.binding = {"choices": choice_set, "observation": observation, "facts": facts, "age_s": age_s,
                        "objective": self.objective, "directive": directive,
                        "directive_history": list(history), "payload": payload}
        model = replay.ARMS[ARM][0]
        body = {"model": model, "questions": {"next_action": {
            "type": "choice", "instructions": payload["instructions"],
            "criteria": {c["id"]: c["label"] for c in payload["choices"]}}}, "state": payload}
        request_sha = digest(canonical(body))
        try:
            reservation = self.ledger.reserve(
                episode=self.episode, turn=len(self.loop.turns), based_on_seq=observation.seq,
                request_sha256=request_sha, requested_model=model)
        except call_budget.BudgetStop as exc:
            raise ProviderStop(str(exc)) from None
        try:
            status, data, latency_ms, problem = self.send(ARM, body)
        except Exception as exc:  # Keep the reservation: an unknown outcome is never retried.
            row = reservation.finish(http_status=None, choice=None, distribution=None,
                                     problem=f"adapter error: {type(exc).__name__}", latency_ms=None,
                                     resolved_model=None, usage=None, cost_usd=None)
            choice, distribution, answer_problem = None, None, "adapter error"
        else:
            choice, distribution, answer_problem = replay.parse_answer(
                data if status == 200 else None, {c["id"] for c in payload["choices"]})
            usage = (data or {}).get("usage")
            raw_cost = usage.get("cost") if isinstance(usage, dict) else None
            cost = (float(raw_cost) if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
                    and math.isfinite(raw_cost) and raw_cost >= 0 else None)
            row = reservation.finish(http_status=status, choice=choice, distribution=distribution,
                                     problem=problem or answer_problem, latency_ms=latency_ms,
                                     resolved_model=(data or {}).get("model"), usage=usage, cost_usd=cost)
        self.last = {"facts": facts, "age_s": age_s, "s1_status": row["http_status"], "s1_latency_ms": row["latency_ms"],
                     "resolved_model": row["resolved_model"], "problem": row["problem"], "distribution": distribution,
                     "choice": choice}
        if row["cost_bound_exceeded"]:
            raise ProviderStop("a provider-reported cost exceeded the per-call bound")
        if row["http_status"] != 200:
            raise ProviderStop(f"{ARM} returned {row['http_status'] or 'a transport failure'} ({row['problem']}); "
                               "stopped with no retry")
        if choice is None:
            return None  # A bad answer rejects one turn; the loop records it and nothing is repaired.
        return {"choice_id": choice, "set_version": choice_set.set_version, "distribution": distribution}


def make_snapshot(client: Any, entity_id: str) -> Snapshot:
    state = {"seen": 0}

    async def snapshot() -> dict[str, Any]:
        response = await client.inspect("transforms")
        snap = _entity_snapshot(response, entity_id)
        snap["inspection_seq"] = response.seq
        state["seen"], errors = _new_runtime_errors(snap, state["seen"])
        if errors:
            raise ValueError("game errors were observed during the run")
        return snap

    return snapshot


def _execution_row(execution: Any | None) -> dict[str, Any] | None:
    if execution is None:
        return None
    return {"seq": getattr(execution, "seq", None),
            "wall_utc": _wall(execution.wall_utc) if getattr(execution, "wall_utc", None) is not None else None,
            "game_s": getattr(execution, "game_s", None),
            "action_id": getattr(execution, "action_id", None),
            "input_start_game_s": getattr(execution, "input_start_game_s", None),
            "input_end_game_s": getattr(execution, "input_end_game_s", None),
            "input_start_wall_s": getattr(execution, "input_start_wall_s", None),
            "input_end_wall_s": getattr(execution, "input_end_wall_s", None),
            "status": execution.status.value,
            "reject_reason": execution.reject_reason.value if execution.reject_reason is not None else None,
            "validated_against_seq": execution.validated_against_seq,
            "applied_duration_s": execution.applied_duration_s, "release_verified": execution.release_verified}


def _wall(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


def _recent_row(record: TurnRecord, choice: Choice | None, observation: Observation,
                execution: Any | None, executed_choice: Choice | None = None) -> dict[str, Any]:
    """One action-aligned frontier evidence row, with no rationale or distribution."""
    return {
        "seq": record.based_on_seq,
        "game_s": observation.game_s,
        "wall_utc": _wall(observation.wall_utc),
        "public": copy.deepcopy(observation.public),
        "reader_errors": [error.model_dump(mode="json") for error in observation.reader_errors],
        "turn": {"turn": record.turn, "raw_choice": record.raw_choice,
                 "effective_choice": record.effective_choice, "executed_action": record.executed_action,
                 "outcome": record.outcome.value, "policy_default": record.policy_default,
                 "while_pending": record.while_pending.model_dump(mode="json") if record.while_pending else None},
        "action": (choice.action if choice is not None and choice.kind is ChoiceKind.action else
                   choice.skill if choice is not None and choice.kind is ChoiceKind.skill else None),
        "execution": _execution_row(execution),
        "executed_choice": executed_choice.model_dump(mode="json") if executed_choice is not None else None,
    }


def summarize_episode(turns: list[dict[str, Any]]) -> dict[str, Any]:
    done = [t for t in turns if t["execution"] and t["execution"]["status"] == "completed"
            and t["execution"]["release_verified"] is True]
    ids = [t["executed_action"] for t in done]
    scored = [t["acceptable"] for t in turns if t["acceptable"] is not None]
    s1 = [t["s1_latency_ms"] for t in turns if t["s1_latency_ms"] is not None]
    loop = [t["turn_latency_ms"] for t in turns if t["turn_latency_ms"] is not None]
    finished = bool(turns) and turns[-1]["effective_choice"] == "finish"
    return {"turns": len(turns), "finished": finished, "controls_completed": len(set(ids)),
            "repeat_holds": len(ids) - len(set(ids)),
            "outcomes": dict(Counter(t["outcome"] for t in turns)),
            "stale": sum(t["outcome"] == "rejected_stale" for t in turns),
            "invalid": sum(t["outcome"] == "rejected_invalid" for t in turns),
            "top1": {"hits": sum(scored), "scored": len(scored)},
            "misses": dict(Counter(t["miss"] for t in turns if t["miss"])),
            "s1_latency_ms": {"median": statistics.median(s1) if s1 else None, "max": max(s1) if s1 else None},
            "turn_latency_ms": {"median": statistics.median(loop) if loop else None, "max": max(loop) if loop else None}}


async def run_episode(client: Capturing, *, episode: int, offered: list[Choice], snapshot: Snapshot, s1: LiveS1,
                      max_turns: int, section: dict[str, Any], save: Callable[[], None], clock: Callable[[], float],
                      frontier: frontier_live.LiveFrontier | None = None) -> TurnLoop:
    loop = TurnLoop(client, choices=offered, clock_mode=ClockMode.realtime, lease_s=HOLD_S)
    facts = Facts(snapshot)
    s1.bind(episode, loop, facts)
    if frontier is not None:
        frontier.bind(episode=episode, loop=loop, observe=client.observe_once)
    by_id = {c.id: c for c in offered}
    controls = [c.id for c in offered if c.kind is ChoiceKind.action]
    recent: list[dict[str, Any]] = []
    episode_start_wall = dt.datetime.now(dt.timezone.utc)
    previous_hold: dict[str, Any] | None = None
    while len(loop.turns) < max_turns:
        observed, settled = await facts.before_decision()
        if settled and previous_hold is not None:
            previous_hold["observed_displacement"] = observed
        started, before = clock(), len(client.executions)
        record = await loop.run_turn(s1.decide)
        execution = client.executions[-1] if len(client.executions) > before else None
        choice = by_id.get(record.effective_choice or "") or by_id.get(record.raw_choice or "")
        executed_choice = by_id.get(record.executed_action or "")
        turn_observation = loop.store.get(record.based_on_seq)
        if turn_observation is None:
            raise ValueError("the turn's observation is missing from the store")
        recent.append(_recent_row(record, choice, turn_observation, execution,
                                  executed_choice=executed_choice))
        if record.outcome in (TurnOutcome.escalated, TurnOutcome.delegated) and s1.help_since_game_s is None:
            latest = loop.store.latest()
            s1.help_since_game_s = latest.game_s if latest is not None else None
        facts.note_turn(executed_choice or choice, record, execution)
        accept, done = accepted_ids(controls, s1.last.get("facts", []))
        picked = record.raw_choice if record.outcome is not TurnOutcome.rejected_invalid else None
        distribution = s1.last.get("distribution")
        turn = {"turn": record.turn, "started_s": round(started, 3), "based_on_seq": record.based_on_seq,
                "set_version": record.set_version, "age_s": s1.last.get("age_s"),
                "s1_status": s1.last.get("s1_status"), "s1_latency_ms": s1.last.get("s1_latency_ms"),
                "raw_choice": record.raw_choice, "effective_choice": record.effective_choice,
                "executed_action": record.executed_action, "outcome": record.outcome.value,
                "policy_default": record.policy_default, "turn_latency_ms": record.latency_ms,
                "distribution": distribution, "top_probability": max(distribution.values()) if distribution else None,
                "accept": sorted(accept), "completed_before": len(done),
                "acceptable": (picked in accept) if picked is not None else None,
                "acceptable_mass": sum(distribution[i] for i in accept if i in distribution) if distribution else None,
                "miss": miss_kind(picked, accept, done), "facts": s1.last.get("facts", []),
                "execution": _execution_row(execution),
                "expected_displacement": (float(m.group(1)) if choice is not None and (m := EXPECTED.search(choice.label)) else None),
                "observed_displacement": None}
        section["turns"].append(turn)
        previous_hold = turn if executed_choice is not None and execution is not None else None
        save()
        pending = loop.pending_helper
        if (frontier is not None and record.outcome is TurnOutcome.escalated and pending is not None
                and pending.get("kind") == "escalate" and pending.get("turn") == record.turn):
            try:
                await frontier.on_escalation(record=record, binding=s1.binding, evidence=facts.evidence(),
                                             recent=recent, episode_start_wall_utc=episode_start_wall)
            except frontier_live.FrontierError as exc:
                raise ProviderStop(str(exc)) from None
        if choice is not None and choice.kind is ChoiceKind.finish and record.outcome is TurnOutcome.finished:
            break
    observed, settled = await facts.before_decision()
    if settled and previous_hold is not None:
        previous_hold["observed_displacement"] = observed
    violations = check_adherence(loop.turns)
    section["adherence"] = {"rate": adherence_rate(loop.turns), "violations": [v.__dict__ for v in violations]}
    summary = summarize_episode(section["turns"])
    summary["finished_with_all_controls"] = summary["finished"] and summary["controls_completed"] == len(controls)
    summary["pending_helper_at_end"] = loop.pending_helper is not None
    section["summary"] = summary
    save()
    return loop


async def live(client: BridgeClient, selected: dict[str, Any], calibration: dict[str, Any], trace: dict[str, Any],
               output: Path, s1: LiveS1, *, episodes: int, max_turns: int, seed: int,
               frontier: frontier_live.LiveFrontier | None = None, plan: dict[str, Any] | None = None,
               plan_path: Path | None = None) -> None:
    if selected.get("status") != "identified" or calibration.get("entity_id") != selected.get("entity_id"):
        raise ValueError("the identified entity and calibrated entity do not match")
    if selected.get("stop", {}).get("release_verified") is not True:
        raise ValueError("the stage 2 record has no verified release")
    discovery = _complete(await client.inspect("discovery"), "discovery")
    for name in ("game_objects", "components", "bindings", "input_asset_bindings"):
        if discovery.get("inspection_coverage", {}).get(name, {}).get("truncated") is not False:
            raise ValueError(f"live discovery has incomplete {name} coverage")
    entity_id = selected["entity_id"]
    entities = [item for item in discovery.get("game_objects", []) if item.get("id") == entity_id]
    if len(entities) != 1 or not isinstance(selected.get("entity"), dict):
        raise ValueError("the entity does not resolve uniquely")
    if any(entities[0].get(k) != selected["entity"].get(k) for k in ("scene_id", "scene_path", "path")):
        raise ValueError("the entity descriptor changed")
    controls = [c for c in calibrated_choices(calibration, discovery) if c.kind is ChoiceKind.action]
    if frontier is not None:
        provenance = frontier.use_manifest(frontier_live.manifest_for(controls))
        trace.setdefault("settings", {}).setdefault("frontier", {}).update(provenance)
        if plan is not None:
            plan.setdefault("frontier", {}).update(provenance)
            if plan_path is not None:
                replay._write(plan_path, plan)
        recording._save(output, trace)
    snapshot, capturing = make_snapshot(client, entity_id), Capturing(client)
    epoch = time.monotonic()
    save = lambda: recording._save(output, trace)  # noqa: E731
    for episode in range(1, episodes + 1):
        ids, _ = pass_plan("shuffled", seed, episode, len(controls))
        held = [control.model_copy(update={"id": f"c{ids[i]}"}) for i, control in enumerate(controls)]
        offered = sorted(held, key=lambda item: int(item.id[1:])) + core_choices()
        section = {"episode": episode, "ids": ids, "choices": [c.id for c in offered], "turns": [],
                   "summary": None, "adherence": None}
        trace["episodes"].append(section)
        save()
        await run_episode(capturing, episode=episode, offered=offered, snapshot=snapshot, s1=s1, max_turns=max_turns,
                          section=section, save=save, clock=lambda: time.monotonic() - epoch, frontier=frontier)
    trace["status"] = "complete"


async def _run(project: Path, selected: dict[str, Any], calibration: dict[str, Any], trace: dict[str, Any],
               output: Path, s1: LiveS1, episodes: int, max_turns: int, seed: int, *,
               approval: dict[str, Any], frontier_enabled: bool, plan: dict[str, Any],
               plan_path: Path) -> None:
    client = await BridgeClient.connect_from_project(project, timeout_s=120.0)
    frontier = None
    try:
        trace["unity_version"] = client.status.unity_version if client.status is not None else None
        if frontier_enabled:
            frontier = frontier_live.LiveFrontier(
                output.parent, approval,
                media_source=lambda observation, since, folder: media.capture_clip(
                    project=project, status=client.status, since_utc=since,
                    until_utc=observation.wall_utc, output_dir=folder))
            s1.directive_source = frontier.directives
        await live(client, selected, calibration, trace, output, s1, episodes=episodes, max_turns=max_turns, seed=seed,
                   frontier=frontier, plan=plan, plan_path=plan_path)
    except ProviderStop as exc:
        trace.update(status="stopped", error=f"ProviderStop: {exc}")
    except (BridgeError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        trace.update(status="inconclusive", error=f"{type(exc).__name__}: {exc}")
    finally:
        stop = await client.aclose()
        trace["stop"] = stop.model_dump(mode="json") if stop is not None else None
        trace["game_worktree_after"] = recording._game_worktree(project)
        if stop is None or stop.release_verified is not True:
            trace.update(status="inconclusive",
                         error=f"{trace.get('error') or ''}; bridge shutdown did not verify input release".lstrip("; "))
        if trace["game_worktree_before"] != trace["game_worktree_after"]:
            trace.update(status="inconclusive", error=f"{trace.get('error') or ''}; the game worktree changed".lstrip("; "))
        turns = [t for e in trace["episodes"] for t in e["turns"]]
        trace["totals"] = {"episodes": len(trace["episodes"]), "turns": len(turns), "s1_calls": len(s1.rows),
                           "executed": sum(t["outcome"] == "executed" for t in turns),
                           "stale": sum(t["outcome"] == "rejected_stale" for t in turns),
                           "invalid": sum(t["outcome"] == "rejected_invalid" for t in turns),
                           "adherence_violations": sum(len(e["adherence"]["violations"]) for e in trace["episodes"]
                                                       if e["adherence"])}
        if frontier_enabled:
            rows = frontier.rows if frontier is not None else []
            block = frontier_live.acceptance(rows, run_status=trace["status"])
            known = [row["cost_usd"] for row in rows
                     if isinstance(row.get("cost_usd"), (int, float)) and not isinstance(row["cost_usd"], bool)]
            block["calls"] = len(rows)
            block["cost_usd"] = (round(sum(known), 6) if len(known) == len(rows) else
                                 (0.0 if not rows else None))
            block["reserved_usd"] = str(sum((Decimal(str(row.get("reserved_usd"))) for row in rows), Decimal(0)))
            trace["frontier"] = block
            trace["totals"]["frontier_calls"] = len(rows)
        recording._save(output, trace)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project", "entity", "calibration", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--approval", type=Path, default=Path(os.environ["UPA_LIVE_APPROVAL"]) if os.environ.get("UPA_LIVE_APPROVAL") else None)
    parser.add_argument("--episodes", type=int, default=int(os.environ.get("UPA_LIVE_EPISODES") or EPISODES))
    parser.add_argument("--max-turns", type=int, default=int(os.environ.get("UPA_LIVE_MAX_TURNS") or MAX_TURNS))
    parser.add_argument("--seed", type=int, default=int(os.environ.get("UPA_LIVE_SEED") or SEED))
    parser.add_argument("--frontier", action="store_true", default=_env_flag("UPA_LIVE_FRONTIER"),
                        help="let an S1 escalation call the frontier (also UPA_LIVE_FRONTIER=1)")
    args = parser.parse_args(argv)
    project, output = args.project.resolve(), args.output.resolve()
    if args.approval is None:
        parser.error("--approval (or UPA_LIVE_APPROVAL) is required: no paid call starts without one")
    if output.is_relative_to(project) or output.exists():
        parser.error("--output must be a new file outside the Unity project")
    if not os.environ.get(replay.ARMS[ARM][1]):
        parser.error(f"{replay.ARMS[ARM][1]} is not set in the environment")
    if args.frontier:
        if not os.environ.get("META_API_KEY"):
            parser.error("META_API_KEY is not set in the environment")
        if shutil.which("ffmpeg") is None:
            parser.error("ffmpeg is not on PATH: a frontier clip cannot be encoded")
    try:
        approval, approval_sha = replay._load(args.approval)
        arms = [ARM, frontier_live.FRONTIER_ARM] if args.frontier else [ARM]
        replay.validate_approval(approval, arms)
        limit, planned_s1 = approval["arms"][ARM], args.episodes * args.max_turns
        bound = replay._decimal(limit["per_call_max_usd"], "per-call bound")
        objective = frontier_live.HELP_OBJECTIVE if args.frontier else OBJECTIVE
        assembled = {"enabled": True, "model": frontier_live.FRONTIER_ARM,
                     "reasoning_effort": frontier_live.EFFORT,
                     "objective_sha256": digest(objective),
                     "max_possible_calls": planned_s1,
                     "manifest_source": frontier_live.MANIFEST_SOURCE,
                     "manifest_provenance": frontier_live.MANIFEST_PROVENANCE,
                     "schema_sha256": None, "system_prompt_sha256": None,
                     "manifest_message_sha256": None, "manifest_sha256": None}
        if args.frontier:
            frontier_limit = approval["arms"][frontier_live.FRONTIER_ARM]
            frontier_bound = replay._decimal(frontier_limit["per_call_max_usd"], "frontier per-call bound")
            planned_frontier = planned_s1
            if (planned_s1 + planned_frontier > approval["max_calls"]
                    or planned_s1 > limit["max_calls"] or planned_frontier > frontier_limit["max_calls"]
                    or bound * planned_s1 > replay._decimal(limit["max_cost_usd"], "arm limit")
                    or frontier_bound * planned_frontier > replay._decimal(frontier_limit["max_cost_usd"],
                                                                           "frontier arm limit")
                    or (bound * planned_s1 + frontier_bound * planned_frontier)
                    > replay._decimal(approval["max_cost_usd"], "overall limit")):
                raise ValueError("the approved call or cost envelope is smaller than the longest possible run")
        elif planned_s1 > approval["max_calls"] or planned_s1 > limit["max_calls"] or bound * planned_s1 > replay._decimal(
                approval["max_cost_usd"], "overall limit") or bound * planned_s1 > replay._decimal(limit["max_cost_usd"], "arm limit"):
            raise ValueError("the approved call or cost envelope is smaller than the longest possible run")
        entity_bytes, calibration_bytes = args.entity.read_bytes(), args.calibration.read_bytes()
        selected, calibration = json.loads(entity_bytes), json.loads(calibration_bytes)
        repo = Path(__file__).resolve().parents[3]
        design = repo / "docs" / "design" / "s1-bench.md"
        plan = {"version": 1, "kind": "live-loop-smoke", "arm": ARM, "approval_sha256": approval_sha, "seed": args.seed,
                "episodes": args.episodes, "max_turns": args.max_turns, "max_possible_calls": planned_s1,
                **{key: digest(path.read_bytes()) for key, path in SOURCES.items()},
                "design_sha256": digest(design.read_bytes()) if design.is_file() else None,
                "budget_assumptions": {"balance_override": approval.get("unverified_balance_override")}}
        if args.frontier:
            plan["frontier"] = assembled
            plan["max_possible_total_calls"] = planned_s1 + planned_s1
        trace: dict[str, Any] = {
            "version": 1, "kind": "live-loop-smoke", "status": "running", "error": None,
            "game_commit": recording._git_head(project), "repo_commit": recording._git_head(repo),
            "game_worktree_before": recording._game_worktree(project),
            "bridge_source_sha256": recording._source_hash(repo / "bridge", {".cs", ".asmdef", ".json"}),
            "python_source_sha256": recording._source_hash(repo / "src" / "playtest", {".py"}),
            "entity_sha256": digest(entity_bytes), "calibration_sha256": digest(calibration_bytes),
            "instructions_sha256": digest(INSTRUCTIONS), "objective_sha256": digest(objective),
            "recorded_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "clock_mode": "realtime",
            "settings": {"arm": ARM, "episodes": args.episodes, "max_turns": args.max_turns, "seed": args.seed,
                         "lease_s": HOLD_S, "max_age_s": 0.5, "freshness": "TurnLoop default",
                         "approval_sha256": approval_sha},
            "episodes": [], "stop": None}
        if args.frontier:
            trace["settings"]["frontier"] = dict(assembled)
            trace["settings"]["max_possible_total_calls"] = planned_s1 + planned_s1
        output.parent.mkdir(parents=True, exist_ok=True)
        plan_path = output.parent / "plan.json"
        replay._write(plan_path, plan, exclusive=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(trace, stream, allow_nan=False)
        s1 = LiveS1(output.parent, approval, objective=objective)
        asyncio.run(_run(project, selected, calibration, trace, output, s1, args.episodes, args.max_turns, args.seed,
                         approval=approval, frontier_enabled=args.frontier, plan=plan, plan_path=plan_path))
    except (OSError, ValueError, BridgeError, BridgeLockError) as exc:
        print(f"live: {exc}", file=sys.stderr)
        return 1
    print(f"live run {trace['status']}: {trace.get('totals')}")
    if args.frontier:
        print(f"frontier: {trace.get('frontier')}")
    return 0 if trace["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
