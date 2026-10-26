# Context fidelity reference

Read this when an ownership audit includes what S1 was actually sent, or when a run is explained by "S1 ignored", "waited", "forgot" or "never saw" something. The actuator rules remain in `SKILL.md`.

## Reconstruct the real request

Use the production builder's output, not a summary of it. Record the builder path, payload version or hash, choice-set version, observation sequence, and the exact serialized fields. If the payload contains an image or clip, record its reference and consumer. If no production context builder exists, review the builder design and any frozen request artifacts, and label runtime behaviour unverified. Do not invent a command or a JSON shape.

## Trace evidence forward

For each fact, directive and choice that matters to the claim, follow one chain: observation sequence -> extraction or derivation -> fact -> window or compaction -> serialized field. Record the source sequence, age, and whether the value is measured or inferred. A value that appears in a short view but not in the live record, or the reverse, is a finding about the view, not about the game.

## Do not confuse persistence with mention

A live directive is a separate field that can persist across turns. A compact-history entry about that directive can expire from the window while the directive remains current. Do not treat an old compact entry as the live direction, and do not conclude that guidance was evicted because its entry left the window. Check both the live field and the bounded history.

## Check meaning and executability

- Check documented wait/release and hold-renewal semantics against the current executor. In the prototype, `wait` released controls, a hold required another selection, and supersession transferred ownership. These observations do not prove the new runtime behaves the same way; its semantics must be explicit to S1.
- An action choice must be in the current set, version-matched, precondition-satisfied and executable against the current observation. Expiry, a lost target or a failed precondition must be recorded with a reason; the current contract has no removal-reason field, so treat that as a design gap.
- On a real-time help turn, `while_pending` states what S1 wants while help is pending. It does not authorise code to choose a continuation.
- A pending result is adopted once with its source and age. Do not let a shortened history hide an unresolved hypothesis, a failed attempt, a contradiction or a negative result.

## Keep the core name-blind

Check reader names and semantics against the manifest, not a hardcoded list in `src/playtest/core/`. A generic-looking suffix rule is still game knowledge if it assumes a field family. The manifest declares readers, units and preconditions; the core may not add a game-specific key to make a case work.

## Outcome

Report the payload hash or identifier, the fact/choice provenance table, the persistence check, the executability check and any missing builder path. If the payload cannot be reconstructed, say so and name the artifact or design change that would make the review possible; do not infer runtime behaviour from the source alone.
