import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from victus_fan.backends.base import FanBackend
from victus_fan.config import Config, load_config
from victus_fan.service import FanService


class FakeBackend(FanBackend):
    name = "fake"

    def __init__(self, level=True, keepalive=None):
        self.supports_level = level
        self.supports_max = True
        self.keepalive = keepalive
        self.temp = 40.0
        self.calls = []

    def read_cpu_temp(self):
        return self.temp

    def read_gpu_temp(self):
        return None

    def on_battery(self):
        return False

    def set_percent(self, percent):
        self.calls.append(("level", percent))

    def set_max(self, enabled):
        self.calls.append(("max", enabled))

    def restore_auto(self):
        self.calls.append(("restore",))


class ServiceTest(unittest.TestCase):
    def test_level_mode_sends_only_changes_and_keepalive(self):
        b = FakeBackend(level=True, keepalive=60)
        s = FanService(Config(), b)
        with mock.patch("victus_fan.service.time.monotonic", side_effect=[0, 2, 70]):
            s.step(); s.step(); s.step()
        self.assertEqual(b.calls, [("level", 20), ("level", 20)])  # Start + Keepalive

    def test_toggle_mode_uses_max(self):
        b = FakeBackend(level=False)
        s = FanService(Config(), b)
        b.temp = 90.0
        s.step()
        self.assertEqual(b.calls[-1], ("max", True))

    def test_critical_in_level_mode_uses_max(self):
        b = FakeBackend(level=True)
        s = FanService(Config(), b)
        b.temp = 99.0
        s.step()
        self.assertEqual(b.calls[-1], ("max", True))

    def test_override_and_status_file(self):
        with tempfile.TemporaryDirectory() as d:
            override = Path(d) / "override.json"
            status = Path(d) / "status.json"
            override.write_text(json.dumps({"profile": "performance"}))
            s = FanService(Config(), FakeBackend(), status_file=status, override_file=override)
            s.step()
            data = json.loads(status.read_text())
            self.assertEqual(data["profile"], "performance")
            self.assertEqual(data["profile_reason"], "manuell")

    def test_dry_run_sets_nothing(self):
        b = FakeBackend()
        FanService(Config(), b, dry_run=True).step()
        self.assertEqual(b.calls, [])

    def test_modes_paused_and_max(self):
        with tempfile.TemporaryDirectory() as d:
            override = Path(d) / "override.json"
            b = FakeBackend()
            s = FanService(Config(), b, override_file=override)
            override.write_text(json.dumps({"mode": "max"}))
            self.assertEqual(s.step()["output"], "MAX")
            self.assertEqual(b.calls[-1], ("max", True))
            override.write_text(json.dumps({"mode": "paused"}))
            self.assertEqual(s.step()["output"], "BIOS")
            self.assertEqual(b.calls[-1], ("restore",))
            n = len(b.calls)
            s.step()
            self.assertEqual(len(b.calls), n)  # pausiert: nichts senden
            override.write_text(json.dumps({"mode": "normal"}))
            b.reconfigure = lambda settings: b.calls.append(("reconfigure",))
            s.step()
            self.assertIn(("reconfigure",), b.calls)
            self.assertEqual(b.calls[-1][0], "level")

    def test_config_reload(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "config.toml"
            cfg_path.write_text("[general]\ncritical_temp = 92.0\n")
            s = FanService(load_config(cfg_path), FakeBackend())
            cfg_path.write_text("[general]\ncritical_temp = 80.0\n")
            os.utime(cfg_path, (1, 1))
            s.step()
            self.assertEqual(s.controller.critical_temp, 80.0)
            cfg_path.write_text("[general]\nprofile = 'gibtsnicht'\n")
            os.utime(cfg_path, (2, 2))
            s.step()  # fehlerhafte Konfiguration -> alte bleibt
            self.assertEqual(s.controller.critical_temp, 80.0)


class EffectTest(unittest.TestCase):
    def _service(self, rpm):
        b = FakeBackend(level=True)
        b.temp = 65.0
        b.read_fan_rpm = lambda: rpm
        b.expected_rpm = lambda p: [int(55 * p), int(57 * p)]
        return FanService(Config(), b)

    def _run(self, s, seconds):
        status = None
        with mock.patch("victus_fan.service.time.monotonic", side_effect=list(range(0, seconds, 2))):
            for _ in range(0, seconds, 2):
                status = s.step()
        return status

    def test_warns_when_fans_ignore_command(self):
        status = self._run(self._service([2700, 0]), 80)
        self.assertIsNotNone(status["warning"])

    def test_no_warning_when_fans_follow(self):
        status = self._run(self._service([2475, 2565]), 80)
        self.assertIsNone(status["warning"])


class ConfigUpdateTest(unittest.TestCase):
    def test_update_keeps_comments_and_adds_keys(self):
        from victus_fan.config import update_config_values
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text(Path("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
            update_config_values(p, "windows", {"mode": "toggle", "max_level_cpu": 49}, note="Test")
            cfg = load_config(p)
            self.assertEqual(cfg.windows["mode"], "toggle")
            self.assertEqual(cfg.windows["max_level_cpu"], 49)
            self.assertIn("# Eigene Profile", p.read_text(encoding="utf-8"))
            update_config_values(p, "neu", {"x": 1})
            self.assertIn("[neu]", p.read_text(encoding="utf-8"))


class RearmTest(unittest.TestCase):
    """Fallen die Lüfter in die BIOS-Automatik zurück, wird derselbe
    Leistungsmodus erneut gesetzt - kein Wechsel zu einem anderen Modus."""

    def make(self, follows_after_rearm: bool):
        b = FakeBackend(level=True)
        b.temp = 65.0
        b.rearms = 0
        b.following = False

        def rearm():
            b.rearms += 1
            b.following = follows_after_rearm

        b.rearm = rearm
        b.expected_rpm = lambda p: [int(54 * p), int(52 * p)]
        b.read_fan_rpm = lambda: b.expected_rpm(b.calls[-1][1]) if b.following and b.calls else [2700, 0]
        return b, FanService(Config(), b)

    def run_for(self, s, seconds):
        status = None
        with mock.patch("victus_fan.service.time.monotonic", side_effect=list(range(0, seconds, 2))):
            for _ in range(0, seconds, 2):
                status = s.step()
        return status

    def test_rearm_fixes_it(self):
        b, s = self.make(follows_after_rearm=True)
        status = self.run_for(s, 200)
        self.assertEqual(b.rearms, 1)
        self.assertIsNone(status["warning"])

    def test_keeps_trying_and_warns(self):
        b, s = self.make(follows_after_rearm=False)
        status = self.run_for(s, 400)
        self.assertGreater(b.rearms, 3)                   # gibt nicht auf (1x pro Minute)
        self.assertLessEqual(b.rearms, 400 // 60 + 1)
        self.assertIsNotNone(status["warning"])

    def test_small_changes_do_not_delay_check(self):
        b, s = self.make(follows_after_rearm=True)
        temps = iter([65.0, 66.0] * 200)                  # ständige kleine Schwankung
        b.read_cpu_temp = lambda: next(temps)
        self.run_for(s, 60)
        self.assertGreaterEqual(b.rearms, 1)              # Abweichung trotzdem erkannt

    def test_auto_chosen_mode_is_reset_once(self):
        from victus_fan.config import reset_auto_chosen_fan_mode
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text('[windows]\nfan_mode = "performance"  # automatisch gewählt\n', encoding="utf-8")
            self.assertTrue(reset_auto_chosen_fan_mode(p))
            self.assertEqual(load_config(p).windows["fan_mode"], "default")
            self.assertFalse(reset_auto_chosen_fan_mode(p))      # nur einmal
            p.write_text('[windows]\nfan_mode = "performance"\n', encoding="utf-8")
            self.assertFalse(reset_auto_chosen_fan_mode(p))      # eigene Wahl bleibt


class FanModeTest(unittest.TestCase):
    """Der BIOS-Leistungsmodus kommt nur aus der Konfiguration ([windows] fan_mode)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.override = Path(self.tmp.name) / "override.json"
        self.b = FakeBackend(level=True)
        self.b.applied = []
        self.b.reconfigure = lambda settings: self.b.applied.append(settings.get("fan_mode"))
        self.s = FanService(Config(), self.b, override_file=self.override)

    def test_old_power_mode_choice_is_ignored(self):
        self.override.write_text(json.dumps({"power_mode": "performance"}))   # aus Version 2.1
        status = self.s.step()
        self.assertEqual(self.b.applied, [])            # nichts umgestellt
        self.assertEqual(status["fan_mode"], "default")

    def test_resume_after_pause_sets_mode_again(self):
        self.override.write_text(json.dumps({"mode": "paused"}))
        self.s.step()
        self.assertEqual(self.b.calls[-1], ("restore",))
        self.override.write_text(json.dumps({"mode": "normal"}))
        self.s.step()
        self.assertEqual(self.b.applied, ["default"])

if __name__ == "__main__":
    unittest.main()
