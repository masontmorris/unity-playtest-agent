"""Live runner: drive the production turn loop on a wired game from a preflight manifest.

The loop in `playtest.core` is game-agnostic; everything here is the host side that binds it to one
game: choices built from the manifest, the S1 provider adapter, the effect probe, the session driver
and the command line. See `docs/design/turn-loop.md`.
"""
