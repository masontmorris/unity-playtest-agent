"""Measure per-field freshness from repeated field samples.

This module reports sampled facts for preflight. It does not choose, rank or
escalate an action, and a freshness class never becomes an input decision here.

Terms
-----
A *window* is one list of samples whose adjacent pairs may be compared. The
idle samples form one window. Every item in ``input_segments`` is a separate
held-input window; the edge between two windows is never an interval.

A *comparable* value is one of:

* ``value_state == "present"`` with ``value_kind`` in
  ``{"number", "integer", "boolean", "enum", "string"}``, a JSON primitive
  value of the matching type, no read error, no truncation and a finite numeric
  value; or
* ``value_state == "null"`` with ``is_null is True`` and ``value is None``.

An *interval* is one adjacent pair inside a window. It is comparable only when
both endpoint values are comparable. A change is exact inequality of those
values; there is no tolerance, interpolation, smoothing or variance estimate.
An interval touching a missing, unsupported, errored, destroyed, truncated or
non-finite sample is unknown: it is neither a change nor an unchanged interval.

The reported counts describe only the intervals that were actually observed.
Between samples and across window edges nothing is measured, so a
``no_change_observed`` class does not mean the value was constant at all times;
it means no change was seen in the comparable intervals.
"""

from __future__ import annotations

import math
from typing import Any

COMPARABLE_KINDS = frozenset({"number", "integer", "boolean", "enum", "string"})
CHANGED_CLASSIFICATIONS = frozenset({"observed_continuous", "observed_change"})
SAMPLING_LIMITATION = (
    "Freshness is measured only from adjacent samples inside one window. Values "
    "between samples and edges between held-input windows are not observed; an "
    "interval touching a missing, unsupported, errored, destroyed, truncated or "
    "non-finite sample is unknown."
)


def _validated_fields(samples: list[dict], label: str) -> list[dict[str, dict]]:
    """Validate one window and index each sample's field rows by id.

    Structural damage that would make a field id ambiguous raises ``ValueError``.
    A missing field id is not structural damage: that sample simply has no
    evidence for the field.
    """
    if not isinstance(samples, list):
        raise ValueError(f"{label} must be a list of samples")

    maps: list[dict[str, dict]] = []
    previous: int | float | None = None
    for sample_index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise ValueError(f"{label} sample {sample_index} is not an object")
        if "real_time_s" not in sample:
            raise ValueError(f"{label} sample {sample_index} has no real_time_s")
        current = sample["real_time_s"]
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise ValueError(f"{label} sample {sample_index} has a non-finite real_time_s")
        if isinstance(current, float) and not math.isfinite(current):
            raise ValueError(f"{label} sample {sample_index} has a non-finite real_time_s")
        if previous is not None and current <= previous:
            raise ValueError(f"{label} real_time_s must strictly increase within one window")
        previous = current

        rows = sample.get("fields")
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise ValueError(f"{label} sample {sample_index} fields must be a list")

        by_id: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{label} sample {sample_index} has a non-object field row")
            field_id = row.get("id")
            if not isinstance(field_id, str) or not field_id:
                raise ValueError(f"{label} sample {sample_index} has a field row without an id")
            if field_id in by_id:
                raise ValueError(f"{label} sample {sample_index} repeats field id {field_id!r}")
            by_id[field_id] = row
        maps.append(by_id)
    return maps


def _comparable(row: dict | None) -> tuple[str, object] | None:
    """Return a tagged exact value, or ``None`` when this sample is not evidence.

    The kind tags the value so that, for example, boolean ``False`` is not
    compared equal to integer ``0``.
    """
    if row is None:
        return None

    if row.get("read_error") is not None:
        return None

    truncated = row.get("value_truncated")
    if truncated is not None and truncated is not False:
        return None

    state = row.get("value_state")
    value = row.get("value")

    if state == "null":
        if row.get("is_null") is not True or value is not None:
            return None
        return ("null", None)

    is_null = row.get("is_null")
    if state != "present" or (is_null is not None and is_null is not False):
        return None

    kind = row.get("value_kind")
    if not isinstance(kind, str) or kind not in COMPARABLE_KINDS:
        return None

    if kind == "string":
        # A string without an explicit "not truncated" flag could hide a change
        # past the returned prefix, so it is unknown rather than comparable.
        if not isinstance(value, str) or truncated is not False:
            return None
    elif kind == "boolean":
        if not isinstance(value, bool):
            return None
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return None
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
    else:  # enum
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
    return (kind, value)


def _classification(comparable: int, changed: int, max_run: int) -> str:
    if max_run >= 2:
        return "observed_continuous"
    if changed:
        return "observed_change"
    if comparable >= 2:
        return "no_change_observed"
    return "unknown"


def _stats(comparables: list[tuple[str, object] | None], present: list[bool]) -> dict[str, Any]:
    """Summarise one window without comparing its edges to any other window."""
    comparable = 0
    changed = 0
    unchanged = 0
    unknown = 0
    run = 0
    max_run = 0

    for left, right in zip(comparables, comparables[1:]):
        if left is None or right is None:
            unknown += 1
            run = 0
            continue
        comparable += 1
        if left != right:
            changed += 1
            run += 1
            max_run = max(max_run, run)
        else:
            unchanged += 1
            run = 0

    sample_count = len(comparables)
    comparable_samples = sum(value is not None for value in comparables)
    field_present_samples = sum(present)
    return {
        "sample_count": sample_count,
        "field_present_samples": field_present_samples,
        "comparable_samples": comparable_samples,
        "comparable_intervals": comparable,
        "changed_intervals": changed,
        "unchanged_intervals": unchanged,
        "unknown_intervals": unknown,
        "consecutive_changed_intervals_max": max_run,
        "complete": (
            sample_count >= 2
            and unknown == 0
            and comparable_samples == sample_count
        ),
        "classification": _classification(comparable, changed, max_run),
    }


def _aggregate_segments(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum independent windows; never merge values or consecutive runs."""
    sample_count = sum(segment["sample_count"] for segment in segments)
    present_samples = sum(segment["field_present_samples"] for segment in segments)
    comparable_samples = sum(segment["comparable_samples"] for segment in segments)
    comparable = sum(segment["comparable_intervals"] for segment in segments)
    changed = sum(segment["changed_intervals"] for segment in segments)
    unchanged = sum(segment["unchanged_intervals"] for segment in segments)
    unknown = sum(segment["unknown_intervals"] for segment in segments)
    max_run = max(
        (segment["consecutive_changed_intervals_max"] for segment in segments),
        default=0,
    )
    incomplete = [segment["index"] for segment in segments if not segment["complete"]]
    return {
        "segment_count": len(segments),
        "sample_count": sample_count,
        "field_present_samples": present_samples,
        "comparable_samples": comparable_samples,
        "comparable_intervals": comparable,
        "changed_intervals": changed,
        "unchanged_intervals": unchanged,
        "unknown_intervals": unknown,
        "consecutive_changed_intervals_max": max_run,
        "complete": bool(segments) and not incomplete,
        "incomplete_segments": incomplete,
        "segments": segments,
        "classification": _classification(comparable, changed, max_run),
    }


def _overall(idle_classification: str, input_classification: str) -> str:
    idle_changed = idle_classification in CHANGED_CLASSIFICATIONS
    input_changed = input_classification in CHANGED_CLASSIFICATIONS
    if idle_changed and input_changed:
        return "both"
    if idle_changed:
        return "idle"
    if input_changed:
        return "input"
    if idle_classification == "no_change_observed" and input_classification == "no_change_observed":
        return "no_change_observed"
    return "unknown"


def _continuous_overall(idle_classification: str, input_classification: str) -> str:
    idle = idle_classification == "observed_continuous"
    held = input_classification == "observed_continuous"
    if idle and held:
        return "both"
    if idle:
        return "idle"
    if held:
        return "input"
    if "observed_change" in (idle_classification, input_classification):
        return "change_observed_without_continuous_evidence"
    if idle_classification == input_classification == "no_change_observed":
        return "no_change_observed"
    return "unknown"


def analyze_freshness(
    idle_samples: list[dict],
    input_segments: list[list[dict]],
) -> dict[str, dict]:
    """Report per-field freshness evidence for idle and held-input windows.

    ``idle_samples`` is one window. Each item in ``input_segments`` is a distinct
    held-input window; adjacent samples are compared only within their own
    window. ``real_time_s`` must be finite and strictly increasing in every
    window. A repeated field id inside one sample raises ``ValueError``, as do
    malformed sample structures and non-increasing timestamps.

    The returned mapping is keyed by field id. Every value has separate ``idle``
    and ``input`` evidence, an overall ``classification`` and the sampling
    limitation. Counts always describe observed intervals only; unknown
    intervals are reported separately and never folded into unchanged intervals.
    """
    idle_maps = _validated_fields(idle_samples, "idle")
    if not isinstance(input_segments, list):
        raise ValueError("input_segments must be a list of sample windows")
    segment_maps = [
        _validated_fields(segment, f"input segment {index}")
        for index, segment in enumerate(input_segments)
    ]

    field_ids: set[str] = set()
    for mapping in idle_maps:
        field_ids.update(mapping)
    for mapping in segment_maps:
        for sample in mapping:
            field_ids.update(sample)

    result: dict[str, dict] = {}
    for field_id in sorted(field_ids):
        idle_comparables = [_comparable(sample.get(field_id)) for sample in idle_maps]
        idle_present = [field_id in sample for sample in idle_maps]
        idle = _stats(idle_comparables, idle_present)

        segments: list[dict[str, Any]] = []
        for index, mapping in enumerate(segment_maps):
            comparables = [_comparable(sample.get(field_id)) for sample in mapping]
            present = [field_id in sample for sample in mapping]
            segments.append({"index": index, **_stats(comparables, present)})

        input_evidence = _aggregate_segments(segments)
        result[field_id] = {
            "id": field_id,
            "idle": idle,
            "input": input_evidence,
            "classification": _overall(idle["classification"], input_evidence["classification"]),
            "classification_scope": "where_any_change_was_observed",
            "continuous_classification": _continuous_overall(idle["classification"], input_evidence["classification"]),
            "sampling_limitation": SAMPLING_LIMITATION,
        }
    return result
