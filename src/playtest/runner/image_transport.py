"""How one S1 arm carries a game-view JPEG beside its state, written once for every request body.

Day 1 exercised the Baseten and d1.1 combinations. d1-ultra took the same `images` array in its
30 October probe: a 640x480 JPEG added 450 input tokens, the Day 1 image cost scaled by pixel count.

A Liquid arm sends the image in the body's own `images` array beside a plain `state`, and that is the
only transport whose body stays reconstructible from its own bytes. A Baseten arm wraps `state` in a
message and puts the image beside it. Nothing here decides whether a view is fresh: the caller does
(`playtest.runner.game_view`), and this module only encodes and places it.
"""

from __future__ import annotations

import base64
from typing import Any

IMAGE_TRANSPORT = {"mammoth-1": "messages", "elephant-1": "messages", "liquid-d1.1": "images",
                   "liquid-d1-ultra": "images", "meta-muse-spark-s1": "meta",
                   # Our Modal server takes the Liquid transport and rewrites it for Flide exactly as the
                   # 2 November image-correctness check verified (scripts/modal/mammoth_flide.py).
                   "mammoth-1-modal": "images",
                   # The local shim decodes each data URL into the picture d1-2B's `system_one` takes as media
                   # (scripts/s1/d1_local_server.py). Exercised against a stand-in model only, not yet the real one.
                   "d1-2b-local": "images", "modal-d1-2b": "images",
                   # The local d1-2B server decodes `images[0]` as the one picture of the state, as Liquid does.
                   "local-d1-2b": "images"}
IMAGE_TOKENS = {"messages": 1000, "images": 1400, "meta": 1400}  # fallback estimate


def image_url(image: bytes) -> str:
    """One JPEG as the data URL every arm here takes: base64 encoded in exactly one place."""
    return "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")


def attach_images(body: dict[str, Any], arm: str, image: bytes | None) -> dict[str, Any]:
    """Put one image on a request as `images[0]`, the transport a Liquid arm verifies.

    Only an arm whose verified transport is the plain `images` array can carry an image this way: the
    other arms put it inside a message, which would replace the `state` this body was built with. Those
    arms fail here rather than have their context silently swapped.
    """
    if image is None:
        return body
    if IMAGE_TRANSPORT.get(arm) != "images":
        raise ValueError(f"S1 arm {arm!r} does not take a plain images array; it sends the image inside "
                         f"a message, which a plain state body cannot carry")
    body["images"] = [image_url(image)]
    return body
