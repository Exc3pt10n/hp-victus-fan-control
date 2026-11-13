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
        self.path.write_text((ROOT / "victus_fan" / "config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")

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

    def test_comment_with_quotes_is_kept(self):
        save_settings(self.path, {"windows": {"cpu_sensor": "perf:\\_TZ.THRM"}})
        line = next(l for l in self.path.read_text(encoding="utf-8").splitlines() if l.startswith("cpu_sensor ="))
        self.assertTrue(line.endswith('# auto oder eine id aus "victus-fan sensors"'), line)
        self.assertEqual(load_config(self.path).windows["cpu_sensor"], "perf:\\_TZ.THRM")

    def test_file_is_always_formatted_the_same_way(self):
        # Unordentliche Datei: falsche Reihenfolge, eigene Abstände, unbekannte Einträge
        self.path.write_text('config_version = 3\n[windows]\nmax_level_gpu=50\n  mode =   "level"   #x\n'
                             '[general]\nprofile="quiet"\nunbekannt = 1\n'
                             '[profiles.office]\ncpu_curve=[[0,0],[60,0],[80,100]]\n', encoding="utf-8")
        save_settings(self.path, {"windows": {"cpu_sensor": "perf:\\_TZ.THRM"}})
        first = self.path.read_text(encoding="utf-8")
        self.assertTrue(first.startswith("# Konfiguration der Victus-Lüftersteuerung"))
        self.assertNotIn("unbekannt", first)
        self.assertIn('[profiles.office]\ncpu_curve = [[0, 0], [60, 0], [80, 100]]', first)
        cfg = load_config(self.path)
        self.assertEqual((cfg.general["profile"], cfg.windows["max_level_gpu"]), ("quiet", 50))
        for line in first.splitlines():
            code, _, comment = line.partition(" #")
            if "=" in code and comment and not line.startswith("#"):
                self.assertTrue(len(code) >= 29 or line.index(" #") == 29, line)   # Kommentare in Spalte 30
        # Gleiche Werte ergeben immer exakt dieselbe Datei
        save_settings(self.path, {"general": {"profile": "quiet"}})
        self.assertEqual(self.path.read_text(encoding="utf-8"), first)

    def test_hardware_test_values_are_formatted(self):
        save_settings(self.path, {"windows": {"mode": "level", "min_level": 0, "max_level_cpu": 54,
                                              "max_level_gpu": 52, "hardware_test": 1}})
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertIn("hardware_test = 1             # 1 = Hardware-Test ausgeführt (sonst Hinweis im Fenster)",
                      lines)
        self.assertFalse(any("vom Hardware-Test gesetzt" in l for l in lines))

    def test_saving_an_old_file_migrates_it_first(self):
        old = (ROOT / "tests" / "legacy_configs" / "user-2.7.toml").read_text(encoding="utf-8")
        self.path.write_text(old, encoding="utf-8")
        cfg = save_settings(self.path, {"general": {"critical_temp": 90.0}})
        self.assertEqual(cfg.general["critical_temp"], 90.0)
        self.assertEqual(cfg.windows["hardware_test"], 1)          # Vermerk des Hardware-Tests erhalten
        self.assertEqual(cfg.windows["cpu_sensor"], "perf:\\_TZ.THRM")
        self.assertNotIn('backend = "auto"', self.path.read_text(encoding="utf-8"))

    def test_creates_missing_file(self):
        self.path.unlink()
        save_settings(self.path, {"general": {"battery_profile": "balanced"}})
        self.assertEqual(load_config(self.path).general["battery_profile"], "balanced")


if __name__ == "__main__":
    unittest.main()
