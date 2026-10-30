"""Paid frontier HelpPort used only by explicitly enabled live runner sessions."""
from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any, Callable, Mapping

from playtest.bench import call_budget
from playtest.bench.record import digest
from playtest.contracts.bridge import Observation
from playtest.contracts.investigation import Directive
from playtest.contracts.loop import ChoiceKind, ChoiceSet, TurnOutcome, TurnRecord
from playtest.frontier import media, provider
from playtest.frontier.packet import build_packet
from playtest.roles import frontier
from playtest.runner.directives import DirectiveTracker, field_value


class FrontierHelp:
    def __init__(self, *, project: Path, status: Any, manifest: Any, directory: Path,
                 approval: Mapping[str, Any], sink: Any, tracker: DirectiveTracker,
                 started_utc: dt.datetime, send: Callable[..., Any] = provider.send_meta,
                 capture: Callable[..., media.Clip] = media.capture_clip,
                 max_calls: int | None = None) -> None:
        self.project, self.status, self.manifest = project, status, manifest
        self.directory, self.sink, self.tracker = directory, sink, tracker
        self.started_utc, self.send, self.capture = started_utc, send, capture
        # S1 keeps choosing while this request is pending, so the two arms' calls overlap by design.
        self.ledger = call_budget.Ledger(directory, approval, provider.MODEL, concurrent_arms=True)
        self.max_calls = max_calls if max_calls is not None else self.ledger.limit["max_calls"]
        if isinstance(self.max_calls, bool) or not isinstance(self.max_calls, int) or self.max_calls < 1:
            raise ValueError("max_calls must be a positive integer")
        self.calls = 0
        self.hypotheses: dict[str, dict[str, Any]] = {}
        fields = {f"public.{reader.alias}": reader.kind for reader in manifest.readers if reader.kind}
        self.reader_fields = fields

    async def run(self, request: Any) -> Any:
        from playtest.core.ports import HelpResult
        context = request.context
        as_of, raw_obs = context["as_of"], context["observation"]
        seq = int(as_of["seq"])
        if self.calls >= self.max_calls:
            obs_trace = (self.sink.trace_seq_for_observation(seq)
                         if hasattr(self.sink, "trace_seq_for_observation") else None)
            self.sink.append("investigation", {
                "event": "not_called", "request_turn": request.turn, "based_on_seq": seq,
                "outcome_seq": seq, "hypothesis": None, "directive": None,
                "outcome": "unknown", "evidence_trace_seq": obs_trace,
                "reason": "the approved frontier call cap was reached",
            }, author="frontier", turn=None, refs=[obs_trace] if obs_trace is not None else [])
            return HelpResult(source="frontier", result={"error": "approved frontier call cap reached",
                                                           "hypotheses": [], "directive": None})
        now = dt.datetime.now(dt.timezone.utc)
        observation = Observation(
            session="runner", seq=seq, game_s=as_of.get("game_s"), wall_utc=now,
            clock_mode="realtime", scene=raw_obs["scene"], playing=raw_obs["playing"],
            paused=raw_obs["paused"], time_scale=raw_obs["time_scale"], frame=0,
            public=raw_obs.get("public") or {}, ui=raw_obs.get("ui"),
            reader_errors=raw_obs.get("reader_errors") or [],
        )
        choices = ChoiceSet(set_version=context["choice_set_version"], built_from_seq=seq,
                            choices=context["choices"])
        self.role_manifest = self._manifest_for(choices)
        turn = TurnRecord(turn=request.turn, set_version=choices.set_version, based_on_seq=seq,
                          raw_choice=request.choice_id, effective_choice=request.choice_id,
                          executed_action=None, outcome=TurnOutcome.escalated)
        folder = self.directory / "clips" / f"t{request.turn}"
        clip = await asyncio.to_thread(self.capture, project=self.project, status=self.status,
                                       since_utc=self.started_utc, until_utc=now, output_dir=folder)
        fact_rows = [{"text": text, "seqs": [seq]} for text in context.get("facts", []) if isinstance(text, str)]
        ids = [f"hyp-{request.turn}-{i}" for i in range(frontier.MAX_HYPOTHESES)]
        old_hypotheses = [row for row in self.hypotheses.values() if row.get("status") == "open"]
        current = (Directive.model_validate(self.tracker.active["directive"])
                   if self.tracker.active is not None and self.tracker.active.get("directive") else None)
        history = [Directive.model_validate(item["directive"]) for item in self.tracker.history
                   if item.get("status") != "open" and item.get("directive")]
        packet = build_packet(
            objective=context["objective"], observation=observation, choices=choices,
            escalation=turn, facts=fact_rows, directive=current, directive_history=history,
            recent=[], hypotheses=old_hypotheses,
            new_hypothesis_ids=ids, age_s=context.get("as_of", {}).get("age_s"),
            clip=clip.descriptor, clip_unavailable_reason=clip.reason,
        )
        # The live S1 context does not expose the bridge frame number. Do not send the
        # placeholder needed by Observation's local constructor as if it were measured.
        packet.payload["observation"].pop("frame", None)
        body = provider.build_request(self.role_manifest, packet, clip_path=clip.path)
        from playtest.core.context import canonical
        reservation = self.ledger.reserve(
            episode=0, turn=request.turn, based_on_seq=seq, request_sha256=digest(canonical(body)),
            requested_model=provider.MODEL,
            extra={"role": "frontier", "request_turn": request.turn,
                   "packet_sha256": digest(packet.canonical()), "clip": clip.descriptor,
                   "clip_unavailable_reason": clip.reason},
        )
        self.calls += 1
        try:
            status, data, latency, problem = await asyncio.to_thread(self.send, body)
            result = provider.decode_response(data if status == 200 else None, self.role_manifest, packet)
            answer = result.answer
            if status != 200 or answer is None:
                details = {"error": problem or ("invalid frontier answer" if answer is None else f"HTTP {status}"),
                           "errors": result.errors, "hypothesis": None, "directive": None}
                reservation.finish(http_status=status, latency_ms=latency, cost_usd=result.cost_usd,
                                         resolved_model=result.resolved_model, usage=result.usage,
                                         answer=None, parse_valid=False, **details)
                obs_trace = (self.sink.trace_seq_for_observation(seq)
                             if hasattr(self.sink, "trace_seq_for_observation") else None)
                self.sink.append("investigation", {
                    "event": "invalid_answer", "request_turn": request.turn, "based_on_seq": seq,
                    "outcome_seq": seq, "hypothesis": None, "directive": None, "outcome": "unknown",
                    "evidence_trace_seq": obs_trace, "reason": details["error"],
                }, author="frontier", turn=None, refs=[obs_trace] if obs_trace is not None else [])
                return HelpResult(source="frontier", result=details)
            review = frontier.review(answer, self.role_manifest, packet.refs)
            accepted = [item for i, item in enumerate(answer.hypotheses)
                        if not review.rejected(f"hypotheses[{i}]")]
            for hypothesis in accepted:
                self.hypotheses[hypothesis.id] = hypothesis.model_dump(mode="json")
            directive = None
            if answer.directive is not None and not review.rejected("directive"):
                directive = frontier.to_directive(answer.directive, directive_id=f"d-{request.turn}",
                    parent=current.directive_id if current is not None else None,
                    model=result.resolved_model or provider.MODEL,
                    based_on_seq=seq, received_game_s=observation.game_s)
            result_payload = {
                "hypotheses": [h.model_dump(mode="json") for h in accepted],
                "directive": directive.model_dump(mode="json") if directive is not None else None,
                "errors": list(result.errors),
                "review": [{"item": p.item, "reason": p.reason} for p in review.problems],
            }
            obs_trace = (self.sink.trace_seq_for_observation(seq)
                         if hasattr(self.sink, "trace_seq_for_observation") else None)
            self.sink.append("investigation", {
                "event": "hypothesis_directive", "request_turn": request.turn, "based_on_seq": seq,
                "outcome_seq": seq,
                "hypothesis": result_payload["hypotheses"], "directive": result_payload["directive"],
                "outcome": "open" if directive is not None else "unknown", "evidence_trace_seq": obs_trace,
                "reason": None if directive is not None else "no accepted directive was returned",
            }, author="frontier", turn=None, refs=[obs_trace] if obs_trace is not None else [])
            if directive is not None:
                expected = directive.expected
                kind = self.role_manifest.fields.get(expected.field) if expected else "other"
                baseline = field_value(observation.public, expected.field.removeprefix("public.")) if expected else None
                self.tracker.adopt({**directive.model_dump(mode="json"),
                               "hypothesis": result_payload["hypotheses"], "kind": kind, "baseline": baseline,
                               "scene": observation.scene, "last_frame": observation.frame,
                               "last_game_s": observation.game_s, "directive": directive.model_dump(mode="json")},
                              request_turn=request.turn, based_on_seq=seq, evidence_trace_seq=obs_trace)
            reservation.finish(http_status=status, latency_ms=latency, cost_usd=result.cost_usd,
                               resolved_model=result.resolved_model, usage=result.usage,
                               parse_valid=True, answer=answer.model_dump(mode="json"))
            return HelpResult(source="frontier", result=result_payload)
        except asyncio.CancelledError:
            reservation.finish(directive_result="cancelled", cost_usd=None)
            raise
        except Exception:
            reservation.finish(directive_result="failed", cost_usd=None)
            raise

    def _manifest_for(self, choices: ChoiceSet) -> frontier.Manifest:
        """Restrict frontier choice names to the executable choices this run actually offers."""
        declarations = {item.name: item for item in self.manifest.actions}
        skills_declared = {item.name for item in self.manifest.skills}
        actions: dict[str, frontier.ActionSpec] = {}
        skills: list[str] = []
        for choice in choices.choices:
            if choice.kind is ChoiceKind.action and choice.action in declarations:
                declared = declarations[choice.action]
                actions[choice.action] = frontier.ActionSpec(
                    axis=declared.axis,
                    max_hold_s=declared.hold_s if declared.hold_s is not None else declared.tap_hold_s,
                )
            elif choice.kind is ChoiceKind.skill and choice.skill in skills_declared:
                skills.append(choice.skill)
        return frontier.Manifest(fields=self.reader_fields, actions=actions, skills=skills)
