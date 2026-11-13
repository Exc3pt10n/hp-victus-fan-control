import tempfile
import tomllib
import unittest
from pathlib import Path

from victus_fan.config import CONFIG_VERSION, HARDWARE_TEST_VERSION, load_config
from victus_fan.migrate import migrate_config

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "victus_fan" / "config.example.toml"
LEGACY = ROOT / "tests" / "legacy_configs"


class MigrateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.toml"

    def migrate(self, text):
        self.path.write_text(text, encoding="utf-8")
        result = migrate_config(self.path, TEMPLATE)
        return result, load_config(self.path), self.path.read_text(encoding="utf-8")

    def test_every_earlier_version_becomes_current_and_valid(self):
        for old in sorted(LEGACY.glob("*.toml")):
            with self.subTest(old.name):
                result, cfg, text = self.migrate(old.read_text(encoding="utf-8"))
                self.assertTrue(result.changed)
                self.assertEqual(tomllib.loads(text)["config_version"], CONFIG_VERSION)
                self.assertNotEqual(cfg.general["backend"], "auto")
                self.assertIn("# Ort: C:\\ProgramData\\VictusFan\\config.toml", text)   # neue Vorlage
                self.assertEqual(self.path.with_name("config.toml.bak").read_text(encoding="utf-8"),
                                 old.read_text(encoding="utf-8"))

    def test_untested_old_template_values_are_not_taken_as_measurements(self):
        # Die erste Vorlage hatte max_level 55/57 und mode = "auto" - keine Messung.
        result, cfg, _ = self.migrate((LEGACY / "v1.0.toml").read_text(encoding="utf-8"))
        self.assertEqual((cfg.windows["max_level_cpu"], cfg.windows["max_level_gpu"]), (54, 52))
        self.assertEqual(cfg.windows["mode"], "level")
        self.assertEqual(cfg.windows["hardware_test"], 0)
        self.assertFalse(result.hardware_tested)

    def test_real_user_file_keeps_measurements_and_sensor(self):
        result, cfg, text = self.migrate((LEGACY / "user-2.7.toml").read_text(encoding="utf-8"))
        self.assertTrue(result.hardware_tested)
        self.assertEqual(cfg.windows["hardware_test"], HARDWARE_TEST_VERSION)
        self.assertEqual(cfg.windows["cpu_sensor"], "perf:\\_TZ.THRM")
        self.assertEqual((cfg.windows["max_level_cpu"], cfg.windows["max_level_gpu"]), (54, 52))
        self.assertIn("general.backend", result.dropped)
        self.assertNotIn("Nach Änderungen den Dienst neu starten", text)

    def test_own_values_and_profiles_are_kept_invalid_ones_dropped(self):
        result, cfg, _ = self.migrate(
            '[general]\nprofile = "office"\ncritical_temp = 88\ninterval = 0\n'
            '[update]\ncheck_only = true\nrepo = "x/y"\nbranch = "main"\n'
            '[profiles.office]\ncpu_curve = [[0, 0], [60, 0], [70, 40], [90, 100]]\n'
            '[profiles.balanced]\ncpu_curve = [[0, 25], [60, 40], [90, 100]]\n'
            '[profiles.kaputt]\ncpu_curve = [[50, 10], [40, 20]]\n')
        self.assertEqual(cfg.general["profile"], "office")
        self.assertEqual(cfg.general["critical_temp"], 88)
        self.assertEqual(cfg.general["interval"], 2.0)                 # ungültig -> Standard
        self.assertTrue(cfg.update["check_only"])
        self.assertEqual(cfg.profile("office").cpu_curve[2], (70, 40))
        self.assertEqual(cfg.profile("balanced").cpu_curve[0], (0, 25))
        self.assertNotIn("kaputt", cfg.raw["profiles"])
        for name in ("update.repo", "update.branch", "Profil kaputt (ungültige Kurve)"):
            self.assertIn(name, result.dropped)
        self.assertTrue(any(d.startswith("general.interval") for d in result.dropped))

    def test_unchanged_default_profiles_are_not_copied(self):
        from victus_fan.config import DEFAULT_PROFILES
        curve = DEFAULT_PROFILES["quiet"]
        _, _, text = self.migrate(f'[profiles.quiet]\ncpu_curve = {curve["cpu_curve"]}\n'
                                  f'gpu_curve = {curve["gpu_curve"]}\n')
        self.assertNotIn("[profiles.quiet]", text)

    def test_runs_only_once(self):
        self.migrate((LEGACY / "user-2.7.toml").read_text(encoding="utf-8"))
        before = self.path.read_text(encoding="utf-8")
        self.path.with_name("config.toml.bak").unlink()
        result = migrate_config(self.path, TEMPLATE)
        self.assertFalse(result.changed)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertFalse(self.path.with_name("config.toml.bak").exists())

    def test_unreadable_file_is_replaced_by_template_with_backup(self):
        result, cfg, _ = self.migrate("das ist [kein toml")
        self.assertTrue(result.changed)
        self.assertEqual(cfg.general["profile"], "balanced")
        self.assertEqual(self.path.with_name("config.toml.bak").read_text(encoding="utf-8"), "das ist [kein toml")

    def test_values_valid_alone_but_not_together(self):
        # release >= engage ist nur zusammen ungültig -> Hardware-Werte bleiben trotzdem
        result, cfg, _ = self.migrate('[toggle]\nengage_percent = 60.0\nrelease_percent = 70.0\n'
                                      '[windows]\nmax_level_cpu = 50  # vom Hardware-Test gesetzt\n')
        self.assertEqual(cfg.windows["max_level_cpu"], 50)
        self.assertEqual(cfg.toggle["engage_percent"], 80.0)
        self.assertTrue(any("passten nicht zusammen" in d for d in result.dropped))

    def test_template_itself_is_current(self):
        self.path.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertFalse(migrate_config(self.path, TEMPLATE).changed)


if __name__ == "__main__":
    unittest.main()
