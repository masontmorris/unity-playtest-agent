"""The manifest's readers, installed in a live session: each one is read from the Editor's field inspection.

The bridge's own observation carries only the generic baseline (the camera). A manifest reader is a named
game field that preflight (stage 4) chose and recorded with its descriptor. `FieldReaders` reads them all
with one `inspect("fields")` before each turn's observation (`TurnLoop` installs the sample in that
observation's `public`), so the reading never ages the observation an action is bound to. It costs one
inspection a turn; a bridge-side producer would be atomic with the observation, and this is the stopgap until
there is one.

Each reader is matched by its field id, and the live row must still carry the manifest's descriptor (the same
scene, object, component, declaring type and field type). A reader is unknown, never zero, when its field is
not in the live scene, its descriptor differs, its value is not `present`, or its kind is not the one the
manifest declares; the sample says why, per reader. A lost bridge is not a failed read: `BridgeError` ends the
run.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from playtest.contracts.bridge import InspectResult
from playtest.contracts.manifest import ManifestReader
from playtest.core.bridge_client import BridgeClient
from playtest.core.ports import ReaderSample
from playtest.core.readers import unread
from playtest.preflight.packet import field_kind, field_offered

IDENTITY_KEYS = ("id", "component_id", "object_id", "scene_id", "name", "declaring_type", "declaration_id",
                 "field_type", "field_type_assembly")


@dataclass(frozen=True)
class Installed:
    """What the first reading found: which readers resolved in the live scene and which did not."""

    resolved: list[str]
    unresolved: list[dict[str, str]]
    inspection_seq: int | None
    game_s: float | None

    def summary(self) -> dict[str, Any]:
        return {"resolved": list(self.resolved), "unresolved": list(self.unresolved),
                "inspection_seq": self.inspection_seq, "game_s": self.game_s}


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

    async def install(self) -> Installed:
        """Read once, before any paid call, and say which readers resolve in the live scene.

        An unresolved reader stays installed (its value is unknown every turn, with the reason). If none of
        the declared readers resolves, the scene is not the one the manifest was made for: `ValueError`.
        """
        sample = await self._take()
        unresolved = [{"alias": alias, "reason": reason} for alias, reason in sample.errors.items()]
        resolved = [alias for alias in self.aliases if alias not in sample.errors]
        self.installed = Installed(resolved, unresolved, sample.inspection_seq, sample.game_s)
        if self._readers and not resolved:
            reasons = "; ".join(sorted({item["reason"] for item in unresolved}))
            raise ValueError(f"none of the manifest's {len(self._readers)} readers can be read in the live scene: {reasons}")
        return self.installed

    async def read(self) -> ReaderSample:
        return await self._take()

    async def _take(self) -> ReaderSample:
        response = await self._client.inspect("fields")
        sample = self._sample(response)
        self.stats.reads += 1
        self.stats.failed_samples += 1 if sample.problem is not None else 0
        for alias in sample.errors:
            self.stats.unread[alias] = self.stats.unread.get(alias, 0) + 1
        return sample

    def _sample(self, response: InspectResult) -> ReaderSample:
        rows = response.data.get("fields")
        if response.stage != "fields" or not isinstance(rows, list):
            return unread(self.aliases, "the field inspection did not return a field list")
        cap = response.caps.get("fields")
        complete = cap is not None and cap.truncated is False
        by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
        values: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for reader in self._readers:
            value, reason = _value(reader, by_id.get(reader.field_id), complete)
            values[reader.alias] = value
            if reason is not None:
                errors[reader.alias] = reason
        game_s = response.data.get("game_time_s")
        game_s = float(game_s) if isinstance(game_s, (int, float)) and not isinstance(game_s, bool) \
            and math.isfinite(game_s) else None
        return ReaderSample(values=values, errors=errors, inspection_seq=response.seq, game_s=game_s)


def _value(reader: ManifestReader, row: dict[str, Any] | None, complete: bool) -> tuple[Any, str | None]:
    """The live value of one reader, or `None` and why there is none."""
    if row is None:
        return None, ("the field is not in the live scene" if complete
                      else "the field is not in the live inspection, which was truncated")
    differs = [key for key in IDENTITY_KEYS if key in reader.descriptor and row.get(key) != reader.descriptor[key]]
    if differs:
        return None, f"the live field differs from the manifest's descriptor ({', '.join(differs)})"
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
