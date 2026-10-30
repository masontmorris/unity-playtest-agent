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
from playtest.contracts.trace import TraceAuthor
from playtest.core.findings import FindingLog
from playtest.frontier import media, provider
from playtest.frontier.packet import build_packet
from playtest.roles import frontier
from playtest.roles.checks import field_value
from playtest.runner.directives import DirectiveTracker


def reader_calibration(record: Mapping[str, Any] | None, readers: Any) -> dict[str, str]:
    """Each reader's freshness class from the calibration record, keyed by its observation path.

    Preflight measured these on this build before play (`preflight.freshness`). A reader with no class, or one
    the frontier has no wording for, is left out, so it stays unknown rather than looking measured.
    """
    freshness = (record or {}).get("freshness") or {}
    out = {}
    for reader in readers:
        measured = freshness.get(getattr(reader, "field_id", None)) if isinstance(freshness, Mapping) else None
        label = measured.get("classification") if isinstance(measured, Mapping) else None
        if label in provider.CALIBRATION_LABELS:
            out[f"public.{reader.alias}"] = label
    return out


def reader_notes(readers: Any) -> dict[str, dict[str, str]]:
    """What the manifest says about each reader: the game field it reads and the preflight model's note.

    Both come from the preflight manifest, where game knowledge lives. A reader without them gets none.
    """
    out: dict[str, dict[str, str]] = {}
    for reader in readers:
        descriptor = getattr(reader, "descriptor", None)
        descriptor = descriptor if isinstance(descriptor, Mapping) else {}
        owner, name = descriptor.get("declaring_type"), descriptor.get("name")
        note: dict[str, str] = {}
        if isinstance(owner, str) and owner and isinstance(name, str) and name:
            note["source"] = f"{owner}.{name}"
        why = getattr(reader, "why", None)
        if isinstance(why, str) and why.strip():
            note["preflight_note"] = why
        if note:
            out[f"public.{reader.alias}"] = note
    return out


class FrontierHelp:
    def __init__(self, *, project: Path, status: Any, manifest: Any, directory: Path,
                 approval: Mapping[str, Any], sink: Any, tracker: DirectiveTracker,
                 started_utc: dt.datetime, send: Callable[..., Any] = provider.send_meta,
                 capture: Callable[..., media.Clip] = media.capture_clip,
                 max_calls: int | None = None, calibration: Mapping[str, Any] | None = None) -> None:
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
        self._shown: dict[str, dict[str, Any]] = {}  # each hypothesis as S1's guidance last listed it
        self.findings: list[dict[str, Any]] = []  # candidate findings written to the trace
        fields = {f"public.{reader.alias}": reader.kind for reader in manifest.readers if reader.kind}
        self.reader_fields = fields
        self.reader_calibration = reader_calibration(calibration, manifest.readers)
        self.reader_notes = reader_notes(manifest.readers)

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
            findings=[{key: f[key] for key in ("finding_id", "title", "repro_check")} for f in self.findings],
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
            reason = None
            if directive is None:
                rejected = [p.reason for p in review.problems if p.item == "directive"]
                reason = (f"the directive was rejected: {'; '.join(rejected)}" if rejected
                          else "no accepted directive was returned")
            self.sink.append("investigation", {
                "event": "hypothesis_directive", "request_turn": request.turn, "based_on_seq": seq,
                "outcome_seq": seq,
                "hypothesis": result_payload["hypotheses"], "directive": result_payload["directive"],
                "outcome": "open" if directive is not None else "unknown", "evidence_trace_seq": obs_trace,
                "reason": reason,
            }, author="frontier", turn=None, refs=[obs_trace] if obs_trace is not None else [])
            if directive is not None:
                expected = directive.expected
                kind = self.role_manifest.fields.get(expected.field) if expected else "other"
                # Only the fallback: the tracker re-takes a changed/unchanged baseline when S1 first sees the directive.
                baseline = field_value(observation.public, expected.field.removeprefix("public.")) if expected else None
                self.tracker.adopt({**directive.model_dump(mode="json"),
                               "hypothesis": result_payload["hypotheses"], "kind": kind, "baseline": baseline,
                               "scene": observation.scene, "last_frame": observation.frame,
                               "last_game_s": observation.game_s, "requested_game_s": observation.game_s,
                               "directive": directive.model_dump(mode="json")},
                              request_turn=request.turn, based_on_seq=seq, evidence_trace_seq=obs_trace)
            if answer.candidate_finding is not None and not review.rejected("candidate_finding"):
                result_payload["candidate_finding"] = self._record_finding(answer.candidate_finding, request.turn)
            reservation.finish(http_status=status, latency_ms=latency, cost_usd=result.cost_usd,
                               resolved_model=result.resolved_model, usage=result.usage,
                               parse_valid=True, answer=answer.model_dump(mode="json"))
            return HelpResult(source="frontier", result=self._guidance(result_payload))
        except asyncio.CancelledError:
            reservation.finish(directive_result="cancelled", cost_usd=None)
            raise
        except Exception:
            reservation.finish(directive_result="failed", cost_usd=None)
            raise

    def _record_finding(self, proposed: Any, turn: int) -> dict[str, Any]:
        """Write the frontier's candidate finding to the trace, citing trace entries, and say what happened.

        The answer cites bridge observation seqs, which review checked against the packet. A trace finding
        must cite earlier trace entries, so each seq maps to the entry that recorded its observation. If any
        has none, the finding is not written and the reason is returned: a citation is never dropped or
        guessed to make it fit.
        """
        finding_id = f"f-{turn}"
        mapper = getattr(self.sink, "trace_seq_for_observation", None)
        mapped = [mapper(seq) if mapper is not None else None for seq in proposed.evidence_seq]
        if not mapped or any(seq is None for seq in mapped):
            missing = [seq for seq, trace_seq in zip(proposed.evidence_seq, mapped) if trace_seq is None]
            return {"finding_id": finding_id, "trace_seq": None,
                    "reason": f"not recorded: cited observations with no trace entry: {missing or 'none cited'}"}
        finding = frontier.to_finding(proposed, finding_id=finding_id, evidence_seq=sorted(set(mapped)))
        trace_seq = FindingLog(self.sink, author=TraceAuthor.frontier).record(finding)
        self.findings.append({"finding_id": finding_id, "trace_seq": trace_seq, "request_turn": turn,
                              "title": finding.title,
                              "repro_check": finding.repro_check.model_dump(mode="json") if finding.repro_check else None})
        return {"finding_id": finding_id, "trace_seq": trace_seq, "reason": None}

    def _guidance(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """What S1 keeps from one answer: each hypothesis listed once, and the directive by id.

        An adopted result stays in every later S1 context. The frontier restates every open hypothesis
        in each answer, usually reworded, and full copies of every answer grew the prompt, and with it
        S1's latency, until most actions reached the bridge stale (#132).
        - A hypothesis is listed in full when its id is new or its status changed. A restatement with the
          same status is listed by id in `restated_hypotheses`, so no hypothesis is hidden.
        - The trace's `investigation` entries keep every version, and the frontier's own requests carry
          the latest.
        - The directive is named by id. The directive tracker states its setpoint, deadline and current or
          final outcome as facts every turn; a copy frozen at adoption could only go stale.
        """
        fresh: list[Any] = []
        restated: list[str] = []
        for row in payload.get("hypotheses") or []:
            key = row.get("id") if isinstance(row, Mapping) else None
            known = self._shown.get(key) if isinstance(key, str) else None
            if known is not None and known.get("status") == row.get("status"):
                restated.append(key)
                continue
            fresh.append(row)
            if isinstance(key, str):
                self._shown[key] = row
        view: dict[str, Any] = {"hypotheses": fresh}
        if restated:
            view["restated_hypotheses"] = restated
        directive = payload.get("directive")
        view["directive_id"] = directive.get("directive_id") if isinstance(directive, Mapping) else None
        finding = payload.get("candidate_finding")
        if isinstance(finding, Mapping):
            view["candidate_finding"] = {key: finding.get(key) for key in ("finding_id", "reason")}
        for key in ("errors", "review"):
            if payload.get(key):
                view[key] = payload[key]
        return view

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
        return frontier.Manifest(fields=self.reader_fields, actions=actions, skills=skills,
                                 calibration=self.reader_calibration, field_notes=self.reader_notes)
