"""Export the contracts to `schemas/*.schema.json` for the C# bridge and any other consumer.

Run `uv run playtest-export-schemas` after changing a contract. `tests/test_contracts.py` fails if
the committed schemas drift from the models.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import CONTRACTS

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas"
DIALECT = "https://json-schema.org/draft/2020-12/schema"


def render(name: str) -> str:
    schema = CONTRACTS[name].model_json_schema(mode="validation")
    schema = {"$schema": DIALECT, "$id": f"upa/{name}.schema.json", **schema}
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    SCHEMA_DIR.mkdir(exist_ok=True)
    for name in CONTRACTS:
        (SCHEMA_DIR / f"{name}.schema.json").write_text(render(name), encoding="utf-8", newline="\n")
    print(f"wrote {len(CONTRACTS)} schemas to {SCHEMA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
