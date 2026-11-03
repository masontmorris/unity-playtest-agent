"""Freeze four recorded S1 contexts for the proposed Modal Mammoth pilot.

This is an offline preparation tool. It never calls a model or starts Modal.
Input traces and image frames remain local under runs/; no game assets are copied
to the repository. The image digest must match the one in the recorded context.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
from typing import Any


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_body(body: dict[str, Any], *, flide_image: bool = False) -> bytes:
    """Match the reference wrapper's rendered state order for an image request.

    The reference adapter constructs ``state`` before ``images``. Flide renders
    JSON objects in received order, so sorting the outer state dictionary would
    put the image before the state and silently change the model prompt.
    Canonicalize nested values and question options first, then restore that
    specific two-key order before encoding the Flide request.
    """
    if not flide_image:
        return canonical(body)
    ordered = json.loads(canonical(body))
    state = ordered["state"]
    if set(state) != {"state", "images"} or state["images"] != ["<image:1>"]:
        raise ValueError("unexpected Flide image state")
    ordered["state"] = {"state": state["state"], "images": state["images"]}
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def read_context(trace: Path, turn: int) -> tuple[dict[str, Any], str]:
    found = []
    with trace.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("kind") == "context" and row.get("turn") == turn:
                found.append(row["body"])
    if len(found) != 1:
        raise ValueError(f"expected one context for turn {turn}, got {len(found)}")
    context = found[0]["payload"]
    if sha(canonical(context)) != found[0]["sha256"]:
        raise ValueError("context hash does not match trace")
    return context, found[0]["sha256"]


def make_bodies(context: dict[str, Any], image: bytes | None) -> tuple[dict[str, Any], dict[str, Any]]:
    choices = context["choices"]
    criteria = {choice["id"]: choice["label"] for choice in choices}
    if len(criteria) != len(choices) or not criteria:
        raise ValueError("choice IDs must be unique and nonempty")
    questions = {"next_action": {"type": "choice", "instructions": context["instructions"],
                                 "criteria": criteria}}
    if image is None:
        if context.get("game_view_status") == "sent":
            raise ValueError("recorded context requires its image")
        return {"state": context, "questions": questions}, {"state": context, "questions": questions}
    if context.get("game_view_status") != "sent" or context.get("game_view_sha256") != sha(image):
        raise ValueError("image is not the game view recorded for this context")
    url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
    # The published-runtime adapter builds this same placeholder state from its
    # top-level images array. Rendered-token equivalence still needs checking.
    flide = {"state": {"state": context, "images": ["<image:1>"]},
             "questions": questions, "media": [{"type": "image", "data": url}]}
    reference = {"state": context, "questions": questions, "images": [url]}
    return flide, reference


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text-trace", type=Path, required=True)
    parser.add_argument("--image-trace", type=Path, required=True)
    parser.add_argument("--short-image", type=Path, required=True)
    parser.add_argument("--long-image", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    specs = [("short-text", args.text_trace, 0, None), ("long-text", args.text_trace, 89, None),
             ("short-image", args.image_trace, 0, args.short_image),
             ("long-image", args.image_trace, 37, args.long_image)]
    args.out.mkdir(parents=True, exist_ok=False)
    manifest = []
    for name, trace, turn, image_path in specs:
        context, context_hash = read_context(trace, turn)
        image = image_path.read_bytes() if image_path else None
        flide, reference = make_bodies(context, image)
        row = {"name": name, "turn": turn, "trace_sha256": sha(trace.read_bytes()),
               "context_sha256": context_hash, "image_sha256": sha(image) if image else None}
        for arm, body in (("flide", flide), ("reference", reference)):
            data = encode_body(body, flide_image=arm == "flide" and image is not None)
            (args.out / f"{name}-{arm}.json").write_bytes(data)
            row[f"{arm}_body_sha256"] = sha(data)
        manifest.append(row)
    (args.out / "manifest.json").write_bytes(canonical({"version": 1, "contexts": manifest}))


if __name__ == "__main__":
    main()
