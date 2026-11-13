import json
import tempfile
import unittest
from pathlib import Path

from victus_fan.config import DEFAULT_PROFILES, HARDWARE_TEST_VERSION, load_config
from victus_fan.migrate import migrate_config

LEGACY = Path(__file__).resolve().parent / "legacy_configs"


class MigrateTest(unittest.TestCase):
    """Einmalige Übernahme der config.toml (bis Version 2.8) in settings.json."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.settings, self.legacy = self.dir / "settings.json", self.dir / "config.toml"

    def migrate(self, text):
        self.legacy.write_text(text, encoding="utf-8")
        result = migrate_config(self.settings, self.legacy)
        return result, load_config(self.settings)

    def test_every_earlier_version_is_taken_over(self):
        for old in sorted(LEGACY.glob("*.toml")):
            with self.subTest(old.name):
                self.settings.unlink(missing_ok=True)
                result, cfg = self.migrate(old.read_text(encoding="utf-8"))
                self.assertTrue(result.changed)
                self.assertFalse(self.legacy.exists())
                self.assertEqual(self.dir.joinpath("config.toml.bak").read_text(encoding="utf-8"),
                                 old.read_text(encoding="utf-8"))
                self.assertEqual(cfg.general["backend"], "windows")

    def test_untested_old_template_values_are_not_taken_as_measurements(self):
        # Die erste Vorlage hatte max_level 55/57 und mode = "auto" - keine Messung.
        result, cfg = self.migrate((LEGACY / "v1.0.toml").read_text(encoding="utf-8"))
        self.assertEqual((cfg.windows["max_level_cpu"], cfg.windows["max_level_gpu"]), (54, 52))
        self.assertEqual(cfg.windows["mode"], "level")
        self.assertEqual(cfg.windows["hardware_test"], 0)
        self.assertFalse(result.hardware_tested)

    def test_real_user_file_keeps_measurements_and_sensor(self):
        result, cfg = self.migrate((LEGACY / "user-2.7.toml").read_text(encoding="utf-8"))
        self.assertTrue(result.hardware_tested)
        self.assertEqual(cfg.windows["hardware_test"], HARDWARE_TEST_VERSION)
        self.assertEqual(cfg.windows["cpu_sensor"], "perf:\\_TZ.THRM")
        self.assertEqual((cfg.windows["max_level_cpu"], cfg.windows["max_level_gpu"]), (54, 52))
        stored = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(stored["windows"]["max_level_cpu"], 54)

    def test_own_values_and_curves_kept_rest_dropped(self):
        result, cfg = self.migrate(
            '[general]\nprofile = "quiet"\ncritical_temp = 88\ninterval = 0\nlog_level = "DEBUG"\n'
            '[update]\ncheck_only = true\nrepo = "x/y"\nbranch = "main"\n'
            '[windows]\nkeepalive = 90.0\n'
            '[profiles.office]\ncpu_curve = [[0, 0], [60, 0], [70, 40], [90, 100]]\n'
            '[profiles.balanced]\ncpu_curve = [[0, 25], [60, 40], [90, 100]]\n'
            '[profiles.performance]\ncpu_curve = [[50, 10], [40, 20]]\n')
        self.assertEqual(cfg.general["profile"], "quiet")
        self.assertEqual(cfg.general["critical_temp"], 88)
        self.assertEqual(cfg.general["log_level"], "DEBUG")
        self.assertEqual(cfg.general["interval"], 2.0)                 # ungültig -> Standard
        self.assertEqual(cfg.windows["keepalive"], 60.0)               # fest im Programm
        self.assertTrue(cfg.update["check_only"])
        self.assertEqual(cfg.profile("balanced").cpu_curve[0], (0, 25))
        self.assertEqual(cfg.raw["profiles"]["performance"], DEFAULT_PROFILES["performance"])
        for name in ("update.repo", "update.branch", "eigenes Profil office", "Profil performance (ungültige Kurve)"):
            self.assertIn(name, result.dropped)
        self.assertTrue(any(d.startswith("general.interval") for d in result.dropped))

    def test_profile_choice_that_no_longer_exists(self):
        result, cfg = self.migrate('[general]\nprofile = "office"\n[profiles.office]\ncpu_curve = [[0, 0], [90, 100]]\n'
                                   '[windows]\nmax_level_cpu = 50  # vom Hardware-Test gesetzt\n')
        self.assertEqual(cfg.general["profile"], "balanced")
        self.assertEqual(cfg.windows["max_level_cpu"], 50)             # Messwert bleibt trotzdem

    def test_runs_only_once(self):
        self.migrate((LEGACY / "user-2.7.toml").read_text(encoding="utf-8"))
        before = self.settings.read_text(encoding="utf-8")
        self.legacy.write_text('[general]\nprofile = "quiet"\n', encoding="utf-8")
        self.assertFalse(migrate_config(self.settings, self.legacy).changed)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), before)

    def test_fresh_install_creates_defaults(self):
        result = migrate_config(self.settings, self.legacy)
        self.assertTrue(result.created)
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), {"version": 1})

    def test_unreadable_file(self):
        result, cfg = self.migrate("das ist [kein toml")
        self.assertTrue(result.changed)
        self.assertEqual(cfg.general["profile"], "balanced")
        self.assertEqual(self.dir.joinpath("config.toml.bak").read_text(encoding="utf-8"), "das ist [kein toml")


if __name__ == "__main__":
    unittest.main()
