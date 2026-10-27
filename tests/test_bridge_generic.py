"""The bridge is generic.

The bridge exposes verbs and facts; it never knows a game. No game title, genre, character,
object, control or field vocabulary may appear in the package code, tests or manifest:
identifiers, strings or comments. Game knowledge lives only in the preflight-generated manifest,
and the test games are external subjects. This is the bridge-side companion to law 8's
name-blind core lint.
"""

from __future__ import annotations

import re
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1] / "bridge"
ROOTS = (
    BRIDGE / "Runtime", BRIDGE / "Editor", BRIDGE / "Tests" / "EditMode",
    BRIDGE / "Tests" / "PlayMode", BRIDGE / "Tests" / "Fixtures",
)

BANNED = frozenset(
    """
    fpskit dragonblaze relay station street
    player players enemy enemies npc boss weapon weapons gun guns ammo bullet bullets projectile
    health hp lives coin coins gem gems inventory pickup pickups loot door doors
    checkpoint checkpoints lap laps ring rings racing racer car cars vehicle steer steering throttle brake braking
    jump jumps crouch sprint dash shoot shooting aim respawn
    shooter platformer fps rpg dragon
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
    for path in sorted(root.rglob("*.cs")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # The stage name is protocol vocabulary. Permit it only in the declarations and
            # protocol tests that use that literal; a gameplay field or an arbitrary string
            # with the same name remains subject to the lint.
            stripped = line.strip()
            if path.name == "BridgeProtocol.cs" and stripped == '"inventory", "discovery", "transforms", "fields",':
                line = line.replace('"inventory"', '""', 1)
            elif path.name == "EditorInspectionSource.cs" and stripped == 'if (stage == "inventory")':
                line = line.replace('"inventory"', '""', 1)
            elif path.name == "UnityInspectionSurveyTests.cs":
                for form in ('source.Inspect("inventory")', 'Stage = "inventory",'):
                    line = line.replace(form, form.replace('"inventory"', '""'), 1)
            elif path.name == "BridgeInspectTests.cs":
                for form in ('Inspect("inventory",', 'Is.EqualTo("inventory")', 'new[] { "inventory" }'):
                    line = line.replace(form, form.replace('"inventory"', '""'), 1)
            hits = words(line) & BANNED
            if hits:
                found.append(f"{path.relative_to(root.parent)}:{n}: {sorted(hits)}")
    return found


def test_bridge_is_generic():
    assert all(root.is_dir() for root in ROOTS), "the bridge package layout is missing"
    found = [v for root in ROOTS for v in violations(root)]
    assert not found, "game-specific words in the generic bridge:\n" + "\n".join(found)


def test_package_manifest_is_generic():
    manifest = BRIDGE / "package.json"
    assert manifest.is_file()
    hits = words(manifest.read_text(encoding="utf-8")) & BANNED
    assert not hits, f"game-specific words in bridge/package.json: {sorted(hits)}"


def test_lint_catches_planted_words(tmp_path):
    pkg = tmp_path / "Runtime"
    pkg.mkdir()
    (pkg / "x.cs").write_text("// FPSKit player health\nvar genericEntityPos = 0;\n", encoding="utf-8")
    (pkg / "y.cs").write_text("var genericEntityPos = 0;\n", encoding="utf-8")
    found = violations(pkg)
    assert len(found) == 1 and "player" in found[0] and "health" in found[0]


def test_protocol_stage_literals_do_not_hide_identifiers(tmp_path):
    pkg = tmp_path / "Runtime"
    pkg.mkdir()
    (pkg / "BridgeProtocol.cs").write_text(
        '"inventory", "discovery", "transforms", "fields",\nvar inventory = 1;\nvar description = "inventory";\n',
        encoding="utf-8",
    )
    found = violations(pkg)
    assert len(found) == 2 and all("inventory" in item for item in found)
