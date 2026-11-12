"""The manifest's readers, installed in a live session: each one is read from the Editor's field inspection.

The bridge's own observation carries only the generic baseline (the camera). A manifest reader is a named
game field that preflight (stage 4) chose and recorded with its descriptor. `FieldReaders` verifies every
reader once, before any paid call, from one `inspect("discovery")`: that is the stage whose field rows still
carry the full static descriptor. It then reads values with one cheap, values-only `inspect("fields")` before
each turn's observation (`TurnLoop` installs the sample in that observation's `public`), so the reading never
ages the observation an action is bound to. It costs one inspection a turn; a bridge-side producer would be
atomic with the observation, and this is the stopgap until there is one.

Identity is static and values change, so the two checks are separate. Install compares every identity key the
manifest recorded (`IDENTITY_KEYS`) against the live full descriptor, and a key the live row omits is a
difference there: that is where "the scene is not the one the manifest was made for" is decided, and a reader
that fails keeps its install reason forever instead of being read later. A per-turn values-only row carries
only `id`, `object_id` and `scene_id`, so a turn compares just the identity keys the row and the descriptor
both carry: a key absent from the row is unmeasured, never a difference and never a match.

A reader is unknown, never zero, when its field is not in the live scene, its identity differs, its value is
not `present`, or its kind is not the one the manifest declares; the sample says why, per reader. A lost
bridge is not a failed read: `BridgeError` ends the run.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from playtest.contracts.bridge import InspectResult, Observation
from playtest.contracts.manifest import ManifestReader
from playtest.core.bridge_client import BridgeClient
from playtest.core.ports import ReaderSample
from playtest.core.readers import unread
from playtest.preflight.packet import field_kind, field_offered
from playtest.contracts.scene_ids import scene_free_index, without_scene_index

# Every static key a full field descriptor carries. Install checks all of them; a per-turn row carries the
# first three of these (`id`, `object_id`, `scene_id`) and nothing else.
IDENTITY_KEYS = ("id", "component_id", "object_id", "scene_id", "name", "declaring_type", "declaration_id",
                 "field_type", "field_type_assembly")

# The stage whose field rows are the full descriptor (identity), and the values-only stage read every turn.
IDENTITY_STAGE = "discovery"
SAMPLE_STAGE = "fields"


@dataclass(frozen=True)
class Installed:
    """What the identity inspection found: which readers resolved in the live scene and which did not.

    `inspection_seq` is the seq of that one inspection, taken with `identity_stage`; it is the inspection the
    identity of every resolved reader was verified in. The readers are then sampled with the cheaper
    values-only stage, once a turn.
    """

    resolved: list[str]
    unresolved: list[dict[str, str]]
    inspection_seq: int | None
    game_s: float | None
    identity_stage: str = IDENTITY_STAGE

    def summary(self) -> dict[str, Any]:
        return {"resolved": list(self.resolved), "unresolved": list(self.unresolved),
                "inspection_seq": self.inspection_seq, "game_s": self.game_s,
                "identity_stage": self.identity_stage}


@dataclass(frozen=True)
class PublishedInstall:
    resolved: list[str]
    unresolved: list[dict[str, str]]
    observation_seq: int
    game_s: float | None

    def summary(self) -> dict[str, Any]:
        return {"source": "observe.public", "resolved": self.resolved, "unresolved": self.unresolved,
                "observation_seq": self.observation_seq, "game_s": self.game_s,
                "identity_verified": False, "freshness": None,
                "reason": "publication was checked once; field identity and freshness were not measured"}


def published_install(observation: Observation, readers: Sequence[ManifestReader]) -> PublishedInstall:
    """Check the current published aliases without caching or replacing their values."""
    errors = {error.reader: error.error for error in observation.reader_errors}
    resolved = []
    unresolved = []
    for reader in readers:
        alias = reader.alias
        if alias in errors:
            unresolved.append({"alias": alias, "reason": errors[alias]})
        elif alias not in observation.public:
            raise ValueError(f"manifest reader {alias!r} is absent from the bridge observation")
        else:
            resolved.append(alias)
    if readers and not resolved:
        raise ValueError("none of the manifest's readers can be read in the live observation")
    return PublishedInstall(resolved, unresolved, observation.seq, observation.game_s)


@dataclass
class ReadStats:
    reads: int = 0  # samples taken
    failed_samples: int = 0  # samples in which nothing could be read
    unread: dict[str, int] = field(default_factory=dict)  # alias -> samples in which it had no value

    def summary(self) -> dict[str, Any]:
        return {"reads": self.reads, "failed_samples": self.failed_samples, "unread_by_reader": dict(self.unread)}


class FieldReaders:
    def __init__(self, client: BridgeClient, readers: Sequence[ManifestReader]) -> None:
        self._client = client
        self._readers = list(readers)
        self.aliases = [reader.alias for reader in self._readers]
        self.stats = ReadStats()
        self.installed: Installed | None = None
        # alias -> why its identity failed at install. A reader here is never read later: its field is not the
        # one the manifest was made for, and a value from a different field would be a measurement of nothing.
        self._unverified: dict[str, str] = {}

    async def install(self) -> Installed:
        """Verify every reader's full identity once, before any paid call, from the discovery inspection.

        A reader that does not verify stays installed (its value is unknown every turn, with the install's
        reason) and is never read later. If none of the declared readers resolves, the scene is not the one the
        manifest was made for: `ValueError`.
        """
        self._unverified = {}
        sample, unverified = await self._take(IDENTITY_STAGE)
        self._unverified = unverified
        unresolved = [{"alias": alias, "reason": reason} for alias, reason in sample.errors.items()]
        resolved = [alias for alias in self.aliases if alias not in sample.errors]
        self.installed = Installed(resolved, unresolved, sample.inspection_seq, sample.game_s, IDENTITY_STAGE)
        if self._readers and not resolved:
            reasons = "; ".join(sorted({item["reason"] for item in unresolved}))
            raise ValueError(f"none of the manifest's {len(self._readers)} readers can be read in the live scene: {reasons}")
        return self.installed

    async def read(self) -> ReaderSample:
        sample, _ = await self._take(SAMPLE_STAGE)
        return sample

    async def rebind(self, *, client: BridgeClient | None = None, observation: Any = None,
                     generation: str | None = None) -> None:
        """Re-verify the manifest's readers in the restarted scene; a scene none of them resolve in fails closed."""
        if client is None:
            raise ValueError("rebinding the field readers needs the new bridge client")
        self._client = client
        self.installed = None
        await self.install()

    async def _take(self, stage: str) -> tuple[ReaderSample, dict[str, str]]:
        response = await self._client.inspect(stage)
        sample, unverified = self._sample(response, stage)
        self.stats.reads += 1
        self.stats.failed_samples += 1 if sample.problem is not None else 0
        for alias in sample.errors:
            self.stats.unread[alias] = self.stats.unread.get(alias, 0) + 1
        return sample, unverified

    def _sample(self, response: InspectResult, stage: str) -> tuple[ReaderSample, dict[str, str]]:
        """One reading of every reader from `stage`'s rows, and the aliases whose identity failed here."""
        identity = stage == IDENTITY_STAGE
        listed = _field_rows(response, stage)
        if listed is None:
            reason = "the field inspection did not return a field list"
            return unread(self.aliases, reason), (dict.fromkeys(self.aliases, reason) if identity else {})
        rows, complete = listed
        by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
        scene_free = scene_free_index(row for row in rows if isinstance(row, dict))
        values: dict[str, Any] = {}
        errors: dict[str, str] = {}
        unverified: dict[str, str] = {}
        for reader in self._readers:
            alias = reader.alias
            if not identity and alias in self._unverified:  # failed identity at install: never read later
                values[alias], errors[alias] = None, self._unverified[alias]
                continue
            row = by_id.get(reader.field_id)
            if row is None:  # the same field under another loaded-scene index, only when unique
                row = scene_free.get(without_scene_index(reader.field_id))
            if identity:
                value, reason, differential = _installed_value(reader, row, complete)
                if differential:
                    unverified[alias] = reason
            else:
                value, reason = _sampled_value(reader, row, complete)
            values[alias] = value
            if reason is not None:
                errors[alias] = reason
        game_s = response.data.get("game_time_s")
        game_s = float(game_s) if isinstance(game_s, (int, float)) and not isinstance(game_s, bool) \
            and math.isfinite(game_s) else None
        return ReaderSample(values=values, errors=errors, inspection_seq=response.seq, game_s=game_s), unverified


def _field_rows(response: InspectResult, stage: str) -> tuple[list[Any], bool] | None:
    """The field rows one field-list inspection returned, and whether that list was complete."""
    if response.stage != stage:
        return None
    rows = response.data.get("fields")
    if not isinstance(rows, list):
        return None
    cap = response.caps.get("fields")
    return rows, cap is not None and cap.truncated is False


def _missing(row: dict[str, Any] | None, complete: bool) -> str | None:
    """Why this reader has no row in the sample, or `None` when it has one."""
    if row is not None:
        return None
    return ("the field is not in the live scene" if complete
            else "the field is not in the live inspection, which was truncated")


def _installed_value(reader: ManifestReader, row: dict[str, Any] | None,
                     complete: bool) -> tuple[Any, str | None, bool]:
    """One reader's value from a full-descriptor row, and whether the identity check is what failed.

    This is the one strict check: every identity key the manifest recorded must equal the live full descriptor,
    and a key the live row does not carry differs. It decides whether the reader may be read at all.
    """
    reason = _missing(row, complete)
    if reason is not None:
        return None, reason, True
    differs = [key for key in IDENTITY_KEYS if key in reader.descriptor
               and without_scene_index(row.get(key)) != without_scene_index(reader.descriptor[key])]
    if differs:
        return None, f"the live field differs from the manifest's descriptor ({', '.join(differs)})", True
    value, reason = _live_value(reader, row)
    return value, reason, False


def _sampled_value(reader: ManifestReader, row: dict[str, Any] | None, complete: bool) -> tuple[Any, str | None]:
    """One reader's value from a per-turn values-only row.

    Only the identity keys the row and the descriptor both carry are compared, so a key the row omits (the
    static ones install verified) is unmeasured for this turn: it is neither a difference nor a match. The id
    encodes the object, component, declaring type and field name, so another field cannot be read here.
    """
    reason = _missing(row, complete)
    if reason is not None:
        return None, reason
    differs = [key for key in IDENTITY_KEYS if key in row and key in reader.descriptor
               and without_scene_index(row.get(key)) != without_scene_index(reader.descriptor[key])]
    if differs:
        return None, f"the live field differs from the manifest's descriptor ({', '.join(differs)})"
    return _live_value(reader, row)


def _live_value(reader: ManifestReader, row: dict[str, Any]) -> tuple[Any, str | None]:
    """The live value of one reader from its row, or `None` and why there is none."""
    state = row.get("value_state")
    if state != "present":
        detail = row.get("read_error")
        return None, f"the live value is {state}" + (f" ({detail})" if detail else "")
    if not field_offered(row):
        return None, f"the live value is {row.get('value_kind')}"
    live = field_kind(row)
    if reader.kind is not None and live != reader.kind:
        return None, f"the live value is a {live} value and the manifest declares a {reader.kind} reader"
    if row.get("value_truncated") is True:
        return None, "the live value was truncated"
    return row.get("value"), None
