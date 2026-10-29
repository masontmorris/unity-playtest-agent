"""Reader values in the observation: `public.<alias>` per installed reader, `null` plus a reader error when unread.

A reader is a named game value the manifest declares. A `ReaderPort` reads them all once before a turn's
observation (so the reading never ages the observation an action is bound to), and `install` puts the
values into that observation. The result is the observation S1, the trace and every later step see.

A value that could not be read is `None` and has a `reader_errors` entry saying why; it is never zero, false
or an earlier value. An alias the bridge already publishes under `public` is not overwritten: the bridge's
value stays and the reader gets an error entry.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from playtest.contracts.bridge import Observation, ReaderError

from .ports import ReaderSample


def unread(aliases: Iterable[str], problem: str) -> ReaderSample:
    """A sample in which nothing could be read: every alias is unknown, and `problem` says why."""
    names = list(aliases)
    return ReaderSample(values={alias: None for alias in names}, errors={alias: problem for alias in names},
                        problem=problem)


def install(observation: Observation, sample: ReaderSample) -> Observation:
    """The observation with every reader in the sample under `public` and an error entry per unread one."""
    public: dict[str, Any] = dict(observation.public)
    errors: list[ReaderError] = list(observation.reader_errors)
    for alias in sample.values:
        if alias in observation.public:
            errors.append(ReaderError(
                reader=alias, error="the bridge already publishes this name under public; the reader was not installed"))
            continue
        value = sample.values.get(alias)
        public[alias] = value
        reason = sample.errors.get(alias)
        if reason is None and value is None:
            reason = "the reader returned no value"
        if reason is not None:
            errors.append(ReaderError(reader=alias, error=reason))
    return observation.model_copy(update={"public": public, "reader_errors": errors})


def readings(public: Mapping[str, Any], aliases: Iterable[str]) -> dict[str, Any]:
    """The values of `aliases` in one observation's `public`; a missing one reads `None`."""
    return {alias: public.get(alias) for alias in aliases}
