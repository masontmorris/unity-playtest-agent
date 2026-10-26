"""Law 8: the core is name-blind.

No game field, object, control or genre word may appear anywhere in `src/playtest/core/` or in the
role prompts and validation under `src/playtest/roles/`: identifiers, strings or comments. Game
knowledge lives only in the preflight-generated manifest. Add a word here when a new test game or
genre brings one in.
"""

from __future__ import annotations

import re
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "src" / "playtest" / "core"
NAME_BLIND = (CORE, CORE.parent / "roles")

BANNED = frozenset(
    """
    fpskit dragonblaze relay station street
    player players enemy enemies npc boss weapon weapons gun guns ammo bullet bullets projectile
    health hp lives coin coins gem gems inventory pickup pickups loot door doors
    checkpoint checkpoints lap laps ring rings racing racer race car cars vehicle steer steering throttle brake braking
    jump jumps crouch sprint dash shoot shooting aim reload respawn
    shooter platformer fps rpg dragon
    north south east west
    """.split()
)


def words(text: str) -> set[str]:
    """Split identifiers on case and underscores, so `playerPos` and `PLAYER_POS` both yield 'player'."""
    out: set[str] = set()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9]*", text):
        for part in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", token):
            out.add(part.lower())
    return out


def violations(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            hits = words(line) & BANNED
            if hits:
                found.append(f"{path.relative_to(root.parent)}:{n}: {sorted(hits)}")
    return found


def test_core_is_name_blind():
    assert all(root.is_dir() for root in NAME_BLIND)
    found = [v for root in NAME_BLIND for v in violations(root)]
    assert not found, "game-specific words in core:\n" + "\n".join(found)


def test_lint_catches_planted_words(tmp_path):
    pkg = tmp_path / "core"
    pkg.mkdir()
    (pkg / "x.py").write_text("def f(obs):\n    return obs['playerPos']  # STEER_LEFT\n", encoding="utf-8")
    (pkg / "y.py").write_text("ok = 'generic_entity_pos'\n", encoding="utf-8")
    found = violations(pkg)
    assert len(found) == 1 and "player" in found[0] and "steer" in found[0]
