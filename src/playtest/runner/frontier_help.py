"""Paid frontier HelpPort used only by explicitly enabled live runner sessions."""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from playtest.bench import call_budget
from playtest.bench.record import digest
from playtest.contracts.bridge import TERMINAL_STATUSES, Observation
from playtest.contracts.investigation import Directive
from playtest.contracts.loop import ChoiceKind, ChoiceSet, TurnOutcome, TurnRecord
from playtest.contracts.trace import TraceAuthor
from playtest.core.findings import FindingLog, suspicions_in
from playtest.core.ports import HelpResult
from playtest.core.trace import read_trace
from playtest.frontier import media, provider
from playtest.frontier.packet import build_packet
from playtest.frontier.request_archive import preserve_request
from playtest.roles import frontier
from playtest.roles.checks import field_value
from playtest.runner import guidance_codec
from playtest.runner.choices import UNIT
from playtest.runner.directives import DirectiveTracker
from playtest.runner.frontier_history import recent_records
from playtest.runner.probe import SURROUNDINGS_FIELD, allowance, describe_surroundings
from playtest.runner.progress import ProgressProbe, progress_rows


def action_effects(expected: Mapping[str, Mapping[str, Any] | None]) -> dict[str, frontier.Effect]:
    """Each action's calibrated net displacement and its allowance, keyed by action name.

    `expected` is `build_choices(manifest).expected`: the effect calibration measured for exactly that declaration,
    or None. An action with none is left out, so the frontier is never offered an effect check with nothing to
    compare it with. The allowance is the one the live effect probe states (`probe.allowance`); it is None when
    calibration recorded no spread.
    """
    out: dict[str, frontier.Effect] = {}
    for name, effect in expected.items():
        shown = effect.get("net_displacement") if effect else None
        if isinstance(shown, bool) or not isinstance(shown, (int, float)) or not math.isfinite(shown):
            continue
        bounds = allowance(effect)
        out[name] = frontier.Effect(expected=float(shown), allowance=bounds[1] if bounds is not None else None,
                                    unit=str(effect.get("unit") or UNIT))
    return out


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
    """What the manifest records about each reader, kept apart by where it came from.

    - `source`: the game field the reader reads, from the descriptor preflight's field inspection reflected. It
      identifies the field; it says nothing about how the field should behave.
    - `preflight_note`: the preflight model's own reason for choosing the field, exactly as recorded. A model wrote
      it and nothing verified it, so it is an inference. `provider.manifest_message` sends it labelled as one
      (`provider.NOTE_ORIGIN`, `provider.NOTE_VERIFICATION`).

    Both come from the preflight manifest, where game knowledge lives. Whichever the manifest lacks is left out,
    never filled in, and no note is dropped, reworded or judged by what it says.
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


def declared_progress(regions: Any) -> dict[str, frontier.ProgressRegion]:
    """The manifest's reach declarations as the frontier is shown them, keyed by region id.

    Nothing is measured here. The declaration is copied as authored: its label, purpose, frame, scene, bounds,
    pose source, author, source and verification status. An empty manifest yields no rows, so the frontier
    request keeps exactly its old fields and sections.
    """
    out: dict[str, frontier.ProgressRegion] = {}
    for region in regions or ():
        out[region.region_id] = frontier.ProgressRegion(
            region_id=region.region_id, label=region.label, purpose=region.purpose, frame=region.frame,
            scene=region.scene, minimum=tuple(float(value) for value in region.bounds.min),
            maximum=tuple(float(value) for value in region.bounds.max), pose=region.pose,
            author=region.author, source=region.source, verification=region.verification)
    return out


def recorded_suspicions(sink: Any) -> list[dict[str, Any]]:
    """Every suspicion the trace holds so far, oldest first, as rows for the packet.

    A `MemorySink` lists its entries by kind. The file-backed `TraceWriter` keeps no entries, but it flushes each
    one as it writes it, so its file is the trace so far and is read back whole. A line that does not parse is an
    error here, not a gap: a suspicion is never left out because the file could not be read. Nothing is filtered,
    merged or reordered; the packet's cap is the only limit.
    """
    of_kind = getattr(sink, "of_kind", None)
    if of_kind is not None:
        entries = of_kind("suspicion")
        context = [entry for kind in ("observation", "effect") for entry in of_kind(kind)]
    else:
        entries = context = read_trace(sink.path)
    attempts = _attempts(context)
    rows = []
    for raised in suspicions_in(entries):
        row = {"suspicion_id": raised.suspicion.suspicion_id, "basis": raised.suspicion.basis.value,
               "subject": raised.suspicion.subject, "turn": raised.suspicion.turn,
               "statement": raised.suspicion.statement}
        if raised.suspicion.basis.value == "effect_mismatch":
            later = [line for turn, line in attempts if turn > raised.suspicion.turn][:FOLLOW_THROUGH_ATTEMPTS]
            if later:
                row["followed_by"] = later
        rows.append(row)
    return rows


# A shortfall suspicion carries the next few measured attempts after it, so the order of what happened at that
# spot (what stopped, what got past, what moved again) survives after the recent rows have moved on.
FOLLOW_THROUGH_ATTEMPTS = 6


def _entry(entry: Any) -> tuple[str, int | None, Mapping[str, Any]]:
    if isinstance(entry, Mapping):
        return str(getattr(entry["kind"], "value", entry["kind"])), entry.get("turn"), entry["body"]
    return entry.kind.value, entry.turn, entry.body


def _anchor_height(observation: Mapping[str, Any] | None) -> float | None:
    probe = ((observation or {}).get("public") or {}).get(SURROUNDINGS_FIELD)
    position = ((probe or {}).get("anchor") or {}).get("position") if isinstance(probe, Mapping) else None
    value = position[1] if isinstance(position, list) and len(position) == 3 else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _attempts(entries: Any) -> list[tuple[int, str]]:
    """One measured line per executed action, in trace order: turn, action, displacement against its calibrated
    value, the anchor's height change to the next observation, and the surroundings measured before it."""
    observations: dict[int, Mapping[str, Any]] = {}
    effects: list[tuple[int, Mapping[str, Any]]] = []
    for entry in entries:
        kind, turn, body = _entry(entry)
        if not isinstance(turn, int):
            continue
        if kind == "observation":
            observations.setdefault(turn, body)
        elif kind == "effect":
            effects.append((turn, body))
    lines = []
    for turn, body in effects:
        observed = (body.get("observed") or {}).get("net_displacement")
        expected = (body.get("expected") or {}).get("net_displacement")
        moved = f"{observed:.3f}" if isinstance(observed, (int, float)) else "unmeasured"
        calibrated = f"{expected:.3f}" if isinstance(expected, (int, float)) else "none recorded"
        line = f"turn {turn} {body.get('choice_id') or body.get('action')}: moved {moved} {UNIT} (calibrated {calibrated})"
        before, after = _anchor_height(observations.get(turn)), _anchor_height(observations.get(turn + 1))
        line += (f"; anchor height {after - before:+.2f} by the next observation" if before is not None and after is not None
                 else "; anchor height change unknown")
        surroundings = describe_surroundings(((observations.get(turn) or {}).get("public") or {}).get(SURROUNDINGS_FIELD))
        if surroundings:
            line += f"; before it: {surroundings}"
        lines.append((turn, line))
    return lines


def _evidence_snapshot(sink: Any, recent: list[dict[str, Any]] | None = None,
                       observation_seq: int | None = None) -> dict[int, dict[str, Any]]:
    """Copy only request-relevant citation entries before the helper yields to another turn."""
    of_kind = getattr(sink, "of_kind", None)
    entries = ([entry for kind in ("observation", "execution", "effect") for entry in of_kind(kind)]
               if of_kind is not None else read_trace(sink.path))
    seqs = ({observation_seq, *(row["seq"] for row in recent)} if recent is not None else None)
    refs = ({value for row in recent if row.get("record_kind") == "action_attempts"
             for value in (row.get("trace_seq"), row.get("effect_trace_seq")) if type(value) is int}
            if recent is not None else None)
    snapshot: dict[int, dict[str, Any]] = {}
    for entry in entries:
        if hasattr(entry, "model_dump"):
            kind, trace_seq, turn, body, entry_refs = (entry.kind.value, entry.trace_seq, entry.turn,
                                                        entry.body, entry.refs)
        else:
            kind, trace_seq, turn, body, entry_refs = (entry["kind"], entry["trace_seq"], entry["turn"],
                                                        entry["body"], entry["refs"])
        if kind == "observation":
            if seqs is not None and body.get("seq") not in seqs:
                continue
            # The citation needs the bridge sequence, session and turn, not a copy of the observation's
            # contents. A restart may reuse a `seq`, so the session is what tells the entries apart.
            body = {"seq": body.get("seq"), "session": body.get("session")}
        elif kind in ("execution", "effect"):
            if refs is not None and trace_seq not in refs:
                continue
        else:
            continue
        snapshot[trace_seq] = {"kind": kind, "turn": turn, "body": copy.deepcopy(body),
                               "refs": copy.deepcopy(entry_refs)}
    return snapshot


def _observation_ref(trace: Mapping[int, Mapping[str, Any]], seq: int,
                     session: str | None = None) -> int | None:
    """The entry that observed `seq`: the current session's when both observed it, else a unique one."""
    matches = [ref for ref, entry in trace.items()
               if entry["kind"] == "observation" and entry["body"].get("seq") == seq]
    if session is not None:
        current = [ref for ref in matches if trace[ref]["body"].get("session") == session]
        if current:
            return current[0] if len(current) == 1 else None
    return matches[0] if len(matches) == 1 else None


def _same_measurement(a: Any, b: Any) -> bool:
    """JSON equality that does not mistake a boolean for a number."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return a.keys() == b.keys() and all(_same_measurement(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same_measurement(x, y) for x, y in zip(a, b))
    return a == b


@dataclass(frozen=True)
class _FindingEvidence:
    refs: tuple[int, ...] = ()
    reason: str | None = None


def _finding_evidence(proposed: Any, recent: list[dict[str, Any]],
                      trace: Mapping[int, Mapping[str, Any]]) -> _FindingEvidence:
    """Resolve bridge citations and every forwarded attempt at those observations, or refuse them all."""
    refs: set[int] = set()
    observations: dict[int, list[int]] = {}
    for trace_seq, entry in trace.items():
        if entry["kind"] == "observation":
            observations.setdefault(entry["body"].get("seq"), []).append(trace_seq)
    if not proposed.evidence_seq:
        return _FindingEvidence(reason="not recorded: no observations cited")
    for seq in sorted(set(proposed.evidence_seq)):
        matches = observations.get(seq, [])
        if len(matches) != 1:
            return _FindingEvidence(reason=f"not recorded: cited observations with no unique trace entry: [{seq}]")
        obs_ref = matches[0]
        refs.add(obs_ref)
        for row in recent:
            if row.get("record_kind") != "action_attempts" or row.get("seq") != seq:
                continue
            # Do not trust a number merely because it happens to identify an existing trace entry.
            def refusal(detail: str) -> _FindingEvidence:
                return _FindingEvidence(reason=f"not recorded: history at observation {seq} has {detail}")

            required = {"observation_trace_seq", "trace_seq", "choice_id", "purpose", "effect_trace_seq",
                        "expected_effect", "observed_effect", "effect_fact"}
            if not required <= row.keys():
                return refusal("incomplete provenance")
            if (not _same_measurement(row.get("observation_trace_seq"), obs_ref)
                    or trace[obs_ref].get("turn") != row.get("turn")):
                return refusal("an unavailable or mismatched observation reference")
            ex_ref = row.get("trace_seq")
            if isinstance(ex_ref, bool) or not isinstance(ex_ref, int):
                return refusal("an unavailable execution reference")
            execution = trace.get(ex_ref)
            if not execution or execution.get("kind") != "execution" or execution.get("turn") != row.get("turn"):
                return refusal("an unavailable or mismatched execution reference")
            body = execution.get("body") or {}
            request = body.get("request") or {}
            if (not isinstance(request, Mapping) or not isinstance(request.get("action"), str)
                    or not request["action"] or not isinstance(request.get("choice_id"), str)
                    or not request["choice_id"] or request["choice_id"] != row.get("choice_id")
                    or not _same_measurement(request.get("based_on_seq"), seq)
                    or body.get("purpose") not in ("choice", "while_pending")
                    or body.get("purpose") != row.get("purpose")):
                return refusal("a mismatched execution binding")
            records = body.get("records")
            if not isinstance(records, list) or (records and not isinstance(records[-1], Mapping)):
                return refusal("invalid execution records")
            last = records[-1] if records else {}
            terminal = last if last.get("status") in TERMINAL_STATUSES else {}
            measured = {"terminal_status": terminal.get("status"), "reject_reason": terminal.get("reject_reason"),
                        "applied_duration_s": terminal.get("applied_duration_s"),
                        "release_verified": terminal.get("release_verified"),
                        "bridge_expected_effects": terminal.get("expected_effect") or None,
                        "bridge_observed_effects": terminal.get("observed_effect") or None}
            if any(key not in row or not _same_measurement(row[key], value) for key, value in measured.items()):
                return refusal("execution measurements that differ from the trace")
            refs.add(ex_ref)
            effect_ref = row.get("effect_trace_seq")
            if effect_ref is None:
                if any(row.get(key) is not None for key in ("expected_effect", "observed_effect", "effect_fact")):
                    return refusal("effect measurements without an effect reference")
                continue
            if isinstance(effect_ref, bool) or not isinstance(effect_ref, int):
                return refusal("an unavailable effect reference")
            effect = trace.get(effect_ref)
            if (not effect or effect.get("kind") != "effect" or effect.get("turn") != row.get("turn")
                    or ex_ref not in effect.get("refs", [])):
                return refusal("an unavailable or mismatched effect reference")
            effect_body = effect.get("body") or {}
            if (effect_body.get("choice_id") != request.get("choice_id")
                    or effect_body.get("action") != request.get("action")
                    or any(key not in row or not _same_measurement(row[key], effect_body.get(trace_key))
                           for key, trace_key in (("expected_effect", "expected"), ("observed_effect", "observed")))):
                return refusal("effect binding or measurements that differ from the trace")
            refs.add(effect_ref)
    return _FindingEvidence(refs=tuple(sorted(refs)))


@dataclass
class _StagedResult:
    result: HelpResult
    payload: dict[str, Any]
    directive_row: dict[str, Any] | None
    keep_row: dict[str, Any] | None
    based_on_seq: int
    evidence_trace_seq: int | None
    candidate: Any | None
    finding_evidence: _FindingEvidence | None


class FrontierHelp:
    def __init__(self, *, project: Path, status: Any, manifest: Any, directory: Path,
                 approval: Mapping[str, Any], sink: Any, tracker: DirectiveTracker,
                 started_utc: dt.datetime, send: Callable[..., Any] = provider.send_meta,
                 capture: Callable[..., media.Clip] = media.capture_clip,
                 max_calls: int | None = None, calibration: Mapping[str, Any] | None = None,
                 effects: Mapping[str, frontier.Effect] | None = None,
                 generation: str | None = None, session: str | None = None,
                 clip_window_s: float = media.WINDOW_S,
                 progress: ProgressProbe | None = None) -> None:
        # A sink that cannot be read back is refused now, before any paid call, not at the first request.
        if not hasattr(sink, "of_kind") and not hasattr(sink, "path"):
            raise ValueError("the trace sink must list its entries (of_kind) or name its file (path), so the "
                             "frontier can be shown the suspicions recorded so far")
        self.project, self.status, self.manifest = project, status, manifest
        self.directory, self.sink, self.tracker = directory, sink, tracker
        self.started_utc, self.send, self.capture = started_utc, send, capture
        self.generation = generation
        self.clip_window_s = clip_window_s
        self.session = session
        # S1 keeps choosing while this request is pending, so the two arms' calls overlap by design.
        self.ledger = call_budget.Ledger(directory, approval, provider.MODEL, concurrent_arms=True)
        self.max_calls = max_calls if max_calls is not None else self.ledger.limit["max_calls"]
        if isinstance(self.max_calls, bool) or not isinstance(self.max_calls, int) or self.max_calls < 1:
            raise ValueError("max_calls must be a positive integer")
        self.calls = 0
        self.hypotheses: dict[str, dict[str, Any]] = {}
        self._shown: dict[str, dict[str, Any]] = {}  # each hypothesis as S1's guidance last listed it
        self._staged: dict[int, _StagedResult] = {}
        self._adopted: dict[int, tuple[HelpResult, HelpResult]] = {}
        self._discarded: set[int] = set()  # also prevents a still-running request from staging later
        self.findings: list[dict[str, Any]] = []  # candidate findings written to the trace
        self.progress = declared_progress(getattr(manifest, "reach_regions", ()))
        self.progress_probe = (progress if progress is not None
                               else ProgressProbe(getattr(manifest, "reach_regions", ())))
        fields = {f"public.{reader.alias}": reader.kind for reader in manifest.readers if reader.kind}
        # A declared region's distance is the only derived field a setpoint or check may name; membership is
        # shown in the packet but is not a field. Empty for a manifest that declared no region.
        fields.update({region.distance_field: "number" for region in self.progress.values()})
        self.reader_fields = fields
        self.reader_calibration = reader_calibration(calibration, manifest.readers)
        self.reader_notes = reader_notes(manifest.readers)
        self.effects = dict(effects or {})  # run-stable: it names the effect checks a finding may propose

    def rebind(self, *, client: Any = None, observation: Any = None, generation: str | None = None) -> None:
        """Follow the restarted play instance: clips come only from its frames and its own start time.

        Nothing staged or adopted for the earlier generation may become guidance later: every turn it holds
        is marked discarded, so a request that outlives the boundary and finishes later cannot stage. An
        open hypothesis was formed against the earlier session, so it is withdrawn here â€” kept, with the
        reason and the generation, as attributed history rather than current direction. The directive
        tracker is invalidated at once for the same reason.
        """
        if generation is None or observation is None:
            raise ValueError("rebinding the frontier helper needs the new generation and first observation")
        self.generation = generation
        self.started_utc = observation.wall_utc
        self.session = getattr(client, "session", self.session) if client is not None else self.session
        for turn in (*self._staged, *self._adopted):
            self._discarded.add(turn)
        self._staged.clear()
        self._adopted.clear()
        self.tracker.rebind(client=client, observation=observation, generation=generation)
        for row in self.hypotheses.values():
            if row.get("status") == "open":
                row["status"] = "withdrawn"
                row["invalidated_reason"] = "the play session restarted"
                row["invalidated_generation"] = generation
        self._shown.clear()

    async def run(self, request: Any) -> Any:
        context = copy.deepcopy(request.context)
        as_of, raw_obs = context["as_of"], context["observation"]
        seq = int(as_of["seq"])
        if self.calls >= self.max_calls:
            obs_trace = (self.sink.trace_seq_for_observation(seq, self.session)
                         if hasattr(self.sink, "trace_seq_for_observation") else None)
            self.sink.append("investigation", {
                "event": "not_called", "request_turn": request.turn, "based_on_seq": seq,
                "outcome_seq": seq, "hypothesis": None, "directive": None, "keep": None,
                "outcome": "unknown", "evidence_trace_seq": obs_trace,
                "reason": "the approved frontier call cap was reached",
            }, author="frontier", turn=None, refs=[obs_trace] if obs_trace is not None else [])
            return HelpResult(source="frontier", result={"error": "approved frontier call cap reached",
                                                           "hypotheses": [], "directive": None})
        # The trace as of this request, read before the first await: S1 keeps choosing while the clip is captured.
        suspicions = recorded_suspicions(self.sink)
        recent = recent_records(context)
        evidence_snapshot = _evidence_snapshot(self.sink, recent, seq)
        obs_trace = _observation_ref(evidence_snapshot, seq, self.session)
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
        role_manifest = self._manifest_for(choices)
        turn = TurnRecord(turn=request.turn, set_version=choices.set_version, based_on_seq=seq,
                          raw_choice=request.choice_id, effective_choice=request.choice_id,
                          executed_action=None, outcome=TurnOutcome.escalated)
        folder = self.directory / "clips" / f"t{request.turn}"
        clip = await asyncio.to_thread(self.capture, project=self.project, status=self.status,
                                       since_utc=self.started_utc, until_utc=now, output_dir=folder,
                                       generation=self.generation, window_s=self.clip_window_s)
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
            recent=recent, hypotheses=old_hypotheses,
            new_hypothesis_ids=ids, age_s=context.get("as_of", {}).get("age_s"),
            clip=clip.descriptor, clip_unavailable_reason=clip.reason,
            findings=[{key: f[key] for key in ("finding_id", "title", "repro_check")} for f in self.findings],
            suspicions=suspicions,
            progress=(progress_rows(self.progress_probe.regions, observation)
                      if self.progress_probe.regions else ()),
        )
        # The live S1 context does not expose the bridge frame number. Do not send the
        # placeholder needed by Observation's local constructor as if it were measured.
        packet.payload["observation"].pop("frame", None)
        body = provider.build_request(role_manifest, packet, clip_path=clip.path)
        # Preserve the complete body before any POST. Keep disk I/O off the bridge/S1 event loop.
        request_artifact = await asyncio.to_thread(preserve_request, self.directory, body)
        reservation = self.ledger.reserve(
            episode=0, turn=request.turn, based_on_seq=seq, request_sha256=request_artifact.sha256,
            requested_model=provider.MODEL,
            extra={"role": "frontier", "request_turn": request.turn,
                   "packet_sha256": digest(packet.canonical()), "clip": clip.descriptor,
                   "request_body_path": request_artifact.relative_path,
                   "clip_unavailable_reason": clip.reason},
        )
        self.calls += 1
        try:
            status, data, latency, problem = await asyncio.to_thread(self.send, body)
            result = provider.decode_response(data if status == 200 else None, role_manifest, packet)
            answer = result.answer
            if status != 200 or answer is None:
                details = {"error": problem or ("invalid frontier answer" if answer is None else f"HTTP {status}"),
                           "errors": result.errors, "hypothesis": None, "directive": None}
                reservation.finish(http_status=status, latency_ms=latency, cost_usd=result.cost_usd,
                                         resolved_model=result.resolved_model, usage=result.usage,
                                         answer=None, parse_valid=False, **details)
                self.sink.append("investigation", {
                    "event": "invalid_answer", "request_turn": request.turn, "based_on_seq": seq,
                    "outcome_seq": seq, "hypothesis": None, "directive": None, "keep": None,
                    "outcome": "unknown",
                    "evidence_trace_seq": obs_trace, "reason": details["error"],
                }, author="frontier", turn=None, refs=[obs_trace] if obs_trace is not None else [])
                return HelpResult(source="frontier", result=details)
            review = frontier.review(answer, role_manifest, packet.refs)
            accepted = [item for i, item in enumerate(answer.hypotheses)
                        if not review.rejected(f"hypotheses[{i}]")]
            directive = None
            if answer.directive is not None and not review.rejected("directive"):
                directive = frontier.to_directive(answer.directive, directive_id=f"d-{request.turn}",
                    parent=current.directive_id if current is not None else None,
                    model=result.resolved_model or provider.MODEL,
                    based_on_seq=seq, received_game_s=observation.game_s)
            # A null directive asks for no replacement; it never renews one. Record that request beside the
            # open directive, with the exact time left, so a later measured close can be read against it.
            keep_row = None
            if answer.directive is None and current is not None:
                deadline = current.deadline_game_s
                keep_row = {"directive_id": current.directive_id,
                            "requested": {"turn": request.turn, "seq": seq, "game_s": observation.game_s},
                            "deadline_game_s": deadline,
                            "expires_in_game_s": (deadline - observation.game_s
                                                  if deadline is not None and observation.game_s is not None
                                                  else None)}
            result_payload = {
                "hypotheses": [h.model_dump(mode="json") for h in accepted],
                "directive": directive.model_dump(mode="json") if directive is not None else None,
                "keep": copy.deepcopy(keep_row),
                "errors": list(result.errors),
                "review": [{"item": p.item, "reason": p.reason} for p in review.problems],
            }
            reason = None
            if directive is None:
                rejected = [p.reason for p in review.problems if p.item == "directive"]
                reason = (f"the directive was rejected: {'; '.join(rejected)}" if rejected
                          else "no accepted directive was returned")
            self.sink.append("investigation", {
                "event": "hypothesis_directive", "request_turn": request.turn, "based_on_seq": seq,
                "outcome_seq": seq,
                "hypothesis": result_payload["hypotheses"], "directive": result_payload["directive"],
                "keep": None,
                "outcome": "open" if directive is not None else "unknown", "evidence_trace_seq": obs_trace,
                "reason": reason,
            }, author="frontier", turn=None, refs=[obs_trace] if obs_trace is not None else [])
            directive_row = None
            if directive is not None:
                expected = directive.expected
                kind = role_manifest.fields.get(expected.field) if expected else "other"
                # Only the fallback: the tracker re-takes a changed/unchanged baseline when S1 first sees the directive.
                baseline = self._reading(observation, expected.field) if expected else None
                directive_row = {**directive.model_dump(mode="json"),
                                 "hypothesis": copy.deepcopy(result_payload["hypotheses"]),
                                 "kind": kind, "baseline": baseline, "scene": observation.scene,
                                 "last_frame": observation.frame, "last_game_s": observation.game_s,
                                 "requested_game_s": observation.game_s,
                                 "directive": directive.model_dump(mode="json")}
            candidate = (answer.candidate_finding.model_copy(deep=True)
                         if answer.candidate_finding is not None and not review.rejected("candidate_finding") else None)
            finding_evidence = (_finding_evidence(candidate, recent, evidence_snapshot)
                                if candidate is not None else None)
            reservation.finish(http_status=status, latency_ms=latency, cost_usd=result.cost_usd,
                               resolved_model=result.resolved_model, usage=result.usage,
                               parse_valid=True, answer=answer.model_dump(mode="json"))
            completed = HelpResult(source="frontier", result=copy.deepcopy(result_payload))
            if request.turn not in self._discarded and request.turn not in self._adopted:
                self._staged[request.turn] = _StagedResult(
                    result=completed, payload=copy.deepcopy(result_payload), directive_row=directive_row,
                    keep_row=copy.deepcopy(keep_row),
                    based_on_seq=seq, evidence_trace_seq=obs_trace, candidate=candidate,
                    finding_evidence=finding_evidence)
            return completed
        except asyncio.CancelledError:
            reservation.finish(directive_result="cancelled", cost_usd=None)
            raise
        except Exception:
            reservation.finish(directive_result="failed", cost_usd=None)
            raise

    def adopt_result(self, request_turn: int, result: HelpResult) -> HelpResult:
        """Publish only the exact finished result the loop is adopting, once.

        The loop must pass the original HelpResult returned by run, before appending guidance and
        collecting facts. A replacement, discarded result, or failed call passes through unchanged.
        """
        adopted = self._adopted.get(request_turn)
        if adopted is not None:
            original, published = adopted
            return published if result is original or result is published else result
        staged = self._staged.pop(request_turn, None)
        if (staged is None or result is not staged.result or result.source != "frontier"
                or result.result != staged.payload):
            # resolve_helper may replace a result even before the background request finishes.
            self._discarded.add(request_turn)
            return result
        for row in staged.payload["hypotheses"]:
            latest = copy.deepcopy(row)
            previous = self.hypotheses.get(row["id"])
            if previous is not None:
                # Provider updates contain only new evidence; retain the earlier citations for its next packet.
                for key in ("supporting_seq", "contradicting_seq"):
                    latest[key] = list(dict.fromkeys([*previous[key], *row[key]]))
            self.hypotheses[row["id"]] = latest
        if staged.directive_row is not None:
            self.tracker.adopt(staged.directive_row, request_turn=request_turn,
                               based_on_seq=staged.based_on_seq, evidence_trace_seq=staged.evidence_trace_seq)
        if staged.keep_row is not None:
            self.tracker.note_keep(staged.keep_row, adopted_turn=request_turn)
        payload = copy.deepcopy(staged.payload)
        if staged.candidate is not None and staged.finding_evidence is not None:
            payload["candidate_finding"] = self._record_finding(staged.candidate, request_turn,
                                                                 staged.finding_evidence)
        published = HelpResult(source=result.source, result=self._guidance(payload))
        self._adopted[request_turn] = (result, published)
        return published

    def discard_result(self, request_turn: int) -> None:
        """Forget an unadopted response, including one that finishes after invalidation."""
        self._staged.pop(request_turn, None)
        self._discarded.add(request_turn)

    def render_guidance(self, items: Any) -> dict[str, Any]:
        """The adopted guidance as S1 reads it: current rows, earlier versions and a ledger (`guidance_codec`).

        Layout only. The loop sends it only if `restore_guidance` gives back exactly `items`.
        """
        return guidance_codec.render(items)

    def restore_guidance(self, view: Any) -> list[dict[str, Any]]:
        """The exact inverse of `render_guidance`; raises when `view` is not one it could have produced."""
        return guidance_codec.restore(view)

    def _record_finding(self, proposed: Any, turn: int, evidence: _FindingEvidence) -> dict[str, Any]:
        """Write the frontier's candidate finding to the trace, citing trace entries, and say what happened.

        Evidence was resolved against the request-time snapshot; nothing added later can become a citation.
        """
        finding_id = f"f-{turn}"
        if evidence.reason is not None:
            return {"finding_id": finding_id, "trace_seq": None, "reason": evidence.reason}
        finding = frontier.to_finding(proposed, finding_id=finding_id, evidence_seq=list(evidence.refs))
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
        - A hypothesis is listed in full whenever any field or evidence differs from the last version
          shown. Only an identical version is listed by id in `restated_hypotheses`.
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
            if known is not None and known == row:
                restated.append(key)
                continue
            fresh.append(row)
            if isinstance(key, str):
                self._shown[key] = copy.deepcopy(row)
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
                                 calibration=self.reader_calibration, field_notes=self.reader_notes,
                                 effects=self.effects, progress=self.progress)

    def _reading(self, observation: Observation, field: str) -> Any:
        """One field's value at this observation, including a declared region's derived distance."""
        if field.startswith("progress."):
            return self.progress_probe.derived(observation).get(field)
        return field_value(observation.public, field.removeprefix("public."))
