import re
import tomllib
import unittest
from pathlib import Path

import victus_fan

ROOT = Path(__file__).resolve().parent.parent


class VersionTest(unittest.TestCase):
    """Die Version steht nur in victus_fan/__init__.py; pyproject.toml liest sie von dort."""

    def test_single_source(self):
        with (ROOT / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
        self.assertNotIn("version", data["project"])
        self.assertIn("version", data["project"]["dynamic"])
        self.assertEqual(data["tool"]["setuptools"]["dynamic"]["version"], {"attr": "victus_fan.__version__"})
        self.assertRegex(victus_fan.__version__, r"^\d+\.\d+\.\d+$")
        code = re.search(r'__version__ = "(.+?)"', (ROOT / "victus_fan" / "__init__.py").read_text()).group(1)
        self.assertEqual(code, victus_fan.__version__)   # so liest es auch der Release-Workflow


if __name__ == "__main__":
    unittest.main()
