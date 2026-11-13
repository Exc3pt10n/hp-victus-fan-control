import tempfile
import unittest
from pathlib import Path
from unittest import mock

from victus_fan import cli, updater


class BrokenConfigTest(unittest.TestCase):
    """Eine fehlerhafte config.toml darf Updater und Wiederherstellung nie blockieren -
    sonst kann nicht einmal die Version installiert werden, die den Fehler behebt."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "config.toml"
        self.config.write_text('[general]\nbackend = "kaputt"\ninterval = 0\n', encoding="utf-8")

    def test_update_still_runs(self):
        with mock.patch.object(updater, "setup_logging"), \
                mock.patch.object(updater, "installed", return_value={}), \
                mock.patch.object(updater, "check_and_update",
                                  return_value={"state": "current", "message": "Aktuell"}) as check:
            code = cli.main(["--config", str(self.config), "update", "--auto"])
        self.assertEqual(code, 0)
        check.assert_called_once()
        self.assertTrue(check.call_args.args[0]["enabled"])      # Standardwerte

    def test_defaults_with_error(self):
        cfg, error = cli.load_config_or_defaults(self.config)
        self.assertIsNotNone(error)
        self.assertEqual(cfg.general["interval"], 2.0)
        self.assertEqual(cfg.path, self.config)                  # Neuladen nach Korrektur möglich


if __name__ == "__main__":
    unittest.main()
