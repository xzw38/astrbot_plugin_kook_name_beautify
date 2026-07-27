"""Synchronize plugins.json from metadata.yaml using only the standard library."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
METADATA_PATH = ROOT / "metadata.yaml"
MARKETPLACE_PATH = ROOT / "plugins.json"


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def read_metadata() -> dict[str, object]:
    result: dict[str, object] = {}
    active_list = ""
    for raw_line in METADATA_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") and active_list:
            values = result.setdefault(active_list, [])
            assert isinstance(values, list)
            values.append(_scalar(line[4:]))
            continue
        active_list = ""
        if ":" not in line or line.startswith(" "):
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if value:
            result[key] = _scalar(value)
        else:
            result[key] = []
            active_list = key
    return result


def build_marketplace(metadata: dict[str, object], existing: dict[str, object]) -> dict[str, object]:
    name = str(metadata["name"])
    old_entry = existing.get(name, {}) if isinstance(existing, dict) else {}
    if not isinstance(old_entry, dict):
        old_entry = {}
    entry = {
        "display_name": metadata["display_name"],
        "desc": metadata["desc"],
        "short_desc": metadata["short_desc"],
        "version": metadata["version"],
        "author": metadata["author"],
        "repo": metadata["repo"],
        "tags": old_entry.get("tags", ["KOOK", "频道管理", "名称美化", "AI 工具"]),
        "category": old_entry.get("category", "management"),
    }
    return {name: entry}


def render_marketplace() -> str:
    metadata = read_metadata()
    existing = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8")) if MARKETPLACE_PATH.exists() else {}
    return json.dumps(build_marketplace(metadata, existing), ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render_marketplace()
    current = MARKETPLACE_PATH.read_text(encoding="utf-8") if MARKETPLACE_PATH.exists() else ""
    if args.check:
        if current != expected:
            print("plugins.json is out of sync; run: python tools/sync_marketplace.py")
            return 1
        print("plugins.json is synchronized")
        return 0
    MARKETPLACE_PATH.write_text(expected, encoding="utf-8", newline="\n")
    print("updated plugins.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
