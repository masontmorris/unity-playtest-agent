"""Run trace vocabulary: the kinds of entry an append-only trace holds and who writes them (build bible 5.7).

`playtest.core.trace_sink` documents each kind's body. Issue #30 adds the entry model here.
"""

from __future__ import annotations

from enum import Enum


class TraceKind(str, Enum):
    run = "run"
    observation = "observation"
    choice_set = "choice_set"
    context = "context"
    s1_call = "s1_call"
    decision = "decision"
    execution = "execution"
    effect = "effect"
    help = "help"
    turn = "turn"
    stop = "stop"
    end = "end"


class TraceAuthor(str, Enum):
    harness = "harness"  # the process that opened the trace: run record and end of run
    core = "core"
    bridge = "bridge"
    s1 = "s1"
    frontier = "frontier"
    helper = "helper"
