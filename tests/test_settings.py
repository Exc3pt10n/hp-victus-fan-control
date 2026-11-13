import tempfile
import unittest
from pathlib import Path

from victus_fan.config import load_config, save_settings

ROOT = Path(__file__).resolve().parent.parent


class SaveSettingsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.toml"
        self.path.write_text((ROOT / "config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")

    def test_saves_values_and_curves_keeping_comments(self):
        curve = [[0, 10], [45, 10], [60, 40], [80, 100]]
        save_settings(self.path, {
            "general": {"profile": "quiet", "critical_temp": 90.0},
            "update": {"check_only": True},
            "profiles.quiet": {"cpu_curve": curve, "gpu_curve": curve},
        })
        cfg = load_config(self.path)
        self.assertEqual(cfg.general["profile"], "quiet")
        self.assertEqual(cfg.general["critical_temp"], 90.0)
        self.assertTrue(cfg.update["check_only"])
        self.assertEqual(cfg.profile("quiet").cpu_curve, [(0, 10), (45, 10), (60, 40), (80, 100)])
        self.assertEqual(cfg.profile("balanced").cpu_curve[0], (0, 20))     # andere Profile unverändert
        self.assertIn("# Konfiguration der Victus-Lüftersteuerung", self.path.read_text(encoding="utf-8"))

    def test_invalid_change_leaves_file_untouched(self):
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            save_settings(self.path, {"profiles.quiet": {"cpu_curve": [[50, 10], [40, 20]]}})
        with self.assertRaises(ValueError):
            save_settings(self.path, {"general": {"profile": "gibtsnicht"}})
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_inline_comments_are_kept(self):
        save_settings(self.path, {"general": {"profile": "quiet"}})
        line = next(l for l in self.path.read_text(encoding="utf-8").splitlines() if l.startswith("profile ="))
        self.assertEqual(line, 'profile = "quiet"             # Profil im Netzbetrieb')

    def test_creates_missing_file(self):
        self.path.unlink()
        save_settings(self.path, {"general": {"battery_profile": "balanced"}})
        self.assertEqual(load_config(self.path).general["battery_profile"], "balanced")


if __name__ == "__main__":
    unittest.main()
