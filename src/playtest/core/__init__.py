"""Game-agnostic core. Nothing in this package may name a game, its fields, controls or genre.

`tests/test_name_blind.py` enforces that rule.
"""

from . import adherence, bridge_client, decisions, execution, observations, turn_loop

__all__ = ["adherence", "bridge_client", "decisions", "execution", "observations", "turn_loop"]
