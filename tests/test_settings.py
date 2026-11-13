import json
import tempfile
import unittest
from pathlib import Path

from victus_fan.config import DEFAULT_PROFILES, load_config, reset_settings, save_settings


class SaveSettingsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "settings.json"

    def stored(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_saves_values_and_curves(self):
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

    def test_only_differences_are_stored(self):
        save_settings(self.path, {"general": {"profile": "quiet", "interval": 2.0}})
        self.assertEqual(self.stored(), {"version": 1, "general": {"profile": "quiet"}})
        # Zurück auf den Standard -> verschwindet wieder aus der Datei
        save_settings(self.path, {"general": {"profile": "balanced"},
                                  "profiles.quiet": DEFAULT_PROFILES["quiet"]})
        self.assertEqual(self.stored(), {"version": 1})

    def test_invalid_change_leaves_file_untouched(self):
        save_settings(self.path, {"general": {"profile": "quiet"}})
        before = self.path.read_text(encoding="utf-8")
        for changes in ({"profiles.quiet": {"cpu_curve": [[50, 10], [40, 20]]}},
                        {"general": {"profile": "gibtsnicht"}}, {"general": {"interval": 0}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                save_settings(self.path, changes)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_measurements_are_stored_completely(self):
        save_settings(self.path, {"windows": {"mode": "level", "min_level": 0, "max_level_cpu": 54,
                                              "max_level_gpu": 52, "hardware_test": 1,
                                              "cpu_sensor": "perf:\\_TZ.THRM"}})
        windows = self.stored()["windows"]
        self.assertEqual(windows["max_level_cpu"], 54)            # gleich dem Standard, trotzdem gespeichert
        self.assertEqual(windows["cpu_sensor"], "perf:\\_TZ.THRM")

    def test_damaged_file_is_replaced_on_save(self):
        self.path.write_text("{kaputt", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(self.path)
        save_settings(self.path, {"general": {"profile": "quiet"}})
        self.assertEqual(load_config(self.path).general["profile"], "quiet")

    def test_reset_keeps_measurements(self):
        save_settings(self.path, {"general": {"profile": "quiet", "log_level": "DEBUG"},
                                  "windows": {"max_level_cpu": 50, "hardware_test": 1, "cpu_sensor": "acpi:x"},
                                  "profiles.balanced": {"cpu_curve": [[0, 30], [90, 100]]}})
        cfg = reset_settings(self.path)
        self.assertEqual(cfg.general["profile"], "balanced")
        self.assertEqual(cfg.general["log_level"], "INFO")
        self.assertEqual(cfg.raw["profiles"]["balanced"], DEFAULT_PROFILES["balanced"])
        self.assertEqual((cfg.windows["max_level_cpu"], cfg.windows["hardware_test"], cfg.windows["cpu_sensor"]),
                         (50, 1, "acpi:x"))

    def test_creates_missing_file(self):
        save_settings(self.path, {"general": {"battery_profile": "balanced"}})
        self.assertEqual(load_config(self.path).general["battery_profile"], "balanced")


if __name__ == "__main__":
    unittest.main()
