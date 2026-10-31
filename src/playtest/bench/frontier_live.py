"""The live frontier adapter for one S1 escalation (#33).

S1 asks for help; code runs exactly one Muse call for that turn, records the request and answer,
and hands the answer back through `TurnLoop.resolve_helper` once. Nothing here chooses an action:
the directive is a controlled test S1 may use, added and pruned choices wait for the choice
builder (#29), hypotheses and a candidate finding wait for the investigation store (#35), and an
invalid answer stops the run instead of being repaired or replaced.

Every paid call reserves its own ledger row before the POST, shares the run-wide overall envelope
with S1, and keeps its reservation when the call or the decode fails. There is no retry and no
fallback to another model.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from playtest.bench import call_budget
from playtest.bench.record import OBJECTIVE, digest
from playtest.contracts.bridge import Observation
from playtest.contracts.investigation import Directive, DirectiveOutcome
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSet, TurnOutcome, TurnRecord
from playtest.core.context import canonical
from playtest.frontier import media, provider
from playtest.frontier.packet import build_packet
from playtest.roles import frontier

FRONTIER_ARM = provider.MODEL
EFFORT = "minimal"
MANIFEST_FIELD = "scene"
MANIFEST_PROVENANCE = "calibrated-choices stand-in, not a production preflight manifest"
MANIFEST_SOURCE = "calibrated offered choices"

# Under the plain checklist S1 chose `escalate` in none of 30 turns (docs/probes/2026-10-27-fpskit-s1-live.md),
# so a live frontier call could not be reached. This adds one sentence to the objective so asking is part of
# the task. It is context only: S1 still decides whether and when, and no code reads a fact to escalate.
HELP_OBJECTIVE = (OBJECTIVE + " If a completed check measures no net displacement, ask the frontier model "
                  "for help once, then continue the checklist.")

MediaSource = Callable[[Observation, dt.datetime, Path], media.Clip]


class FrontierError(RuntimeError):
    """The frontier cannot provide an adoptable answer; the run stops with no retry."""


def manifest_for(offered: Sequence[Choice]) -> frontier.Manifest:
    """A conservative run manifest from the choices this run actually offers.

    Only the bridge's generic `scene` observation field is declared; no public field is guessed.
    Actions take the default `ActionSpec()`, so a hold or axis override is invalid. A duplicate
    action name is one action, and skills appear only when an offered choice is a skill.
    """
    actions: set[str] = set()
    skills: set[str] = set()
    for choice in offered:
        if choice.kind is ChoiceKind.action and choice.action:
            actions.add(choice.action)
        if choice.kind is ChoiceKind.skill and choice.skill:
            skills.add(choice.skill)
    return frontier.Manifest(
        fields={MANIFEST_FIELD: "string"},
        actions={name: frontier.ActionSpec() for name in sorted(actions)},
        skills=sorted(skills),
    )


def dynamic_enum_problems(answer: frontier.FrontierAnswer, manifest: frontier.Manifest) -> list[str]:
    """Values the rendered schema restricts to enums but Pydantic keeps as plain strings."""
    fields = set(manifest.fields)
    names = set(manifest.actions) | set(manifest.skills)
    problems: list[str] = []
    directive = answer.directive
    if directive is not None and directive.expected is not None and directive.expected.field not in fields:
        problems.append(f"directive.expected.field {directive.expected.field!r} is not a manifest field")
    for index, item in enumerate(answer.add_choices):
        if item.name not in names:
            problems.append(f"add_choices[{index}].name {item.name!r} is not a manifest action or skill")
    for index, item in enumerate(answer.prune_choices):
        if item.name not in names:
            problems.append(f"prune_choices[{index}].name {item.name!r} is not a manifest action or skill")
    finding = answer.candidate_finding
    if finding is not None and finding.repro_check is not None and finding.repro_check.field not in fields:
        problems.append(f"candidate_finding.repro_check.field {finding.repro_check.field!r} is not a manifest field")
    return problems


def schema_valid(answer: frontier.FrontierAnswer | None, manifest: frontier.Manifest) -> bool:
    """Structural decode plus the dynamic enums of the exact schema sent for this run.

    This is deliberately not `review.clean`: a citation or setpoint-type problem rejects one item
    semantically, while a schema-invalid value means the provider did not meet the declared shape.
    """
    return answer is not None and not dynamic_enum_problems(answer, manifest)


def acceptance(rows: Sequence[Mapping[str, Any]], *, run_status: str) -> dict[str, Any]:
    """Whether this run's live frontier acceptance passed, failed or has no evidence."""
    calls = [row for row in rows if row.get("role") == "frontier"]
    passed = [row for row in calls if _call_passes(row)]
    if not calls:
        return {"acceptance": "inconclusive", "reason": "no S1 escalation reached the frontier"}
    if passed and run_status == "complete":
        return {"acceptance": "passed", "reason": None}
    if passed:
        return {"acceptance": "inconclusive",
                "reason": f"a valid call exists but the run ended as {run_status!r}, not 'complete'"}
    return {"acceptance": "failed",
            "reason": "no call had HTTP 200, a parsed answer, exact schema validity and a nonempty attached clip"}


def _call_passes(row: Mapping[str, Any]) -> bool:
    media_row = row.get("media") if isinstance(row.get("media"), Mapping) else {}
    clip = media_row.get("clip")
    size = clip.get("bytes") if isinstance(clip, Mapping) else None
    return (row.get("http_status") == 200
            and row.get("parse_valid") is True
            and row.get("schema_valid") is True
            and isinstance(clip, Mapping)
            and media_row.get("attached") is True
            and isinstance(size, int) and not isinstance(size, bool) and size > 0)


class LiveFrontier:
    """One Muse call per S1 escalation, sharing the run's call ledger with S1."""

    def __init__(self, directory: Path, approval: Mapping[str, Any], *,
                 media_source: MediaSource, send: Callable[..., Any] = provider.send_meta,
                 manifest: frontier.Manifest | None = None) -> None:
        self.directory = directory
        self.ledger = call_budget.Ledger(directory, approval, FRONTIER_ARM)
        self.media_root = directory / "clips"
        self.media_source = media_source
        self.send = send
        self.manifest: frontier.Manifest | None = None
        self.hashes: dict[str, str | None] = {"schema_sha256": None, "system_prompt_sha256": None,
                                             "manifest_message_sha256": None, "manifest_sha256": None}
        self.directive: Directive | None = None
        self.directive_history: list[Directive] = []
        self.episode = 0
        self.loop: Any = None
        self.observe: Callable[[], Any] | None = None
        self.last: dict[str, Any] = {}
        if manifest is not None:
            self.use_manifest(manifest)

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.ledger.arm_rows()

    def use_manifest(self, manifest: frontier.Manifest) -> dict[str, Any]:
        """Fix this run's manifest and return the provenance a plan or trace records."""
        self.manifest = manifest
        schema = frontier.schema(manifest)
        self.hashes = {
            "schema_sha256": digest(canonical(schema)),
            "system_prompt_sha256": digest(frontier.SYSTEM),
            "manifest_message_sha256": digest(provider.manifest_message(manifest)),
            "manifest_sha256": digest(canonical({
                "fields": [{"path": path, "kind": manifest.fields[path]} for path in sorted(manifest.fields)],
                "actions": sorted(manifest.actions),
                "skills": sorted(manifest.skills),
            })),
        }
        return {
            "enabled": True,
            "model": provider.MODEL,
            "reasoning_effort": EFFORT,
            **self.hashes,
            "manifest_source": MANIFEST_SOURCE,
            "manifest_provenance": MANIFEST_PROVENANCE,
        }

    def bind(self, *, episode: int, loop: Any, observe: Callable[[], Any]) -> None:
        self.episode, self.loop, self.observe = episode, loop, observe
        self.directive, self.directive_history, self.last = None, [], {}

    def directives(self) -> tuple[Directive | None, list[Directive]]:
        """The directive S1's next payload carries and the superseded history."""
        return self.directive, list(self.directive_history)

    async def on_escalation(self, *, record: TurnRecord, binding: Mapping[str, Any],
                            evidence: Sequence[Mapping[str, Any]],
                            recent: Sequence[Mapping[str, Any]],
                            episode_start_wall_utc: dt.datetime) -> dict[str, Any]:
        """Run the one call for this escalation; raise `FrontierError` when it cannot continue."""
        if self.manifest is None:
            raise FrontierError("the frontier manifest is unavailable; no call was made")
        pending = self.loop.pending_helper if self.loop is not None else None
        if record.outcome is not TurnOutcome.escalated or pending is None or pending.get("kind") != "escalate" \
                or pending.get("turn") != record.turn:
            raise FrontierError("the pending escalation does not match this turn; no call was made")
        observation, choices = binding.get("observation"), binding.get("choices")
        if not isinstance(observation, Observation) or not isinstance(choices, ChoiceSet):
            raise FrontierError("the exact S1 binding for this escalation is missing; no call was made")
        objective = binding.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise FrontierError("the objective S1 was given for this escalation is missing; no call was made")

        folder = self.media_root / f"e{self.episode}-t{record.turn}"
        try:
            clip = await asyncio.to_thread(self.media_source, observation, episode_start_wall_utc, folder)
        except Exception as exc:  # A capture helper that raises is an integration failure, not a clip.
            raise FrontierError(f"clip capture raised {type(exc).__name__}; no call was made") from None
        if not isinstance(clip, media.Clip):
            raise FrontierError("clip capture returned no result; no call was made")

        try:
            packet = build_packet(
                objective=objective,
                observation=observation,
                choices=choices,
                escalation=record,
                facts=evidence,
                recent=recent,
                directive=self.directive,
                directive_history=self.directive_history,
                age_s=binding.get("age_s"),
                clip=clip.descriptor,
                clip_unavailable_reason=clip.reason,
            )
            request = provider.build_request(self.manifest, packet, clip_path=clip.path)
        except ValueError as exc:
            raise FrontierError(f"the frontier request could not be built: {exc}") from None
        request_sha = digest(canonical(request))
        media_row = self._media_row(folder, clip)
        extra = {
            "role": "frontier",
            "requested_model": provider.MODEL,
            "packet_sha256": digest(packet.canonical()),
            "media": media_row,
            "s1_facts": list(binding.get("facts") or ()),
            "s1_age_s": binding.get("age_s"),
            **self.hashes,
        }
        try:
            reservation = self.ledger.reserve(
                episode=self.episode,
                turn=record.turn,
                based_on_seq=observation.seq,
                request_sha256=request_sha,
                requested_model=provider.MODEL,
                extra=extra,
            )
        except call_budget.BudgetStop as exc:
            raise FrontierError(str(exc)) from None

        base = {
            **extra,
            "request": provider.redact_media(request),
            "packet": packet.payload,
        }
        try:
            status, data, latency_ms, problem = await asyncio.to_thread(self.send, request)
        except Exception as exc:
            reservation.finish(http_status=None, resolved_model=None, usage=None, cost_usd=None,
                               latency_ms=None, problem=f"adapter error: {type(exc).__name__}",
                               answer=None, review=None, errors=[], parse_valid=False, schema_valid=False,
                               schema_problems=[], raw_answer=None, directive_result="not_attempted",
                               directive=None, directive_receipt=None, received_unapplied=[], **base)
            raise FrontierError("the frontier call failed; stopped with no retry") from None

        result = provider.decode_response(data if status == 200 else None, self.manifest, packet)
        answer = result.answer
        review = result.review
        review_row = None if review is None else {
            "clean": review.clean,
            "problems": [{"item": item.item, "reason": item.reason} for item in review.problems],
        }
        enum_problems = dynamic_enum_problems(answer, self.manifest) if answer is not None else []
        fields = {
            **base,
            "http_status": status,
            "latency_ms": latency_ms,
            "problem": problem,
            "resolved_model": result.resolved_model,
            "usage": result.usage,
            "cost_usd": result.cost_usd,
            "answer": answer.model_dump(mode="json") if answer is not None else None,
            "review": review_row,
            "errors": list(result.errors),
            "parse_valid": answer is not None,
            "schema_valid": answer is not None and not enum_problems,
            "schema_problems": enum_problems,
            "raw_answer": _raw_answer(data),
            "received_unapplied": _received_unapplied(answer),
        }
        if status != 200:
            reservation.finish(directive_result="not_attempted", directive=None, directive_receipt=None, **fields)
            raise FrontierError(
                f"{FRONTIER_ARM} returned {status if status is not None else 'a transport failure'} "
                f"({problem or 'no problem text'}); stopped with no retry")
        if answer is None:
            reservation.finish(directive_result="not_attempted", directive=None, directive_receipt=None, **fields)
            raise FrontierError("the frontier answer was rejected ("
                                + "; ".join(result.errors or ["undecodable answer"])
                                + "); stopped with no retry")
        if _cost_exceeded(result.cost_usd, reservation.bound):
            reservation.finish(directive_result="not_applied", directive=None, directive_receipt=None, **fields)
            raise FrontierError("a provider-reported cost exceeded the per-call bound")

        directive_row, directive_result, receipt_row = await self._apply_directive(
            answer, review or frontier.Review(), record=record, observation=observation,
            reservation=reservation, fields=fields)
        self.loop.resolve_helper({
            "call": reservation.row["slot"],
            "answer": fields["answer"],
            "review": review_row,
            "directive": directive_row,
            "directive_result": directive_result,
        })
        row = reservation.finish(directive_result=directive_result, directive=directive_row,
                                 directive_receipt=receipt_row, **fields)
        self.last = row
        return row

    async def _apply_directive(self, answer: frontier.FrontierAnswer, review: frontier.Review, *,
                               record: TurnRecord, observation: Observation,
                               reservation: call_budget.Reservation,
                               fields: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """Adopt the directive only when it was not rejected; a null answer requests no replacement."""
        if answer.directive is None:
            return None, "kept_current", None
        if review.rejected("directive"):
            return None, "rejected", None
        if self.observe is None:
            reservation.finish(**fields, directive_result="receipt_unavailable", directive=None,
                               directive_receipt=None)
            self.loop.resolve_helper({"call": reservation.row["slot"], "answer": fields.get("answer"),
                                      "review": fields.get("review"), "directive": None,
                                      "directive_result": "receipt_unavailable"})
            raise FrontierError("the receipt observation source is unavailable; run stopped")
        try:
            receipt = await self.observe()
        except Exception as exc:
            reservation.finish(**fields, directive_result="receipt_failed", directive=None,
                               directive_receipt=None)
            self.loop.resolve_helper({"call": reservation.row["slot"],
                                      "answer": fields.get("answer"),
                                      "review": fields.get("review"),
                                      "directive": None,
                                      "directive_result": "receipt_failed"})
            raise FrontierError(f"the directive receipt observation failed ({type(exc).__name__}); run stopped") from None
        self.loop.store.add(receipt)
        receipt_row = {"seq": receipt.seq, "game_s": receipt.game_s, "wall_utc": _wall(receipt.wall_utc)}
        try:
            directive = frontier.to_directive(
                answer.directive,
                directive_id=f"f{self.episode}-{record.turn}",
                parent=self.directive.directive_id if self.directive is not None else None,
                model=str(fields.get("resolved_model") or provider.MODEL),
                based_on_seq=observation.seq,
                received_game_s=receipt.game_s,
            )
        except ValueError:
            return None, "rejected", receipt_row
        if directive.expected is None:
            directive = directive.model_copy(update={"outcome": DirectiveOutcome.unknown})
        if self.directive is not None:
            self.directive_history.append(
                self.directive.model_copy(update={"outcome": DirectiveOutcome.unknown}))
        self.directive = directive
        return directive.model_dump(mode="json"), "adopted", receipt_row

    def _media_row(self, folder: Path, clip: media.Clip) -> dict[str, Any]:
        try:
            relative = folder.relative_to(self.directory).as_posix()
        except ValueError:
            relative = None
        return {
            "folder": relative,
            "clip": clip.descriptor,
            "unavailable_reason": clip.reason,
            "attached": clip.descriptor is not None,
            "manifest": f"{relative}/manifest.json" if relative is not None and clip.manifest is not None else None,
        }


def _received_unapplied(answer: frontier.FrontierAnswer | None) -> list[dict[str, Any]]:
    """Answers this bounded smoke receives but deliberately does not instantiate."""
    if answer is None:
        return []
    rows: list[dict[str, Any]] = []
    if answer.add_choices:
        rows.append({"item": "add_choices", "count": len(answer.add_choices),
                     "reason": "the choice builder is not implemented; the offered choices are unchanged"})
    if answer.prune_choices:
        rows.append({"item": "prune_choices", "count": len(answer.prune_choices),
                     "reason": "the choice builder is not implemented; the offered choices are unchanged"})
    if answer.hypotheses:
        rows.append({"item": "hypotheses", "count": len(answer.hypotheses),
                     "reason": "no investigation store exists yet; kept in this call record only"})
    if answer.candidate_finding is not None:
        rows.append({"item": "candidate_finding", "count": 1,
                     "reason": "no investigation store exists yet; kept in this call record only"})
    return rows


def _raw_answer(data: Mapping[str, Any] | None) -> str | None:
    try:
        content = data["choices"][0]["message"]["content"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) else None


def _cost_exceeded(value: Any, bound: Decimal) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    return Decimal(str(value)) > bound


def _wall(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


__all__ = ["FRONTIER_ARM", "EFFORT", "FrontierError", "LiveFrontier", "acceptance", "manifest_for",
           "dynamic_enum_problems", "schema_valid"]
