"""Put one S1 in the production turn loop on a live game, in real time, for a few short episodes.

This is a smoke test of expected use, not a benchmark. The loop, executor, freshness window, answer
validation and adherence check are the product's. The facts, the escalate and delegate wording and the
provider adapter are named stand-ins until the product has them. Nothing here chooses an action: the
loop executes exactly what S1 answered, and a failed provider call ends the run without a retry.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

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
    measured; it never advises.
    """

    def __init__(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot
        self.lines: list[str] = []
        self.position_line: str | None = None
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
            self.lines.append(f"{held['id']} ({held['action']}) held for {held['duration']} seconds with verified release; "
                              f"net displacement between inspection seq {self._previous['inspection_seq']} and "
                              f"{now['inspection_seq']}: {observed} world units.")
            self._held, settled = None, True
        self._previous = now
        self.position_line = (f"Entity position from inspection seq {now['inspection_seq']} at game time "
                              f"{now['game_time_s']}: {now['position']} in Unity world units.")
        return observed, settled

    def context(self) -> list[str]:
        return [*self.lines, self.position_line] if self.position_line else list(self.lines)

    def note_turn(self, choice: Choice | None, record: TurnRecord, execution: Any | None) -> None:
        if record.outcome is TurnOutcome.rejected_invalid:
            self.lines.append("The last answer did not name an offered choice; no input was sent.")
        elif record.outcome is TurnOutcome.rejected_stale and choice is not None:
            reason = execution.reject_reason.value if execution is not None and execution.reject_reason is not None else "unknown"
            self.lines.append(f"{choice.id} ({choice.action}) was rejected by the bridge ({reason}); no input was sent.")
        elif record.outcome is TurnOutcome.executed and choice is not None and execution is not None:
            if execution.status is ExecStatus.completed and execution.release_verified is True:
                self._held = {"id": choice.id, "action": choice.action, "duration": execution.applied_duration_s}
            else:
                self.lines.append(f"{choice.id} ({choice.action}) ended as {execution.status.value} "
                                  "without a verified release.")


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

    def __init__(self, directory: Path, approval: dict[str, Any], *, send: replay.Send = replay.send_systemone) -> None:
        self.call_dir = directory / "calls"
        self.call_dir.mkdir(parents=True, exist_ok=True)
        self.approval, self.limit, self.send = approval, approval["arms"][ARM], send
        self.bound = replay._decimal(self.limit["per_call_max_usd"], "per-call bound")
        self.rows = replay._calls(self.call_dir)
        self.episode, self.loop, self.facts = 0, None, None
        self.help_since_game_s: float | None = None
        self.last: dict[str, Any] = {}

    def bind(self, episode: int, loop: TurnLoop, facts: Facts) -> None:
        self.episode, self.loop, self.facts, self.help_since_game_s = episode, loop, facts, None
        self.last = {}

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
        payload = build_context(objective=OBJECTIVE, observation=observation, choices=choice_set, facts=facts,
                                pending_help=self.pending_help(observation), age_s=age_s)
        model = replay.ARMS[ARM][0]
        body = {"model": model, "questions": {"next_action": {
            "type": "choice", "instructions": payload["instructions"],
            "criteria": {c["id"]: c["label"] for c in payload["choices"]}}}, "state": payload}
        request_sha = digest(canonical(body))
        now = dt.datetime.now(dt.timezone.utc)
        if now >= replay._price_deadline(self.limit, ARM):
            raise ProviderStop(f"{ARM} pricing validity expired; stopped before another call")
        total = sum((Decimal(str(row["reserved_usd"])) for row in self.rows), Decimal(0))
        if len(self.rows) >= self.approval["max_calls"] or len(self.rows) >= self.limit["max_calls"] \
                or total + self.bound > replay._decimal(self.approval["max_cost_usd"], "overall limit") \
                or total + self.bound > replay._decimal(self.limit["max_cost_usd"], "arm limit"):
            raise ProviderStop("the approved call or cost envelope would be exceeded")
        slot = digest(canonical({"episode": self.episode, "turn": len(self.loop.turns), "request_sha256": request_sha}))
        row = {"slot": slot, "episode": self.episode, "turn": len(self.loop.turns), "arm": ARM,
               "based_on_seq": observation.seq, "request_sha256": request_sha, "requested_model": model,
               "reserved_usd": str(self.bound), "status": "in_flight"}
        path = self.call_dir / f"{slot}.json"
        replay._write(path, row, exclusive=True)  # Reserve before the POST; a crash must not cause a retry.
        status, data, latency_ms, problem = None, None, None, None
        try:
            status, data, latency_ms, problem = self.send(ARM, body)
            choice, distribution, answer_problem = replay.parse_answer(
                data if status == 200 else None, {c["id"] for c in payload["choices"]})
            usage = (data or {}).get("usage")
            raw_cost = usage.get("cost") if isinstance(usage, dict) else None
            cost = (float(raw_cost) if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
                    and math.isfinite(raw_cost) and raw_cost >= 0 else None)
            row.update(status="finished", http_status=status, choice=choice, distribution=distribution,
                       problem=problem or answer_problem, latency_ms=latency_ms, resolved_model=(data or {}).get("model"),
                       usage=usage, cost_usd=cost, cost_bound_exceeded=(Decimal(str(cost)) > self.bound if cost is not None else False))
        except Exception as exc:  # Keep the reservation: an unknown outcome is never retried.
            row.update(status="finished", http_status=None, choice=None, distribution=None,
                       problem=f"adapter error: {type(exc).__name__}", latency_ms=None, resolved_model=None,
                       usage=None, cost_usd=None, cost_bound_exceeded=False)
            choice, distribution, answer_problem = None, None, "adapter error"
        replay._write(path, row)
        self.rows.append(row)
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
    return {"status": execution.status.value,
            "reject_reason": execution.reject_reason.value if execution.reject_reason is not None else None,
            "validated_against_seq": execution.validated_against_seq,
            "applied_duration_s": execution.applied_duration_s, "release_verified": execution.release_verified}


def summarize_episode(turns: list[dict[str, Any]]) -> dict[str, Any]:
    done = [t for t in turns if t["execution"] and t["execution"]["status"] == "completed"
            and t["execution"]["release_verified"] is True]
    ids = [t["effective_choice"] for t in done]
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
                      max_turns: int, section: dict[str, Any], save: Callable[[], None], clock: Callable[[], float]) -> TurnLoop:
    loop = TurnLoop(client, choices=offered, clock_mode=ClockMode.realtime, lease_s=HOLD_S)
    facts = Facts(snapshot)
    s1.bind(episode, loop, facts)
    by_id = {c.id: c for c in offered}
    controls = [c.id for c in offered if c.kind is ChoiceKind.action]
    previous_hold: dict[str, Any] | None = None
    while len(loop.turns) < max_turns:
        observed, settled = await facts.before_decision()
        if settled and previous_hold is not None:
            previous_hold["observed_displacement"] = observed
        started, before = clock(), len(client.executions)
        record = await loop.run_turn(s1.decide)
        execution = client.executions[-1] if len(client.executions) > before else None
        choice = by_id.get(record.effective_choice or "") or by_id.get(record.raw_choice or "")
        if record.outcome in (TurnOutcome.escalated, TurnOutcome.delegated) and s1.help_since_game_s is None:
            latest = loop.store.latest()
            s1.help_since_game_s = latest.game_s if latest is not None else None
        facts.note_turn(choice, record, execution)
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
        previous_hold = turn if record.outcome is TurnOutcome.executed and execution is not None else None
        save()
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
               output: Path, s1: LiveS1, *, episodes: int, max_turns: int, seed: int) -> None:
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
                          section=section, save=save, clock=lambda: time.monotonic() - epoch)
    trace["status"] = "complete"


async def _run(project: Path, selected: dict[str, Any], calibration: dict[str, Any], trace: dict[str, Any],
               output: Path, s1: LiveS1, episodes: int, max_turns: int, seed: int) -> None:
    client = await BridgeClient.connect_from_project(project, timeout_s=120.0)
    try:
        trace["unity_version"] = client.status.unity_version if client.status is not None else None
        await live(client, selected, calibration, trace, output, s1, episodes=episodes, max_turns=max_turns, seed=seed)
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
        recording._save(output, trace)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project", "entity", "calibration", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--approval", type=Path, default=Path(os.environ["UPA_LIVE_APPROVAL"]) if os.environ.get("UPA_LIVE_APPROVAL") else None)
    parser.add_argument("--episodes", type=int, default=int(os.environ.get("UPA_LIVE_EPISODES") or EPISODES))
    parser.add_argument("--max-turns", type=int, default=int(os.environ.get("UPA_LIVE_MAX_TURNS") or MAX_TURNS))
    parser.add_argument("--seed", type=int, default=int(os.environ.get("UPA_LIVE_SEED") or SEED))
    args = parser.parse_args(argv)
    project, output = args.project.resolve(), args.output.resolve()
    if args.approval is None:
        parser.error("--approval (or UPA_LIVE_APPROVAL) is required: no paid call starts without one")
    if output.is_relative_to(project) or output.exists():
        parser.error("--output must be a new file outside the Unity project")
    if not os.environ.get(replay.ARMS[ARM][1]):
        parser.error(f"{replay.ARMS[ARM][1]} is not set in the environment")
    try:
        approval, approval_sha = replay._load(args.approval)
        replay.validate_approval(approval, [ARM])
        limit, planned = approval["arms"][ARM], args.episodes * args.max_turns
        bound = replay._decimal(limit["per_call_max_usd"], "per-call bound")
        if planned > approval["max_calls"] or planned > limit["max_calls"] or bound * planned > replay._decimal(
                approval["max_cost_usd"], "overall limit") or bound * planned > replay._decimal(limit["max_cost_usd"], "arm limit"):
            raise ValueError("the approved call or cost envelope is smaller than the longest possible run")
        entity_bytes, calibration_bytes = args.entity.read_bytes(), args.calibration.read_bytes()
        selected, calibration = json.loads(entity_bytes), json.loads(calibration_bytes)
        repo = Path(__file__).resolve().parents[3]
        design = repo / "docs" / "design" / "s1-bench.md"
        plan = {"version": 1, "kind": "live-loop-smoke", "arm": ARM, "approval_sha256": approval_sha, "seed": args.seed,
                "episodes": args.episodes, "max_turns": args.max_turns, "max_possible_calls": planned,
                **{key: digest(path.read_bytes()) for key, path in SOURCES.items()},
                "design_sha256": digest(design.read_bytes()) if design.is_file() else None,
                "budget_assumptions": {"balance_override": approval.get("unverified_balance_override")}}
        trace: dict[str, Any] = {
            "version": 1, "kind": "live-loop-smoke", "status": "running", "error": None,
            "game_commit": recording._git_head(project), "repo_commit": recording._git_head(repo),
            "game_worktree_before": recording._game_worktree(project),
            "bridge_source_sha256": recording._source_hash(repo / "bridge", {".cs", ".asmdef", ".json"}),
            "python_source_sha256": recording._source_hash(repo / "src" / "playtest", {".py"}),
            "entity_sha256": digest(entity_bytes), "calibration_sha256": digest(calibration_bytes),
            "instructions_sha256": digest(INSTRUCTIONS), "objective_sha256": digest(OBJECTIVE),
            "recorded_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "clock_mode": "realtime",
            "settings": {"arm": ARM, "episodes": args.episodes, "max_turns": args.max_turns, "seed": args.seed,
                         "lease_s": HOLD_S, "max_age_s": 0.5, "freshness": "TurnLoop default",
                         "approval_sha256": approval_sha},
            "episodes": [], "stop": None}
        output.parent.mkdir(parents=True, exist_ok=True)
        replay._write(output.parent / "plan.json", plan, exclusive=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(trace, stream, allow_nan=False)
        s1 = LiveS1(output.parent, approval)
        asyncio.run(_run(project, selected, calibration, trace, output, s1, args.episodes, args.max_turns, args.seed))
    except (OSError, ValueError, BridgeError, BridgeLockError) as exc:
        print(f"live: {exc}", file=sys.stderr)
        return 1
    print(f"live run {trace['status']}: {trace.get('totals')}")
    return 0 if trace["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
