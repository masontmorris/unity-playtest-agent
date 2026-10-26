"""Structured-output contracts for the model roles that answer in JSON (build bible 8.1, 8.4).

Each role module holds the answer models (the single schema source), the role's stable prompt text
and the validation code checks after the provider's constrained decoding. `strict` renders a model
into the provider's strict JSON Schema subset, with run-stable enums injected at render time.

These are candidate contracts (#73). The preflight (#27) and frontier (#33) providers consume them.
"""
