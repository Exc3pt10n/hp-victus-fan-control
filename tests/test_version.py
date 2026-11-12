import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class VersionTest(unittest.TestCase):
    """Der Release-Workflow taggt v<Version> - beide Angaben müssen übereinstimmen."""

    def test_versions_match(self):
        with (ROOT / "pyproject.toml").open("rb") as fh:
            project = tomllib.load(fh)["project"]["version"]
        code = re.search(r'__version__ = "(.+?)"', (ROOT / "victus_fan" / "__init__.py").read_text()).group(1)
        self.assertEqual(project, code)
        self.assertRegex(project, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
