"""A local dashboard for playtest runs: start a hunt, follow a live session, and read what it filed.

It reads what runs already write under `runs/` (traces, frames, ledgers, hunt reports) and starts hunts through
`playtest-hunt`. It never touches a running session: there is no endpoint that chooses, pauses, steers or asks for
help, so S1 alone decides what is played. Missing evidence is shown as unknown, never filled in.
"""
