"""Run output: which values a run's files must never hold.

The trace is written by `playtest.core.trace.TraceWriter` (#30): the `run` entry first, each validated
line flushed as it is appended, the `end` entry last. The writer replaces every secret value it is given
and counts the replacements; this module names those values. Full traces stay local under `runs/`;
nothing here is meant to be committed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


def secret_values(environ: Mapping[str, str] | None = None) -> list[str]:
    """Values of every `*_API_KEY` variable in the environment, so a trace can be checked for them."""
    env = os.environ if environ is None else environ
    return sorted({value for name, value in env.items() if name.endswith("_API_KEY") and len(value) >= 8})
