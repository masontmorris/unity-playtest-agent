"""Wake a scaled-to-zero Modal S1 server before a run starts.

`mammoth-1-modal` runs on our own Modal app (scripts/modal/mammoth_flide.py), which scales to zero when
idle. A cold start loads the weights from a Volume, starts Flide and warms its planners: about 90 s once
the image and weights are cached, longer on the first start ever. `wake` asks the server's `/health`
until it answers ready, so that time is spent before the game is touched instead of on the first turn.

This is readiness, not a decision: it sends no S1 request and selects nothing. It reads the arm's
credentials from the environment by name and never prints them.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from playtest.bench import replay

WAKE_TIMEOUT_S = 1200.0  # the first start ever downloads the weights and builds kernels
POLL_S = 5.0
REQUEST_TIMEOUT_S = 60.0  # Modal may hold a request while the container starts


class WakeError(RuntimeError):
    """The server refused the credentials, has no health route, or did not become ready in time."""


def health_url(arm: str) -> tuple[str, dict[str, str]]:
    url, headers = replay.endpoint(arm)
    if url is None:
        raise WakeError(f"missing endpoint or credential for {arm}")
    return url.removesuffix("/v1/systemone") + "/health", headers


def wake(arm: str, *, timeout_s: float = WAKE_TIMEOUT_S, poll_s: float = POLL_S,
         urlopen: Callable[..., Any] = urllib.request.urlopen, monotonic: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] | None = None) -> float:
    """Block until the arm's server reports ready; return the seconds that took.

    A 503, a timeout or a dropped connection means the server is still starting. A 401, 403 or 404 is
    final: the token is wrong or the URL is not this server.
    """
    url, headers = health_url(arm)
    say = log or (lambda text: print(text, file=sys.stderr, flush=True))
    started = monotonic()
    announced = False
    while True:
        try:
            with urlopen(urllib.request.Request(url, headers=headers), timeout=REQUEST_TIMEOUT_S) as response:
                body = json.loads(response.read() or b"{}")
                if response.status == 200 and isinstance(body, dict) and body.get("status") == "ok":
                    return monotonic() - started
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise WakeError(f"{arm} server answered HTTP {exc.code}; check its URL and proxy token") from None
        except (OSError, ValueError):
            pass  # still starting: refused, reset, timed out, or not JSON yet
        elapsed = monotonic() - started
        if elapsed >= timeout_s:
            raise WakeError(f"{arm} server was not ready after {elapsed:.0f} s")
        if not announced:
            say(f"waking the {arm} server (a cold start takes about 90 s, longer the first time)")
            announced = True
        sleep(poll_s)
