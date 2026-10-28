"""Verify trace integrity and adherence from stored evidence."""

from __future__ import annotations

import argparse
from collections import Counter

from .trace import read_trace
from .trace_replay import replay
from playtest.contracts.trace import TraceKind


def main() -> int:
    parser = argparse.ArgumentParser(prog="playtest-trace")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("trace")
    args = parser.parse_args()
    try:
        entries = read_trace(args.trace)
    except Exception as exc:
        print(f"invalid trace: {exc}")
        return 1
    problems = replay(entries)
    counts = Counter(row.body.get("outcome") for row in entries if row.kind is TraceKind.turn)
    print(f"turns={sum(counts.values())} outcomes={dict(counts)} adherence={'failed' if any(str(p).find('adherence:') >= 0 for p in problems) else 'clean'} problems={len(problems)}")
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
