import unittest

import tempfile
from pathlib import Path

from victus_fan.config import DEFAULTS, TEMPLATE as ROOT_TEMPLATE, Config, Profile, load_config
from victus_fan.controller import SAFETY_FLOOR, FanController, Reading, ToggleAdapter, interpolate


def make(**overrides):
    control = dict(DEFAULTS["control"], **overrides)
    return FanController(control, critical_temp=92)


class InterpolateTest(unittest.TestCase):
    curve = [(40, 0), (60, 50), (80, 100)]

    def test_bounds_and_middle(self):
        self.assertEqual(interpolate(self.curve, 20), 0)
        self.assertEqual(interpolate(self.curve, 50), 25)
        self.assertEqual(interpolate(self.curve, 70), 75)
        self.assertEqual(interpolate(self.curve, 99), 100)


class ControllerTest(unittest.TestCase):
    def setUp(self):
        # Feste Testkurve - unabhängig von den Standardprofilen.
        self.profile = Profile(
            name="test",
            cpu_curve=[(0, 0), (48, 0), (55, 25), (65, 45), (75, 70), (85, 100)],
            gpu_curve=[(0, 0), (45, 0), (52, 25), (62, 45), (72, 70), (82, 100)],
        )

    def test_first_reading_jumps_to_demand(self):
        c = make()
        d = c.update(Reading(65.0), self.profile, now=0)
        self.assertAlmostEqual(d.percent, 45)

    def test_ramp_up_is_rate_limited(self):
        c = make(ramp_up_rate=10)
        c.update(Reading(40.0), self.profile, now=0)
        d = c.update(Reading(85.0), self.profile, now=1)
        self.assertLessEqual(d.percent, 20 + 1e-6)
        self.assertGreater(d.percent, 0)

    def test_no_spin_down_before_delay(self):
        c = make(spin_down_delay=20)
        c.update(Reading(75.0), self.profile, now=0)
        before = c.percent
        for t in range(1, 15):
            d = c.update(Reading(40.0), self.profile, now=t)
        self.assertEqual(d.percent, before)

    def test_spins_down_eventually(self):
        c = make(spin_down_delay=5)
        c.update(Reading(75.0), self.profile, now=0)
        for t in range(1, 300):
            d = c.update(Reading(40.0), self.profile, now=t)
        self.assertEqual(d.percent, 0)

    def test_hysteresis_holds_on_small_drop(self):
        c = make(spin_down_delay=0, smoothing_down=1.0)
        c.update(Reading(65.0), self.profile, now=0)
        before = c.percent
        for t in range(1, 60):
            d = c.update(Reading(63.0), self.profile, now=t)  # 2 °C < Hysterese 4 °C
        self.assertEqual(d.percent, before)

    def test_short_spike_is_damped(self):
        c = make(smoothing_up=0.3)
        c.update(Reading(45.0), self.profile, now=0)
        d = c.update(Reading(80.0), self.profile, now=2)
        self.assertLess(d.demand, 50)

    def test_critical_forces_max(self):
        c = make()
        d = c.update(Reading(95.0), self.profile, now=0)
        self.assertTrue(d.critical)
        self.assertEqual(d.percent, 100)

    def test_no_sensor_data_is_treated_as_critical(self):
        c = make()
        d = c.update(Reading(None, None), self.profile, now=0)
        self.assertTrue(d.critical)

    def test_gpu_drives_demand(self):
        c = make()
        d = c.update(Reading(45.0, 72.0), self.profile, now=0)
        self.assertAlmostEqual(d.percent, 70)

    def test_min_running_percent(self):
        c = make(min_running_percent=20)
        d = c.update(Reading(49.0), self.profile, now=0)
        self.assertEqual(d.percent, 20)


    def test_flat_curve_cannot_keep_fans_off_when_hot(self):
        flat = Profile("flat", [(0, 0), (100, 0)], [(0, 0), (100, 0)])
        c = make()
        self.assertEqual(c.update(Reading(70.0), flat, now=0).percent, 0)
        c = make()
        self.assertGreaterEqual(c.update(Reading(88.0), flat, now=0).percent, 60)

    def test_critical_temp_is_capped(self):
        c = FanController(DEFAULTS["control"], 150)
        self.assertTrue(c.update(Reading(95.0), self.profile, now=0).critical)

    def test_default_profiles_stay_above_safety_floor(self):
        cfg = Config()
        for name in cfg.raw["profiles"]:
            p = cfg.profile(name)
            for t in range(0, 96):
                self.assertGreaterEqual(interpolate(p.cpu_curve, t), interpolate(SAFETY_FLOOR, t), name)
                self.assertGreaterEqual(interpolate(p.gpu_curve, t), interpolate(SAFETY_FLOOR, t), name)


class ToggleTest(unittest.TestCase):
    def test_hysteresis(self):
        t = ToggleAdapter(80, 55)
        self.assertFalse(t.update(70, False))
        self.assertTrue(t.update(85, False))
        self.assertTrue(t.update(60, False))
        self.assertFalse(t.update(50, False))
        self.assertTrue(t.update(0, True))

    def test_min_on_time(self):
        t = ToggleAdapter(80, 55, min_on=60)
        self.assertTrue(t.update(90, False, now=0))
        self.assertTrue(t.update(10, False, now=30))   # noch Mindestlaufzeit
        self.assertFalse(t.update(10, False, now=61))


class ConfigTest(unittest.TestCase):
    def test_example_config_loads(self):
        cfg = load_config(ROOT_TEMPLATE)
        self.assertEqual(cfg.general["profile"], "balanced")
        self.assertIn("quiet", cfg.raw["profiles"])

    def test_default_profiles_never_stop_fans_when_warm(self):
        cfg = Config()
        for name in cfg.raw["profiles"]:
            p = cfg.profile(name)
            for t in range(50, 101):
                self.assertGreater(interpolate(p.cpu_curve, t), 0, f"{name} bei {t} °C")
        self.assertEqual(interpolate(cfg.profile("balanced").cpu_curve, 30), 20)

    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def test_invalid_values_rejected(self):
        for text in ("[general]\ninterval = 0", "[general]\ninterval = \"2\"",
                     "[general]\ncritical_temp = 200", "[control]\nramp_down_rate = -1",
                     "[windows]\nmax_level_cpu = 54.5", "[windows]\nmode = \"turbo\"",
                     "[update]\nenabled = \"ja\"", "[toggle]\nrelease_percent = 90",
                     "general = 5", "[profiles.x]\ncpu_curve = [[\"a\", 1]]"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load(text)

    def test_valid_values_and_old_keys_accepted(self):
        cfg = self.load('[general]\nlog_level = "debug"\ninterval = 1\n'
                        '[update]\nrepo = "x/y"\nbranch = "main"\n')     # alte Schlüssel stören nicht
        self.assertEqual(cfg.general["interval"], 1)

    def test_configs_of_earlier_versions_still_load(self):
        # Die config.toml bleibt bei Updates erhalten - auch alte Dateien müssen gelten.
        folder = Path(__file__).resolve().parent / "legacy_configs"
        files = sorted(folder.glob("*.toml"))
        self.assertTrue(files)
        for path in files:
            with self.subTest(path.name):
                load_config(path)

    def test_invalid_curve_rejected(self):
        cfg = Config()
        cfg.raw["profiles"]["bad"] = {"cpu_curve": [[50, 10], [40, 20]]}
        with self.assertRaises(ValueError):
            cfg.profile("bad")


if __name__ == "__main__":
    unittest.main()
