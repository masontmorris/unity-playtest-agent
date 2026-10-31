"""RightNow System Zero: its HTTP transport and the question layout its served limits require.

System Zero answers named questions about a state with a probability for every option
(https://www.rightnowai.co/docs/system-zero, read 31 October 2026). Its request is the System One shape the
other S1 arms use (`model`, `state`, `questions`), so S1's context goes out unchanged as `state`. Two of its
limits decide how the choices are asked:

- A `choice` question takes 2 to 10 options in a text request and 2 to 26 when the request carries an
  attachment. `GET /v1/models` served these `choice_options` for `system-zero-2.0.0` on 31 October, matching
  the SDK 0.4.2 types; `CHOICE_OPTIONS` follows the served values.
- One request may ask up to 1,024 named questions, each billed on its own.

So when the offered choices fit one question, S1 gets one flat `choice` question: exactly the shape the
other arms send. When they do not, the same choices are laid out as two levels answered in the same call.
The root question names a group, or a choice that stands alone; each group of two or more has its own
question. A group is a run of consecutive choices of one family (actions, skills, then the core choices) in
the context's order, split into balanced chunks no larger than the limit. A run of one is offered at the
root as that choice itself.

The answer is decoded by one fixed rule: the root names a choice, or names a group whose own answer names
the choice. No code ranks, compares or picks. The root answer and the selected group's answer are S1's; the
other groups' answers are recorded and never used. A refused or invalid root answer, or a refused or
invalid answer in the selected group, leaves no choice: the turn is rejected and nothing is substituted.
The composed distribution, P(group) x P(choice | group), is recorded only, like every distribution.

The transport never retries. An HTTP error keeps the provider's error envelope (its code, retry flag and
request id, never a credential) so the trace says why a call failed.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

from playtest.core.context import canonical

ARM = "system-zero"
ARMS = frozenset({ARM})
# Pinned: on 31 October `GET /v1/models` listed the alias `system-zero` as `alias_for` this version. Each reply's
# `model` still records the version that answered.
MODEL = "system-zero-2.0.0"
KEY_ENV = "SYSTEM_ZERO_API_KEY"
PROVIDER = "rightnow"
URL = "https://api.rightnowai.co/v1/system-zero/decide"
TIMEOUT_S = 60
# Options per choice question, as served for system-zero-2.0.0 (GET /v1/models, 31 October): text, or with an attachment.
CHOICE_OPTIONS = {"text": 10, "media": 26}
MIN_OPTIONS = 2
ROOT_NOTE = (" The choices are asked in groups. Name the group that holds the one choice you would take, "
             "or a choice that stands alone.")
GROUP_NOTE = (" This question covers one group of the choices: choose exactly one id from it, the one you "
              "would take from this group.")
PROMPTS = {"root_note": ROOT_NOTE, "group_note": GROUP_NOTE}

Item = tuple[str, str, str]  # (choice id, label, kind)


def _family(kind: str) -> str:
    return kind if kind in ("action", "skill") else "core"


def _chunks(run: list[Item], limit: int) -> list[list[Item]]:
    """Split one run into the fewest chunks of at most `limit`, as equal in size as possible, in order."""
    count = math.ceil(len(run) / limit)
    size, extra = divmod(len(run), count)
    chunks, start = [], 0
    for index in range(count):
        end = start + size + (1 if index < extra else 0)
        chunks.append(run[start:end])
        start = end
    return chunks


def layout(name: str, instructions: str, items: Sequence[Item], *, media: bool) -> dict[str, dict[str, Any]]:
    """The questions that ask S1 to choose exactly one of `items`, within the served option limit.

    Raises `ValueError` when the choices do not fit two levels, or a group name would collide with a choice id.
    """
    limit = CHOICE_OPTIONS["media" if media else "text"]
    items = list(items)
    if len(items) <= limit:
        return {name: {"type": "choice", "instructions": instructions,
                       "criteria": {cid: label for cid, label, _ in items}}}
    runs: list[list[Item]] = []
    for item in items:
        if runs and _family(runs[-1][-1][2]) == _family(item[2]):
            runs[-1].append(item)
        else:
            runs.append([item])
    root: dict[str, str] = {}
    groups: dict[str, dict[str, str]] = {}
    offered = {cid for cid, _, _ in items}
    numbers: dict[str, int] = {}
    for run in runs:
        for chunk in _chunks(run, limit):
            if len(chunk) == 1:
                cid, label, _ = chunk[0]
                root[cid] = label
                continue
            family = _family(chunk[0][2])
            numbers[family] = numbers.get(family, 0) + 1
            group = f"{name}__{family}{numbers[family]}"
            if group in offered:
                raise ValueError(f"group name {group!r} collides with a choice id")
            # Ids only: each label is in the group's own question and in the state's `choices`, and the served
            # question-size limit is unread (G0).
            root[group] = "one of: " + ", ".join(cid for cid, _, _ in chunk)
            groups[group] = {cid: label for cid, label, _ in chunk}
    if not MIN_OPTIONS <= len(root) <= limit:
        raise ValueError(f"{len(items)} choices do not fit two levels of {limit} options")
    questions = {name: {"type": "choice", "instructions": instructions + ROOT_NOTE, "criteria": root}}
    for group, criteria in groups.items():
        questions[group] = {"type": "choice", "instructions": instructions + GROUP_NOTE, "criteria": criteria}
    return questions


def _answer(answers: Mapping[str, Any], question: str,
            options: set[str]) -> tuple[str | None, dict[str, float] | None, str | None]:
    """One question's answer: the option it named, its distribution when valid, and any problem."""
    answer = answers.get(question)
    if isinstance(answer, dict) and isinstance(answer.get("error"), dict):
        return None, None, f"{question} refused: {answer['error'].get('code')}"
    if not isinstance(answer, dict) or answer.get("type") != "choice" or answer.get("choice") not in options:
        return None, None, f"{question}: invalid or unoffered choice"
    probabilities = answer.get("probabilities")
    if probabilities is None:
        return answer["choice"], None, None
    if not isinstance(probabilities, dict) or set(probabilities) != options or any(
        isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or p < 0 or p > 1
        for p in probabilities.values()
    ) or abs(sum(probabilities.values()) - 1.0) > 0.01:
        return answer["choice"], None, f"{question}: invalid distribution"
    return answer["choice"], {key: float(value) for key, value in probabilities.items()}, None


def decode(name: str, questions: Mapping[str, Any], data: Mapping[str, Any] | None,
           offered: set[str]) -> tuple[str | None, dict[str, float] | None, str | None]:
    """Read the choice S1 named from a reply to `layout(name, ...)`'s questions, by the fixed rule above.

    Returns the same triple as `replay.parse_answer`: the choice (or `None`), the composed distribution when
    every answer carried a valid one, and the problems noted, joined.
    """
    answers = (data or {}).get("answers")
    if not isinstance(answers, dict):
        return None, None, "invalid or unoffered choice"
    root = questions[name]["criteria"]
    groups = {key: set(questions[key]["criteria"]) for key in root if key != name and key in questions}
    selected, root_distribution, problem = _answer(answers, name, set(root))
    if selected is None:
        return None, None, problem
    problems = [problem] if problem else []
    within: dict[str, dict[str, float] | None] = {}
    choice = selected
    for group, options in groups.items():
        picked, distribution, note = _answer(answers, group, options)
        within[group] = distribution
        if note:
            problems.append(note)
        if group == selected:
            choice = picked
    if choice is None:
        return None, None, "; ".join(problems)
    composed: dict[str, float] | None = None
    if root_distribution is not None and all(value is not None for value in within.values()):
        composed = {}
        for key, p in root_distribution.items():
            if key in groups:
                composed.update({cid: p * q for cid, q in (within[key] or {}).items()})
            else:
                composed[key] = p
        if set(composed) != offered:
            problems.append("the composed distribution does not cover the offered choices")
            composed = None
    return choice, composed, "; ".join(problems) or None


def send(arm: str, body: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None, float, str | None]:
    """POST one decision. Never retries; an HTTP error returns the provider's error envelope as the data."""
    if arm not in ARMS:
        raise ValueError(f"{arm!r} is not a System Zero arm")
    key = os.environ.get(KEY_ENV)
    if not key:
        raise ValueError(f"missing credential for {arm}")
    request = urllib.request.Request(URL, data=canonical(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        latency = (time.monotonic() - started) * 1000
        envelope = _json(exc.read())
        error = envelope.get("error") if isinstance(envelope, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        retryable = " (retryable)" if isinstance(error, dict) and error.get("retryable") is True else ""
        return exc.code, envelope if isinstance(envelope, dict) else None, latency, (
            f"HTTP error: {code}{retryable}" if code else "HTTP error")
    except (OSError, urllib.error.URLError):
        return None, None, (time.monotonic() - started) * 1000, "transport error"
    latency = (time.monotonic() - started) * 1000
    data = _json(raw)
    if data is None:
        return status, None, latency, "response was not JSON"
    return status, data if isinstance(data, dict) else None, latency, None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
