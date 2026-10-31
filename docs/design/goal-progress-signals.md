# Declared goal-progress measurements

This is the implementation brief following the session-13 progress review for #91, related to #36.
The feature is not implemented or validated by this note. The offline review is local at
`runs/b04-hunt/progress-check-review/NOTE.md`.

## Decision

Implement opt-in, manifest-declared reach regions and a derived distance from a declared measured pose.
Do not ship height gain alone as an arrival check. The review's negative control gained height away from
the intended structures; another reached a footprint beside an inaccessible face. Neither establishes
arrival at an entry region or use of a structure.

The region declaration is an assertion about a place. Its author, source and verification status must
be explicit. A preflight model's label, coordinates or explanation remain unverified inferences. A
measured pose and a distance computed from it do not verify the declaration's meaning. Both S1 and the
frontier must receive the declaration, provenance, pose source and derived measurement.

## Scope

- A manifest without reach declarations keeps its current behavior and request sections.
- A declaration names a unique id, a point/region in a stated coordinate frame, its scene binding,
  author/source, verification status and purpose. Prefer an explicit entry volume to a whole footprint.
  All coordinates and dimensions must be finite and valid; do not repair malformed geometry.
- A pose source must name an actually supported measurement and coordinate frame. The bridge's probe
  anchor is one possible source; describe it as that anchor, without inferring a different entity's pose.
  Reject unsupported source/frame combinations. Missing pose, wrong scene or unavailable frame means
  a null measurement and an explicit reason, never zero distance or success.
- Compute only distance to the declared region and membership in it. Give the frontier the resulting
  numeric fields as valid setpoint fields only for declarations this manifest actually contains.
  Do not open every observed field to setpoints or infer a target from a directive's prose.
- An outcome may say that the measured point was inside the declared region. It cannot say that the
  objective was completed, a particular object was contacted, or an ascent/defect test was performed.
- These measurements never select, rank or replace actions, extend deadlines, suppress choices or
  request help. S1 owns every decision. A new observation epoch invalidates old pose/baseline state.

Keep the existing null setpoint when no declaration can demonstrate the proposed outcome. Do not
retrospectively score old runs using geometry that was never in their manifests.

## Acceptance

Use opaque synthetic declarations in tracked tests. Test an entry volume against an adjacent face,
movement away from a target, a height gain elsewhere, missing readings, a scene/frame mismatch,
non-finite or reversed geometry and a restart boundary. Check that existing undeclared fields remain
invalid setpoints, all valid choices remain identical, and old manifests remain usable. Exercise the
actual serialized S1 and frontier request paths rather than only the geometry helper.

The session-13 counterexamples may be used locally as negative controls, with their operator-declared
geometry clearly identified as analysis input. They are not a production manifest or live validation.
No navigation-quality claim follows from these checks. A later paid comparison needs its own frozen
inputs, call count and approval; this implementation work makes no provider calls.
