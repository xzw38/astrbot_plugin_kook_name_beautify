import json
import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def metadata_scalar(key: str) -> str:
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, flags=re.MULTILINE)
    if not match:
        raise AssertionError(f"missing metadata key: {key}")
    return match.group(1).strip().strip('"\'')


class MetadataSyncTests(unittest.TestCase):
    def test_marketplace_file_is_generated_from_metadata(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "sync_marketplace.py"), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_main_metadata_and_marketplace_versions_match(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        version_match = re.search(r'^__version__\s*=\s*"([^"]+)"', main_text, flags=re.MULTILINE)
        self.assertIsNotNone(version_match)
        market = json.loads((ROOT / "plugins.json").read_text(encoding="utf-8"))
        name = metadata_scalar("name")
        entry = market[name]
        self.assertEqual(version_match.group(1), metadata_scalar("version"))
        self.assertEqual(entry["version"], metadata_scalar("version"))
        self.assertEqual(entry["repo"], metadata_scalar("repo"))
        self.assertEqual(entry["display_name"], metadata_scalar("display_name"))

    def test_llm_tool_has_required_docstring_argument_schema(self):
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('@filter.llm_tool(name="kook_beautify_channels")', main_text)
        self.assertIn("Args:\n            instruction(string):", main_text)


if __name__ == "__main__":
    unittest.main()
