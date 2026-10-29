"""Wire and record contracts. These models are the single schema source; `schemas/` is exported from them."""

from __future__ import annotations

from pydantic import BaseModel

from . import action_catalog, bridge, calibration, investigation, loop, manifest
from .common import PROTOCOL, ClockMode, Envelope

# Every contract with an exported JSON Schema, by schema file stem.
CONTRACTS: dict[str, type[BaseModel]] = {
    "hello": bridge.Hello,
    "status": bridge.Status,
    "observe_request": bridge.ObserveRequest,
    "observation": bridge.Observation,
    "act_request": bridge.ActRequest,
    "execution_record": bridge.ExecutionRecord,
    "stop_request": bridge.StopRequest,
    "stop_result": bridge.StopResult,
    "clock_request": bridge.ClockRequest,
    "inspect_request": bridge.InspectRequest,
    "inspection_result": bridge.InspectResult,
    "calibration_record": calibration.CalibrationRecord,
    "preflight_manifest": manifest.PreflightManifest,
    "action_catalog": action_catalog.ActionCatalog,
    "bridge_error": bridge.BridgeError,
    "choice_set": loop.ChoiceSet,
    "decision": loop.Decision,
    "turn_record": loop.TurnRecord,
    "directive": investigation.Directive,
    "finding": investigation.Finding,
    "run_record": investigation.RunRecord,
}

__all__ = ["CONTRACTS", "PROTOCOL", "ClockMode", "Envelope", "action_catalog", "bridge", "investigation", "loop"]
