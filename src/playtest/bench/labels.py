"""Create blank label sheets from recorded contexts for a human reviewer to complete."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playtest.bench.record import digest, recorded_payload


def make_sheet(corpus_path: Path, directory: Path) -> None:
    raw = corpus_path.read_bytes()
    corpus = json.loads(raw)
    frames = corpus.get("frames")
    if corpus.get("status") != "complete" or not isinstance(frames, list) or len(frames) != 40:
        raise ValueError("a complete 40-frame recording is required")
    if directory.exists():
        raise ValueError("choose a new directory; existing labels are never overwritten")
    labels, views = [], []
    ids: set[str] = set()
    for frame in frames:
        frame_id = frame.get("id")
        if not isinstance(frame_id, str) or not frame_id or frame_id in ids:
            raise ValueError("frame ids must be unique and nonempty")
        ids.add(frame_id)
        payload = recorded_payload(frame)
        if payload is None:
            raise ValueError(f"context changed for {frame_id}")
        labels.append({"frame_id": frame_id, "payload_sha256": frame["payload_sha256"],
                       "acceptable": [], "labeller": "", "date": "", "rationale": ""})
        views.append({"frame_id": frame_id, "payload": payload})
    directory.mkdir(parents=True)
    for name, value in (("labels.json", {"version": 1, "corpus_sha256": digest(raw), "labels": labels}),
                        ("contexts.json", {"version": 1, "frames": views})):
        with (directory / name).open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        make_sheet(args.corpus, args.directory)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"label sheet: {exc}", file=sys.stderr)
        return 1
    print("Created blank labels.json and contexts.json for human review; no labels were generated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
