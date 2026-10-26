---
name: playtest-contract-change
description: Change or review a unity-playtest-agent contract, exported JSON Schema, shared fixture, or schema consumer. Use for edits under src/playtest/contracts, schemas, or fixtures/contracts, or for a C# or Python consumer; covers source authority, cross-field rules absent from the export, fixtures, drift, unknown values and parity.
---

# Contract change

Paths are relative to the target repository root, the checkout containing `src/playtest/`. Do not infer the root from where this skill is installed.

Implementation notes describe `879f9129`; verify component availability in the target checkout before applying them.

## Scope

Use when a change touches `src/playtest/contracts/**`, `schemas/**`, `fixtures/contracts/**`, or a consumer. If only runtime behaviour changes, use the ownership or evidence skill. This is not a generic data-model review.

## Binding rules

The Pydantic models in `src/playtest/contracts/` are the single source. `CONTRACTS` maps schema stems; `export.py` renders `schemas/<stem>.schema.json`; fixtures hold `valid-*` and `invalid-*`; `test_contracts.py` checks fixtures and drift. Never hand-edit a generated schema or add a second authority.

- Wire messages inherit `Envelope`; local records (`ChoiceSet`, `Decision`, `TurnRecord`, `Directive`, `Finding`, `RunRecord`) do not. `ExecutionRecord` is a wire message despite the name.
- `Strict` forbids extra fields and freezes the model.
- Use enum or `Literal` where the protocol closes a set: execution status, reject reason, directive outcome, finding state, clock mode, stop reason, bridge error. Ids, labels, text and field references stay strings; do not enum every string.
- Unmeasured wire and effect values are `None`; a missing reader is `null` plus `reader_errors`. `RunRecord` uses `UNKNOWN`, currently the lowercase string `"unknown"`. Read the model; do not invent an uppercase convention.
- Cross-field rules are Pydantic validators, for example `Choice._kind_fields`, `WhilePending._continue_has_choice`, `Directive._consistency`, `Repro._k_le_n`, `ExecutionRecord._reject_has_reason`, `ClockRequest._op_args`. The current export does not automatically encode these validators; shared invalid fixtures expose the extra checks consumers must implement.
- Names stay generic; game knowledge lives in the manifest.
- Keep provider schemas closed where strict mode requires it. Never loosen `additionalProperties`, drop a required field or add a permissive branch solely to accept invalid output. Intentional supported evolution changes the model, fixtures, schema and every consumer together.

At this snapshot the C# bridge and its DTO consumer do not exist. C# parity is pending, not verified.

## Procedure

1. Classify the contract as a wire message or local record and name every consumer. Make the smallest model change. Prefer a closed enum where the protocol closes the set, and an explicit nullable field over a sentinel. If you add a contract, add it to `CONTRACTS`.
2. Add one `valid-*.json` fixture and at least one `invalid-*.json` fixture whose name states the rejected behaviour. The invalid fixture must fail for the intended rule, not a missing field or another enum. `test_fixture_against_model` only asserts that some `ValidationError` occurs, so inspect the actual error.
3. For a cross-field rule, add the validator and identify whether the generated schema encodes it. Do not assume export preserves Python validators or hand-edit the generated schema to compensate.
4. Export and test:

   ```sh
   uv run playtest-export-schemas
   uv run pytest tests/test_contracts.py
   ```

   Inspect `git diff -- schemas/`; only intended stems may change. `test_schema_not_drifted` must pass. Run the full `uv run pytest` when a consumer or shared behaviour changed.
5. Update every Python consumer in the same change. If one cannot be updated, stop; do not land half a contract.
6. If a C# consumer exists, update its DTO and run it against the same fixtures. If not, report parity as pending, not verified. Do not name a bridge command or fixture runner that does not exist.
7. Confirm no game-specific identifier enters `src/playtest/core/`, and run `uv run pytest tests/test_name_blind.py`.

## Outcome and stop

Report the changed model, valid and invalid fixtures, regenerated schemas, commands and results, diff scope, and C# parity status. If a claim is unsupported or unknown, say which consumer or fixture is missing and what check would settle it.

Stop if an invalid fixture passes or fails for an unrelated reason. Stop if the export changes unrelated schemas or a consumer cannot be updated in the same change. Stop if a closed protocol set becomes a free string, or a game field enters generic or core code. Stop if the only way to pass is loosening the schema or defaulting an unmeasured value solely to accept invalid output. Stop if parity is claimed without a consumer. Scale the check: a comment-only edit needs no new fixture, but a model change does.
