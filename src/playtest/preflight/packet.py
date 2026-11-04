"""Adapt Editor discovery and measured calibration for model preflight stages.

Control ids in this packet identify physical device controls (keyboard keys, mouse delta/buttons
and gamepad sticks, triggers, dpad and buttons), not Unity's logical InputAction axes. Each
offered control carries the one declared value calibration measured. Bindings outside the
supported root controls remain visible as coverage gaps; they are not turned into actions by
code.

Every readable field is offered unless the provider's enum limits force a choice. Those limits
are the ones the schema renderer enforces (`strict.MUSE_LIMITS`): a field id is one enum value,
so a scene with a heavy UI or framework footprint can exceed them. When it does, the offer is
filled in tiers, strongest evidence first:

1. fields declared by the project's own assemblies;
1b. the text of what the project's own fields reference: a string field of a component, or of any
    component on a game object, that such a field holds a live reference to in the scene. Calibration
    cannot trigger a rare event, so the text a script writes about one is otherwise never measured;
2. fields calibration measured changing;
2b. the authored numbers and flags of that same referenced set, up to a percentage of the field enum;
3. the rest.

Every field left out is listed in `unoffered_fields` with its reason and tier. A packet that fits the
limits is unchanged but for one thing: an offered field that more than one live component carries names
how many do (`instances`), so a reader bound to it is known to read one of several.

The offer bounds what the model may choose, not what it is shown. A project whose discovery is far
larger than the enum limits can still build a packet no single call can read, so the same rule is
applied to the context: once the offer budget applies, or the packet would exceed the context this
model call can hold, full descriptor rows are sent for the offered fields only. Every other field
is summarized in the packet by reason, tier and declaring assembly, and its full record is returned
as a local sidecar beside the manifest. A packet that would still not fit raises before any request.
"""

from __future__ import annotations

import json
import hashlib
import copy
import math
from dataclasses import dataclass
from typing import Any

from playtest.contracts.bridge import InspectResult, InputExecutionEvidence
from playtest.contracts.calibration import ActionCalibration, CalibrationRecord
from playtest.preflight.calibrate import discover_actions
from playtest.preflight.freshness import CHANGED_CLASSIFICATIONS
from playtest.roles import preflight, strict


@dataclass(frozen=True)
class Packet:
    text: str
    discovery: preflight.Discovery
    field_bindings: dict[str, str]
    measured_taps: frozenset[str]
    image: "Screenshot | None"
    execution_evidence: tuple[InputExecutionEvidence, ...] = ()
    # Every field row the text does not carry in full, as JSON for the caller to write beside the
    # manifest. None when the packet carries all of them and nothing has to be written.
    sidecar: str | None = None

    @property
    def action_prompt(self) -> str:
        return (preflight.STAGE_PROMPTS["actions"] + "\n"
                "Calibration lists one measured declared value and the tested requested holds for each physical "
                "control; whether repeated trials showed an entity-displacement effect; how far the game view "
                "turned per apply (mean_view_turn, in degrees, with how far its trials disagree; null where it was "
                "not measured); and never implies that a different value, a combined input or a different hold was "
                "measured. Mouse delta is one relative impulse per apply: x and y are normalized displacement and "
                "the runtime applies x * 64, y * 64 pixels. A keyboard or device button takes null x and y; a "
                "vector control takes both values in "
                "[-1, 1]; a one-dimensional axis uses x with y = 0. Triggers use x in [0, 1]; dpad components "
                "must each be -1, 0 or 1. Gamepad values are sent unchanged. Absolute "
                "pointer, touch, VR and remapping are unsupported. You may choose chords and bounded skills; "
                "execution support is established later only by release-verified evidence for the exact declared "
                "inputs, values, holds and step order, not by your declaration. Describe game limitations separately "
                "from bridge or instrumentation limitations.")

    def review_bridge(self, answer: preflight.StageAnswer, stage: preflight.Stage) -> list[str]:
        """Bridge limitations are recorded in the manifest; they do not reject model selections."""
        return []


@dataclass(frozen=True)
class Screenshot:
    """Screenshot bytes supplied by the caller, with their stated capture provenance."""

    data: bytes
    mime: str
    provenance: str
    same_session: bool | None = None
    capture_game_s: float | None = None

    def descriptor(self) -> dict[str, Any]:
        if self.mime not in ("image/png", "image/jpeg") or not isinstance(self.data, bytes) or not self.data:
            raise ValueError("a screenshot needs PNG or JPEG bytes")
        signature = b"\x89PNG\r\n\x1a\n" if self.mime == "image/png" else b"\xff\xd8\xff"
        if not self.data.startswith(signature):
            raise ValueError("screenshot bytes do not match the declared MIME type")
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise ValueError("a screenshot needs capture provenance")
        if self.same_session is not None and not isinstance(self.same_session, bool):
            raise ValueError("screenshot same_session must be true, false or null")
        if self.capture_game_s is not None:
            import math
            if isinstance(self.capture_game_s, bool) or not isinstance(self.capture_game_s, (int, float)) or not math.isfinite(self.capture_game_s) or self.capture_game_s < 0:
                raise ValueError("screenshot capture game time must be nonnegative finite seconds or null")
        return {"mime": self.mime, "bytes": len(self.data),
                "sha256": hashlib.sha256(self.data).hexdigest(), "provenance": self.provenance,
                "same_session": self.same_session, "capture_game_s": self.capture_game_s}


def _complete_rows(snapshot: InspectResult, name: str) -> list[dict[str, Any]]:
    cap = snapshot.caps.get(name)
    rows = snapshot.data.get(name)
    if cap is None or cap.observed is None or cap.truncated is not False:
        raise ValueError(f"discovery has incomplete {name} coverage")
    if not isinstance(rows, list) or len(rows) != cap.returned or any(not isinstance(x, dict) for x in rows):
        raise ValueError(f"discovery {name} rows do not match their coverage")
    return rows


def _index(rows: list[dict[str, Any]], key: str, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        ident = row.get(key)
        if not isinstance(ident, str) or not ident or ident in result:
            raise ValueError(f"{label} have missing or duplicate {key} values")
        result[ident] = row
    return result


def _digest(value: Any) -> str:
    serialized = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _serialize(value: Any) -> str:
    """The one wire form of a packet or sidecar: sorted, compact and finite."""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"preflight evidence is not finite JSON: {exc}") from None


def field_kind(row: dict[str, Any]) -> str | None:
    """Use the Editor's observed serialization kind; unmeasured values stay unknown."""
    if row.get("value_state") != "present":
        return None
    kind = row.get("value_kind")
    if kind in ("integer", "number", "enum"):
        return "number"
    if kind == "boolean":
        return "bool"
    if kind in ("character", "string"):
        return "string"
    if kind in ("vector2", "vector3", "vector4", "vector2_int", "vector3_int", "quaternion", "color", "color32", "rect", "bounds"):
        return "vector"
    return "other"


def field_offered(row: dict[str, Any]) -> bool:
    """Do not offer a field the inspection explicitly could not read."""
    return row.get("value_state") in ("present", "null") and row.get("value_kind") not in (
        "unsupported", "error", "special_number", "special_vector")


# --- which readable fields the offer can carry ---------------------------------------------------
# A field id is one enum value in the stage-4 schema, so the provider's enum limits bound how many
# readable fields one packet can offer. These are the tiers the offer is filled from, in order.
TIER_PROJECT = "tier_1_project_assembly"
TIER_REFERENCED_TEXT = "tier_1b_script_referenced_text"
TIER_MEASURED = "tier_2_measured_change"
TIER_REFERENCED_VALUE = "tier_2b_script_referenced_value"
TIER_REST = "tier_3_other"
# The two referenced tiers carry a letter, not a new number, so the tier names a saved sidecar or ledger
# already holds keep their meaning; the names still sort in the order the offer is filled.
OFFER_TIERS = (TIER_PROJECT, TIER_REFERENCED_TEXT, TIER_MEASURED, TIER_REFERENCED_VALUE, TIER_REST)
OFFER_BUDGET_REASON = "offer budget"
# What a field of the project's own holds a reference to. Discovery records the target in the field's
# `value`: `kind` "component" with a `component_id`, or "scene_object" with an `object_id`. An asset, an
# unresolved or a destroyed target names nothing in the scene, so it references nothing here.
REFERENCE_VALUE_KIND = "object_reference"
REFERENCED_COMPONENT = "component"
REFERENCED_OBJECT = "scene_object"
# What counts as text, and as an authored value, is the Editor's own serialization of the field: a string,
# or a number or flag that Unity serializes (the component's authored model, not its caches and dirty flags).
TEXT_FIELD_TYPE = "System.String"
SCALAR_VALUE_KINDS = ("integer", "number", "boolean")
# An authored scalar repeats on every referenced component (one text component carries dozens), unlike the
# one string that is its content. So the scalars may take at most this percentage of the field enum; the
# rest stay in the last tier. The text takes no such limit: the references themselves bound it.
REFERENCED_VALUE_PERCENT = 25
# A project's own script sources are its Assets/ tree and the embedded or local packages in its own
# checkout. Registry and built-in packages, and the engine, are not the project's sources, and the
# IL scan never selects them as containing authored scripts.
SCRIPT_ROOT = "Assets/"
PROJECT_PACKAGE_SOURCES = frozenset({"Embedded", "Local"})
# The IL scan's own coverage of the assemblies, types and fields it read. Only when all three are
# complete can a field outside the scanned assemblies be said not to be the project's.
SCAN_COVERAGE = ("il_assemblies", "il_types", "il_fields")
# Calibration's overall and per-window classes that mean a change was actually observed (#freshness).
MEASURED_CHANGE_OVERALL = frozenset({"both", "idle", "input"})
_PROBE_TOKEN = "field_00000"

# --- how much of the packet one call can read ----------------------------------------------------
# The offer above bounds what the model may choose, not what it is shown. The manifest CLI plans every
# call against this context bound (`manifest_cli._approved_budget`), and what the bound leaves for a
# prompt is the bound minus the largest answer the request asks for, minus the answers a retry chain
# can still carry in the conversation, minus a margin nothing measured accounts for.
CONTEXT_BOUND_TOKENS = 1_000_000
CONTEXT_OUTPUT_TOKENS = 32_000
CONTEXT_CONVERSATION_TOKENS = 256_000
CONTEXT_MARGIN_TOKENS = 12_000
CONTEXT_LIMIT_TOKENS = (CONTEXT_BOUND_TOKENS - CONTEXT_OUTPUT_TOKENS
                        - CONTEXT_CONVERSATION_TOKENS - CONTEXT_MARGIN_TOKENS)
# Measured, not assumed (#42): the recorded night selection ledger's stage-4 attempt reports
# usage.prompt_tokens 245,066 for the inputs whose packet rebuilds to 928,107 characters, so one token
# carried 3.787 of them. The quotient is rounded down, and the tokens came from a whole prompt while the
# characters are the packet alone, so an estimate made from this ratio errs high rather than low.
MEASURED_PACKET_CHARS = 928_107
MEASURED_PROMPT_TOKENS = 245_066
MEASURED_CHARS_PER_TOKEN = 3.787
CONTEXT_RATIO_SOURCE = ("one recorded preflight stage-4 attempt's usage.prompt_tokens (245,066) against the "
                        "characters of the packet rebuilt from the same inputs (928,107)")
# Fields the inspection could not read at all, as opposed to readable fields the offer left out.
NOT_READABLE_REASON = "not readable"
UNRECORDED = "unrecorded"
# Discovery sections that describe an object rather than a field, and the key that names that object.
OBJECT_SECTIONS = {"components": "object_id", "bindings": "source_object_id"}
SIDECAR_KIND = "preflight_sidecar"
SIDECAR_VERSION = 1
# An offered field carried by more than one live component says how many. The count rides on the offered
# entry, not on the discovery row: that row is the Editor's own descriptor, which a manifest keeps verbatim
# and a later inspection is compared against, while the offered entry is already the selection's own record.
INSTANCE_KEY = "instances"
INSTANCES_NOTE = (
    "instances appears on an offered field only when more than one live component in the discovered scenes "
    "carries that exact field declaration, and it is how many do. A reader bound to such an id reads one of "
    "those components; the others are separate fields with their own ids, offered or not.")


def estimated_prompt_tokens(chars: int) -> int:
    """Estimated prompt tokens for a serialized packet of `chars` characters, from a measured ratio."""
    return math.ceil(chars / MEASURED_CHARS_PER_TOKEN)


def context_budget(*, before_chars: int, chars: int) -> dict[str, Any]:
    """The measured estimate, the ratio and bound behind it, and the result, recorded in the packet."""
    return {
        "applied": True,
        "reason": ("the offer budget applied, or the packet was over the context one call can hold; full "
                   "descriptor rows are sent for offered fields only"),
        "chars_per_token": MEASURED_CHARS_PER_TOKEN,
        "measured": {"packet_chars": MEASURED_PACKET_CHARS, "prompt_tokens": MEASURED_PROMPT_TOKENS,
                     "source": CONTEXT_RATIO_SOURCE},
        "before_projection": {"packet_chars": before_chars,
                              "estimated_prompt_tokens": estimated_prompt_tokens(before_chars)},
        "packet_chars": chars,
        "estimated_prompt_tokens": estimated_prompt_tokens(chars),
        "bound_tokens": CONTEXT_BOUND_TOKENS,
        "output_tokens": CONTEXT_OUTPUT_TOKENS,
        "conversation_tokens": CONTEXT_CONVERSATION_TOKENS,
        "margin_tokens": CONTEXT_MARGIN_TOKENS,
        "limit_tokens": CONTEXT_LIMIT_TOKENS,
    }


@dataclass(frozen=True)
class OfferPlan:
    """The offered bindings, the fields the budget left out, and the budget facts."""

    bindings: dict[str, str]
    dropped: tuple[tuple[str, str, str], ...]  # (source field id, tier, basis), in discovery order
    summary: dict[str, Any] | None


def _covered(cap: Any) -> bool:
    """Whether an inspection cap reports a complete observation."""
    if cap is None:
        return False
    if isinstance(cap, dict):
        return cap.get("truncated") is False and cap.get("observed") is not None
    return getattr(cap, "truncated", None) is False and getattr(cap, "observed", None) is not None


def _package_source(path: str, packages: list[Any]) -> str | None:
    """The recorded source of the package that owns an asset path, or None when none is recorded."""
    owned = [(row["asset_path"].rstrip("/"), row.get("source")) for row in packages
             if isinstance(row, dict) and isinstance(row.get("asset_path"), str) and row["asset_path"]
             and path.startswith(row["asset_path"].rstrip("/") + "/")]
    if not owned:
        return None
    source = max(owned, key=lambda item: len(item[0]))[1]
    return source if isinstance(source, str) else None


def _script_sources(scripts: list[Any], packages: list[Any]) -> tuple[bool, bool, str]:
    """Whether every authored script resolves to the project's own tree, and whether that needed a package.

    The bridge selects IL assemblies from the authored script files, so a script outside the
    project's own sources would make an assembly's provenance ambiguous. Only paths and recorded
    package sources are read: nothing here matches a project, package or assembly name.
    """
    packaged = False
    for row in scripts:
        path = row.get("path") if isinstance(row, dict) else None
        if not isinstance(path, str) or not path:
            return False, packaged, "a surveyed script has no path"
        normalized = path.replace("\\", "/")
        if normalized.startswith(SCRIPT_ROOT):
            continue
        source = _package_source(normalized, packages)
        if source is None:
            return False, packaged, "a surveyed script outside Assets/ belongs to no recorded package"
        if source not in PROJECT_PACKAGE_SOURCES:
            return False, packaged, "a surveyed script comes from a package the project does not own"
        packaged = True
    return True, packaged, ""


def project_assemblies(discovery: Any, inventory: Any, caps: Any,
                       scan_caps: Any = None) -> tuple[frozenset[str], str, str, bool]:
    """The assemblies evidenced as built from this project's own script sources, and how that was decided.

    Provenance comes from the IL write scan, which reads exactly the assemblies the bridge selected
    because authored scripts compiled into them, never from an assembly's name. It is only usable
    when every authored script the survey recorded resolves to the project's own tree; otherwise no
    assembly is singled out and every one stays unknown. Returns the set, a statement of what was
    established, the path roots it covers, and whether the scan's own assembly and field coverage is
    complete enough to say that an assembly it did not name is not the project's.
    """
    scope = discovery.get("il_scope") if isinstance(discovery, dict) else None
    write_sets = discovery.get("il_write_sets") if isinstance(discovery, dict) else None
    if not isinstance(scope, str) or not scope or not isinstance(write_sets, list):
        return frozenset(), "discovery has no IL scan section naming the assemblies it read", "", False
    names: set[str] = set()
    for row in write_sets:
        ident = row.get("field_id") if isinstance(row, dict) else None
        if not isinstance(ident, str) or ":" not in ident:
            return frozenset(), "the IL scan's write sets do not all name a declaring assembly", "", False
        names.add(ident.split(":", 1)[0])
    if not names:
        return frozenset(), "the IL scan read no assembly, so none is evidenced as the project's own", "", False
    scan_caps = scan_caps if scan_caps is not None else {}
    complete = all(_covered(scan_caps.get(name)) for name in SCAN_COVERAGE)
    scripts = inventory.get("scripts") if isinstance(inventory, dict) else None
    if not isinstance(scripts, list) or not scripts:
        return frozenset(), "the survey recorded no script sources", "", False
    if not _covered(caps.get("scripts")):
        return frozenset(), "the survey's script coverage is incomplete", "", False
    packages = inventory.get("packages") if isinstance(inventory, dict) else None
    packages = packages if isinstance(packages, list) else []
    own, packaged, problem = _script_sources(scripts, packages)
    if not own:
        return frozenset(), f"assembly build provenance is not established: {problem}", "", False
    if packaged and not _covered(caps.get("packages")):
        return frozenset(), "the survey's package coverage is incomplete", "", False
    roots = (f"{SCRIPT_ROOT} and the project's own embedded or local packages" if packaged
             else SCRIPT_ROOT)
    return (frozenset(names),
            f"the assemblies the IL scan read, in a project whose authored scripts all resolve under {roots}",
            roots, complete)


def measured_change(measurement: Any) -> bool:
    """Whether calibration observed this field change, over idle time or under a held input.

    The overall class already combines the idle and held-input windows. The two per-window classes
    are checked as well so a change seen in either window is never lost to a missing summary field;
    a field with no measurement, or only unknown intervals, is not evidence of change.
    """
    if not isinstance(measurement, dict):
        return False
    if measurement.get("classification") in MEASURED_CHANGE_OVERALL:
        return True
    return any(isinstance(measurement.get(scope), dict) and
               measurement[scope].get("classification") in CHANGED_CLASSIFICATIONS
               for scope in ("idle", "input"))


def script_references(fields: dict[str, dict[str, Any]],
                      project: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
    """The scene components and game objects that a field declared by the project's own assemblies references.

    A script that holds a live reference to a component or an object drives it, and what it shows is state
    the game keeps. `fields` is every discovered field row by Editor id. Only a present reference to a
    target in the scene counts (`REFERENCED_COMPONENT` or `REFERENCED_OBJECT`), and only when the field
    that holds it is declared by an assembly evidenced as the project's: with none evidenced there is
    nothing to say the project's scripts hold anything. No name is read, only the ids discovery recorded.
    """
    components: set[str] = set()
    objects: set[str] = set()
    for row in fields.values():
        assembly, value = row.get("declaring_assembly"), row.get("value")
        if (not isinstance(assembly, str) or assembly not in project
                or row.get("value_kind") != REFERENCE_VALUE_KIND or not isinstance(value, dict)):
            continue
        kind = value.get("kind")
        if kind == REFERENCED_COMPONENT and isinstance(value.get("component_id"), str):
            components.add(value["component_id"])
        elif kind == REFERENCED_OBJECT and isinstance(value.get("object_id"), str):
            objects.add(value["object_id"])
    return frozenset(components), frozenset(objects)


def script_referenced(row: dict[str, Any], components: frozenset[str], objects: frozenset[str]) -> bool:
    """Whether a field sits on a component, or on an object, that the project's own fields reference."""
    component, owner = row.get("component_id"), row.get("object_id")
    return ((isinstance(component, str) and component in components)
            or (isinstance(owner, str) and owner in objects))


def text_field(row: dict[str, Any]) -> bool:
    """A string field. A string that was null during discovery has no value kind, so its type says so."""
    return row.get("value_kind") == "string" or row.get("field_type") == TEXT_FIELD_TYPE


def authored_value_field(row: dict[str, Any]) -> bool:
    """A number or flag that Unity serializes: the component's authored model, not a cache or dirty flag."""
    return row.get("serialized") is True and row.get("value_kind") in SCALAR_VALUE_KINDS


def declaration_instances(fields: dict[str, dict[str, Any]]) -> dict[str, int]:
    """How many live components carry each field declaration, by the declaration id discovery recorded.

    One row exists per component per field, so rows sharing a declaration are the instances of the
    component type that declares it, counting the components that inherit it. A row with no declaration
    id is not counted: nothing says which other rows it repeats.
    """
    carriers: dict[str, set[str]] = {}
    for ident, row in fields.items():
        declaration, component = row.get("declaration_id"), row.get("component_id")
        if isinstance(declaration, str) and declaration:
            carriers.setdefault(declaration, set()).add(component if isinstance(component, str) else ident)
    return {declaration: len(ids) for declaration, ids in carriers.items()}


def field_token_budget(control_types: Any, limits: strict.Limits) -> tuple[int, int]:
    """How many field tokens the field enum may carry, and how many enum values other enums take.

    The renderer's check sums every enum in the schema against `enum_values`, so the control enum
    and the model's own literal enums take part of the budget. A field id appears once, so the rest
    of the schema does not depend on how many field tokens there are, and one probe render measures
    it exactly. The big-enum character rule is applied while filling, because a token's width is
    only known once the ids exist.
    """
    controls = dict(control_types) if control_types else {"__no_control_available__": "Button"}
    stats = strict.stats(preflight.schema(preflight.Discovery({_PROBE_TOKEN: ""}, controls), limits=None))
    total = (stats.enum_values_expanded if limits.enum_counting == "expanded"
             else stats.enum_values_definitions)
    reserved = max(total - 1, 0)
    return max(limits.enum_values - reserved, 0), reserved


def _fits(count: int, chars: int, token: str, cap: int, limits: strict.Limits) -> bool:
    """Whether one more token keeps the field enum inside both enum rules."""
    if count + 1 > cap:
        return False
    return not (count + 1 > limits.big_enum and chars + len(token) > limits.big_enum_chars)


def offer_fields(*, fields: dict[str, dict[str, Any]], bindings: dict[str, str],
                 discovery: Any, inventory: Any, caps: Any, evidence: dict[str, Any],
                 control_types: Any, scan_caps: Any = None,
                 evidence_basis: str = "the calibration record's freshness section",
                 limits: strict.Limits | None = None) -> OfferPlan:
    """Offer every readable field, or fill the provider's budget in tiers when they do not fit.

    `bindings` is every readable field token to its Editor id, in discovery order. A field is in
    tier 1 when its declaring assembly is evidenced as built from the project's own script sources.
    Failing that, it is in tier 1b when it is a string on a component, or on an object, that a field
    of the project's own references in the scene; in tier 2 when calibration measured it changing; in
    tier 2b when it is a serialized number or flag on that same referenced set, as far as a percentage
    of the field enum allows (`REFERENCED_VALUE_PERCENT`); and in tier 3 otherwise. Tiers are taken in
    order and, within a tier, in discovery order. When nothing has to be left out the bindings and
    the packet are exactly what they were before the budget existed.

    The two referenced tiers are structural: they read which ids a project field points at and the
    value kind of the target's own fields, never a name. Text outranks calibration's measurement
    because a rare event leaves nothing to measure; an authored number ranks below it because an
    observed change is the stronger evidence.
    """
    limits = strict.MUSE_LIMITS if limits is None else limits
    project, provenance, roots, complete = project_assemblies(discovery, inventory, caps, scan_caps)
    established = bool(project) and complete
    cap, reserved = field_token_budget(control_types, limits)
    components, objects = script_references(fields, project)
    on_referenced = ("on a component, or on an object, that a field declared by an assembly compiled from the "
                     "project's own script sources references in the scene")
    bases = {
        TIER_PROJECT: f"declared by an assembly compiled from the project's own script sources under {roots}",
        TIER_REFERENCED_TEXT: f"a string field {on_referenced}",
        TIER_MEASURED: "calibration measured this field changing over idle time or under held input",
        TIER_REFERENCED_VALUE: f"a serialized number or flag {on_referenced}; no measured change",
        TIER_REST: ("declared outside the project's assemblies; no measured change" if established
                    else "the declaring assembly's build provenance is unknown; no measured change"),
    }
    tiers: dict[str, str] = {}
    for token, ident in bindings.items():
        row = fields[ident]
        assembly = row.get("declaring_assembly")
        referenced = script_referenced(row, components, objects)
        if isinstance(assembly, str) and assembly in project:
            tiers[token] = TIER_PROJECT
        elif referenced and text_field(row):
            tiers[token] = TIER_REFERENCED_TEXT
        elif measured_change(evidence.get(ident)):
            tiers[token] = TIER_MEASURED
        elif referenced and authored_value_field(row):
            tiers[token] = TIER_REFERENCED_VALUE
        else:
            tiers[token] = TIER_REST
    # Authored scalars repeat on every referenced component, so only the first share of the enum is theirs
    # (in discovery order); the others are the rest, no better than any other field nothing measured.
    value_cap = cap * REFERENCED_VALUE_PERCENT // 100
    over_cap = 0
    for token in [token for token in bindings if tiers[token] == TIER_REFERENCED_VALUE][value_cap:]:
        tiers[token] = TIER_REST
        over_cap += 1
    offered: list[str] = []
    chars = 0
    cut: str | None = None
    for tier in OFFER_TIERS:
        for token in bindings:
            if tiers[token] != tier:
                continue
            if not _fits(len(offered), chars, token, cap, limits):
                cut = token
                break
            offered.append(token)
            chars += len(token)
        if cut is not None:
            break
    if cut is None:
        return OfferPlan(dict(bindings), (), None)
    kept = set(offered)
    dropped = []
    for token, ident in bindings.items():
        if token in kept:
            continue
        basis = bases[tiers[token]]
        if tiers[token] == TIER_REST and not isinstance(fields[ident].get("declaring_assembly"), str):
            basis = "the declaring assembly is not recorded; no measured change"
        dropped.append((ident, tiers[token], basis))
    counts = {}
    for tier in OFFER_TIERS:
        tokens = [token for token in bindings if tiers[token] == tier]
        counts[tier] = {"basis": bases[tier], "readable": len(tokens),
                        "offered": sum(1 for token in tokens if token in kept),
                        "unoffered": sum(1 for token in tokens if token not in kept)}
    summary = {
        "applied": True,
        "reason": "the readable fields exceed the provider's field id enum limits",
        "limits": {"enum_values": limits.enum_values, "big_enum": limits.big_enum,
                   "big_enum_chars": limits.big_enum_chars, "enum_counting": limits.enum_counting},
        "reserved_enum_values": reserved,
        "field_token_cap": cap,
        "readable_fields": len(bindings),
        "offered_fields": len(offered),
        "unoffered_fields": len(bindings) - len(offered),
        "cut_tier": tiers[cut],
        "cut_field_id": bindings[cut],
        "tiers": counts,
        "script_references": {"components": len(components), "objects": len(objects),
                              "authored_value_cap": value_cap, "authored_values_over_cap": over_cap},
        "project_assemblies": sorted(project),
        "provenance": provenance,
        "assembly_scan_complete": complete,
        "evidence": evidence_basis,
    }
    return OfferPlan({token: bindings[token] for token in offered}, tuple(dropped), summary)


PAUSE_ERROR_SAMPLES = 3
PAUSE_ERROR_TEXT_CHARS = 160


def pause_runtime_errors(evidence: Any) -> dict[str, Any] | None:
    """What the game logged while the clock was paused or stepped, as measured by calibration.

    Counts come from the monitor's counter and are exact; the messages are a bounded sample and
    may be incomplete, which is stated. A record without clock error evidence gives None: that is
    unmeasured, not "no errors". Support for stepped mode is not decided here or anywhere in code.
    """
    if not isinstance(evidence, dict) or not isinstance(evidence.get("windows"), dict):
        return None
    windows = {name: {"meaning": window.get("meaning"), "count": window.get("count"),
                      "sample_complete": window.get("complete"), "reason": window.get("reason")}
               for name, window in evidence["windows"].items() if isinstance(window, dict)}
    samples = []
    for name, window in evidence["windows"].items():
        for kind in window.get("kinds", []) if isinstance(window, dict) else []:
            if isinstance(kind, dict) and len(samples) < PAUSE_ERROR_SAMPLES:
                message = kind.get("message")
                samples.append({"window": name, "type": kind.get("type"),
                                "message": message[:PAUSE_ERROR_TEXT_CHARS] if isinstance(message, str) else None,
                                "occurrences_in_sample": kind.get("occurrences_in_sample")})
    return {"errors_observed": evidence.get("errors_observed"), "total_count": evidence.get("total_count"),
            "sample_complete": evidence.get("sample_complete"), "gap": evidence.get("gap"),
            "windows": windows, "sample": samples,
            "note": ("counts are exact monitor-counter differences between consecutive reads taken around the "
                     "pause, the 0.2 s paused interval, the step, the resume and a short interval with the clock "
                     "running again; messages are a bounded sample")}


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    """How many rows carry each value of `key`, with a missing value counted as unrecorded."""
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(key)
        label = value if isinstance(value, str) and value else UNRECORDED
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


def field_summary(*, rows: list[dict[str, Any]], kept: set[str], records: list[dict[str, Any]],
                  declaring: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The compact stand-in for the fields the packet does not carry, and their sidecar records.

    `rows` are the projected descriptor rows in discovery order, `kept` the ids of the offered fields,
    `records` the rows that already explain an unoffered field, and `declaring` maps a field id to its
    declaring assembly. The returned summaries count what left the packet by reason, by tier where the
    offer chose it, by declaring assembly and by the read state discovery recorded. Every field that
    left the packet becomes exactly one sidecar record carrying its descriptor row and its record.
    """
    by_ident = {row.get("source_field_id"): row for row in records}
    reasons: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["id"] in kept:
            continue
        record = by_ident.get(row["id"])
        if record is None:
            raise ValueError("an unoffered field has no record of why it was left out")
        reason = record.get("reason") if isinstance(record.get("reason"), str) else NOT_READABLE_REASON
        reasons.setdefault(reason, []).append(record)
    summaries = []
    for reason, items in reasons.items():
        declared = [{"assembly": declaring.get(item["source_field_id"])} for item in items]
        summary = {"reason": reason, "count": len(items), "by_declaring_assembly": _count_by(declared, "assembly")}
        if reason == OFFER_BUDGET_REASON:
            summary["by_tier"] = _count_by(items, "tier")
        else:
            summary["by_value_state"] = _count_by(items, "value_state")
            summary["by_value_kind"] = _count_by(items, "value_kind")
        summaries.append(summary)
    sidecar = []
    for row in rows:
        if row["id"] in kept:
            continue
        record = by_ident[row["id"]]
        sidecar.append({"source_field_id": row["id"], "descriptor": row,
                        **{key: value for key, value in record.items() if key != "source_field_id"}})
    return summaries, sidecar


def object_projection(*, discovery: dict[str, Any], kept: set[str],
                      ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int], list[dict[str, Any]]]:
    """Split the object-describing sections by whether the object declares an offered field.

    Returns the rows that stay in the packet, the count that left each section, and one sidecar
    record per row that leaves. An object with no offered field can contribute no reader, so its
    components and bindings are not part of the choice; they are kept in the sidecar, not dropped.
    """
    kept_rows: dict[str, list[dict[str, Any]]] = {}
    left_out: dict[str, int] = {}
    sidecar: list[dict[str, Any]] = []
    for section, key in OBJECT_SECTIONS.items():
        rows = discovery.get(section)
        if not isinstance(rows, list):
            continue
        stay = [row for row in rows if row.get(key) in kept]
        kept_rows[section] = stay
        left_out[section] = len(rows) - len(stay)
        sidecar += [{"section": section, "object_id": row.get(key), "row": row}
                    for row in rows if row.get(key) not in kept]
    return kept_rows, left_out, sidecar


def sidecar_text(fields: list[dict[str, Any]], objects: list[dict[str, Any]]) -> str:
    """The local sidecar JSON: every row that left the packet, in discovery order."""
    return json.dumps({"kind": SIDECAR_KIND, "schema_version": SIDECAR_VERSION,
                       "note": ("every field and object row this preflight packet does not carry in full; the "
                                "packet records this file's sha256 and row count"),
                       "fields": fields, "objects": objects},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def project_payload(*, payload: dict[str, Any], rows: list[dict[str, Any]], kept: set[str],
                    records: list[dict[str, Any]], declaring: dict[str, Any], before_chars: int,
                    ) -> tuple[dict[str, Any], str]:
    """Send full descriptor rows for offered fields only, and keep everything else in a sidecar.

    `payload` is the packet as built before any context bound; the offer's own budget facts stay in
    it. What leaves a section is summarized where the section was, so the model sees how much it is
    not being shown. The sidecar carries the rows themselves and its sha256 and row count are
    recorded in the packet. When the field projection alone is still over the context one call can
    hold, the object sections for objects with no offered field follow it. If that is still over this
    raises: nothing is sent to a provider that is known to overflow.
    """
    projection = copy.deepcopy(payload)
    summary, field_records = field_summary(rows=rows, kept=kept, records=records, declaring=declaring)
    projection["discovery"]["fields"] = [row for row in rows if row["id"] in kept]
    projection["unoffered_fields"] = summary
    projection["unoffered_fields_note"] = (
        "discovery.fields carries a full descriptor row only for an offered field. Each row here counts the "
        "fields it left out by reason, by tier where the provider's field id limit chose, by declaring assembly "
        "and by the read state discovery recorded. Their full descriptor rows are in the local sidecar named in "
        "sidecar, which is not part of this message; only offered_fields[].id is selectable.")
    objects: list[dict[str, Any]] = []
    kept_objects = {row.get("object_id") for row in rows if row["id"] in kept}
    sidecar = sidecar_text(field_records, objects)
    for _ in range(2):
        projection["sidecar"] = {
            "sha256": hashlib.sha256(sidecar.encode("utf-8")).hexdigest(),
            "rows": len(field_records) + len(objects),
            "field_rows": len(field_records), "object_rows": len(objects),
            "note": ("a local file written beside the manifest, outside the inspected project; it holds every "
                     "field and object row this packet does not carry in full, so nothing was dropped")}
        text = _settled_budget(projection, before_chars)
        if estimated_prompt_tokens(len(text)) <= CONTEXT_LIMIT_TOKENS:
            return projection, sidecar
        if objects:
            break  # the object sections are already out; nothing else can be given up safely
        kept_rows, left_out, objects = object_projection(discovery=projection["discovery"], kept=kept_objects)
        if not objects:
            break
        for section, stayed in kept_rows.items():
            projection["discovery"][section] = stayed
        projection["unoffered_objects"] = {
            "reason": ("these rows describe objects that declare no offered field, so they cannot carry the "
                       "choice; they are kept in the local sidecar named in sidecar"),
            "rows_left_out": left_out,
            "objects_with_an_offered_field": len(kept_objects),
            "note": "each section in discovery lists only its retained rows; the counts here are exact"}
        sidecar = sidecar_text(field_records, objects)
    raise ValueError(
        f"the packet is about {estimated_prompt_tokens(len(text))} tokens even after summarizing the unoffered "
        f"fields and objects, over this model's {CONTEXT_LIMIT_TOKENS}-token prompt budget; no request was sent")


def _settled_budget(projection: dict[str, Any], before_chars: int) -> str:
    """Record the context block and return the text a caller would send, once its size settles.

    The recorded estimate must describe the packet that is sent, and recording it changes that size.
    The numbers stop moving once their own width is accounted for, which at most a few passes reach.
    """
    projection["context_budget"] = context_budget(before_chars=before_chars, chars=before_chars)
    text = _serialize(projection)
    for _ in range(3):
        projection["context_budget"].update(context_budget(before_chars=before_chars, chars=len(text)))
        text = _serialize(projection)
        if projection["context_budget"]["packet_chars"] == len(text):
            break
    return text


def build(inventory: InspectResult, discovery: InspectResult, calibration: CalibrationRecord,
          *, image: Screenshot | None = None) -> Packet:
    """Build a deterministic packet from compatible stage 1 and stage 3 records.

    A current inspection must resolve the measured entity and the same discovered bindings.
    Only keys with at least one valid calibration trial are offered; excluded bindings and
    their coverage are included as facts. Every readable field is offered unless the provider's
    enum limits force a tiered choice, which then names each field it left out and why.
    An offered field that more than one live component carries is marked with how many do, on
    its offered entry only: the discovery row stays the Editor's own descriptor, and a manifest
    keeps that row verbatim as its reader's descriptor.
    Freshness is passed through as measured, including unknowns. A screenshot is not available
    from these records and is explicitly marked null.

    Once the offer budget applies, or the packet alone is over the context one call can hold, full
    descriptor rows are kept for offered fields only: the rest are summarized in the packet and
    returned as a sidecar on the Packet for the caller to write beside the manifest. A packet that
    would still not fit raises here, before any request.
    """
    if inventory.stage != "inventory" or inventory.errors:
        raise ValueError("an inventory inspection without errors is required")
    if discovery.stage != "discovery" or discovery.errors:
        raise ValueError("a discovery inspection without errors is required")
    if "il_inputs" in discovery.data:
        # The bridge's own snapshot names the IL scan's inputs, local file paths among them. Only a
        # snapshot merged with the scan (tools/ilscan) is evidence for the model.
        raise ValueError("discovery has not been merged with the IL write scan")
    if calibration.status != "measured_with_gaps" and not (
            calibration.status == "inconclusive" and not calibration.actions and calibration.error):
        raise ValueError("completed calibration, or an inconclusive record with no controls and a reason, is required")
    if calibration.action_cap.truncated is not False or calibration.action_cap.observed is None:
        raise ValueError("calibration action coverage is incomplete")
    fields = _complete_rows(discovery, "fields")
    objects = _complete_rows(discovery, "game_objects")
    _complete_rows(discovery, "components")
    _complete_rows(discovery, "bindings")
    _complete_rows(discovery, "input_asset_bindings")
    indexed_fields = _index(fields, "id", "fields")
    indexed_objects = _index(objects, "id", "game objects")
    current_entity = indexed_objects.get(calibration.entity_id)
    entity_keys = ("scene_id", "scene_path", "path")
    if (calibration.entity.get("id") != calibration.entity_id or current_entity is None or
            any(not isinstance(calibration.entity.get(k), str) or
                                     not calibration.entity[k] or current_entity.get(k) != calibration.entity[k]
                                     for k in entity_keys)):
        raise ValueError("calibrated entity no longer resolves to the same scene and path")
    current_actions, unsupported = discover_actions(discovery.data)
    current = _index(current_actions, "control", "discovered controls")
    calibrated = _index(calibration.actions, "control", "calibration controls")
    if current != calibrated or calibration.action_cap.returned != len(calibrated):
        raise ValueError("discovered controls or bindings differ from calibration")
    summary_rows = [ActionCalibration.model_validate(a).model_dump(mode="json") for a in calibration.action_summaries]
    summaries = {}
    values = set()
    for row in summary_rows:
        key = (row["control"], row["declared_x"], row["declared_y"])
        if key in values:
            raise ValueError("duplicate calibration value")
        values.add(key)
        summaries.setdefault(row["control"], row)
    if set(summaries) - set(calibrated):
        raise ValueError("a calibration summary names an undiscovered control")
    if any(row["bindings"] != calibrated[row["control"]].get("bindings") or
           row["resolution"] != calibrated[row["control"]].get("resolution") for row in summary_rows):
        raise ValueError("a calibration summary does not match its discovered bindings")
    tested = {t.get("control") for t in calibration.trials if t.get("valid") is True}
    offered = sorted(set(calibrated) & set(summaries) & tested)
    source = discovery.data.get("source_excerpts")
    if not isinstance(source, list):
        raise ValueError("discovery has no source excerpts")
    for name in ("source_files", "source_characters"):
        if name not in discovery.caps:
            raise ValueError(f"discovery has no {name} coverage")
    # The Editor ids can exceed the provider's enum-string budget. These local, deterministic
    # tokens are an exact bijection to the untouched descriptors; they are not name matching.
    all_bindings = {f"field_{index:05d}": ident for index, ident in enumerate(sorted(indexed_fields))}
    readable = {token: ident for token, ident in all_bindings.items() if field_offered(indexed_fields[ident])}
    # Every readable field is offered unless the provider's enum limits force a tiered choice, which
    # then lists every field it left out. A packet that fits the limits is unchanged by this.
    plan = offer_fields(
        fields=indexed_fields, bindings=readable, discovery=discovery.data, inventory=inventory.data,
        caps=inventory.caps, scan_caps=discovery.caps,
        evidence=calibration.freshness if isinstance(calibration.freshness, dict) else {},
        evidence_basis="the per field freshness classification calibration recorded",
        control_types={key: summaries[key].get("control_type") or "Button" for key in offered})
    bindings = plan.bindings
    # An offered field that several live components carry is one of them: say how many, so a reader bound
    # to it is not taken for the only one. Counted over every discovered row, readable or not.
    instances = declaration_instances(indexed_fields)
    carried: dict[str, int] = {}
    for token, ident in bindings.items():
        declaration = indexed_fields[ident].get("declaration_id")
        if isinstance(declaration, str) and instances.get(declaration, 1) > 1:
            carried[token] = instances[declaration]
    field_types: dict[str, str] = {}
    for token, ident in bindings.items():
        row = indexed_fields[ident]
        declared = row.get("field_type")
        field_types[token] = declared if isinstance(declared, str) else ""
    field_kinds = {token: field_kind(indexed_fields[ident]) for token, ident in bindings.items()}
    role_discovery = preflight.Discovery(
        field_types,
        {key: summaries[key].get("control_type") or "Button" for key in offered},
        field_kinds,
    )
    freshness = calibration.freshness or {}
    matched_freshness = []
    for token, ident in bindings.items():
        measurement = freshness.get(ident)
        if isinstance(measurement, dict):
            matched_freshness.append({"field_id": token, "classification": measurement.get("classification"),
                                      "continuous_classification": measurement.get("continuous_classification"),
                                      "sampling_limitation": measurement.get("sampling_limitation")})
    clock = calibration.clock or {}
    reset = calibration.reset or {}
    scene_reset = reset.get("scene_reload") if isinstance(reset.get("scene_reload"), dict) else {}
    # Source code and descriptors are necessary for preflight; runtime field values are not.
    # Omit them explicitly from the model packet while preserving value_state and value_kind.
    discovery_projection = copy.deepcopy(discovery.data)
    projected_fields = [{k: v for k, v in row.items() if k not in ("value", "enum_name")}
                        for row in fields]
    discovery_projection["fields"] = projected_fields
    unoffered = [{"source_field_id": ident, "value_state": indexed_fields[ident].get("value_state"),
                  "value_kind": indexed_fields[ident].get("value_kind")}
                 for token, ident in all_bindings.items()
                 if token not in readable] + [
        {"source_field_id": ident, "value_state": indexed_fields[ident].get("value_state"),
         "value_kind": indexed_fields[ident].get("value_kind"), "reason": OFFER_BUDGET_REASON,
         "tier": tier, "basis": basis} for ident, tier, basis in plan.dropped]
    payload = {
        "inventory": inventory.data,
        "inventory_caps": {k: v.model_dump(mode="json") for k, v in inventory.caps.items()},
        "discovery": discovery_projection,
        "discovery_projection_note": "Runtime field values and enum names are omitted; source excerpts are included.",
        "discovery_caps": {k: v.model_dump(mode="json") for k, v in discovery.caps.items()},
        "offered_fields": [{"id": token, "source_field_id": ident, "field_type": field_types[token],
                            "same_scene_as_entity": indexed_fields[ident].get("scene_id") == calibration.entity.get("scene_id")
                            if isinstance(indexed_fields[ident].get("scene_id"), str) else None,
                            **({INSTANCE_KEY: carried[token]} if token in carried else {})}
                           for token, ident in bindings.items()],
        "unoffered_fields": unoffered,
        "field_id_note": ("Select only offered_fields[].id as field_id. source_field_id is its exact "
                          "Editor descriptor id for provenance, not a selectable id."),
        **({"instances_note": INSTANCES_NOTE} if carried else {}),
        **({"offer_budget": plan.summary, "offer_note": (
            "Every readable field is offered unless the provider's field id limit forced a choice. "
            "unoffered_fields with reason 'offer budget' names each readable field left out, its tier and "
            "the basis for that tier. Fields without a reason could not be read during discovery.")}
           if plan.summary is not None else {}),
        "calibration": {
            "entity_id": calibration.entity_id, "entity": calibration.entity,
            "coordinate_frame": calibration.coordinate_frame, "units": calibration.units,
            "physical_unit": calibration.physical_unit,
            "freshness": {"id_matches_unverified": matched_freshness, "total_fields": len(freshness),
                          "source_sha256": _digest(calibration.freshness)},
            "clock": {"stepped_mode_supported": clock.get("stepped_mode_supported"),
                      "reason": clock.get("reason"), "error": clock.get("error"),
                      "pause_runtime_errors": pause_runtime_errors(clock.get("runtime_errors")),
                      "source_sha256": _digest(calibration.clock)},
            "field_sampling_gap": calibration.field_sampling_gap,
            "reset": {"scene_reload": {k: v for k, v in scene_reset.items() if k not in ("fields", "evidence", "trials")},
                      "save_load": reset.get("save_load"), "checkpoint": reset.get("checkpoint"),
                      "source_sha256": _digest(calibration.reset)},
            "control_summaries": [row for k in offered for row in summary_rows if row["control"] == k],
            "field_identity_note": ("Freshness uses within-run field ids. The calibration record does not "
                                    "preserve full field descriptors, so matching ids alone do not prove "
                                    "identity across separately captured sessions."),
            "summary_note": ("Full freshness, clock and reset samples are omitted from this prompt and remain "
                             "in the local calibration record; the hashes identify the supplied source data."),
            "unsupported_bindings": unsupported,
            "unoffered_controls": [calibrated[k] for k in sorted(set(calibrated) - set(offered))],
        },
        "offered_controls": [{
            "id": key, **calibrated[key],
            "device": summaries[key].get("device") or "Keyboard",
            "control": summaries[key].get("control_name") or summaries[key].get("control") or key,
            "control_type": summaries[key].get("control_type") or "Button",
            "declared_x": summaries[key].get("declared_x"),
            "declared_y": summaries[key].get("declared_y"),
        } for key in offered],
        "image": image.descriptor() if image is not None else None,
        "image_unavailable_reason": ("no screenshot was supplied" if image is None else None),
        "capability_note": (
            "Each declared act names its physical device controls, normalized values and its hold or ordered "
            "steps. Mouse delta is one relative impulse per apply, with x and y multiplied by the fixed 64-pixel "
            "component bound; it is never repeated every update. Gamepad values go unchanged into device state. "
            "Keyboard and device buttons take null x and y; vector controls take both values; a one-dimensional "
            "axis uses x with y = 0. Triggers use x in [0, 1]; dpad components must each be -1, 0 or 1. "
            "Relative scroll, absolute pointer, touch, VR and remapping are unsupported. "
            "A null action hold requests a tap; if no tested requested hold exists its duration is unknown. "
            "Separately measured keys do not prove a chord, a different value or a skill. Execution support and "
            "effects remain unknown until exact release-verified evidence exists."
        ),
    }
    text = _serialize(payload)
    # The offer bounds what the model may choose, not what it is shown: a discovery with many
    # thousands of fields otherwise carries every field's full row however large the packet becomes.
    # When the offer budget applies, or the packet alone is over what one call can hold, the full
    # rows go to offered fields only and everything else is summarized here and kept in a sidecar.
    sidecar = None
    if plan.summary is not None or estimated_prompt_tokens(len(text)) > CONTEXT_LIMIT_TOKENS:
        payload, sidecar = project_payload(
            payload=payload, rows=projected_fields, kept=set(bindings.values()), records=unoffered,
            declaring={ident: indexed_fields[ident].get("declaring_assembly") for ident in indexed_fields},
            before_chars=len(text))
        text = _serialize(payload)
    measured_taps = frozenset(k for k in offered if
                              summaries[k]["shortest_observed_effective_hold_s"] is not None and
                              0 < summaries[k]["shortest_observed_effective_hold_s"] <= 3600)
    return Packet(text, role_discovery, bindings, measured_taps, image,
                  tuple(calibration.execution_evidence), sidecar)
