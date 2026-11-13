import unittest

from victus_fan.config import DEFAULTS, Config, Profile, config_from_values, load_config
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
    def test_defaults(self):
        cfg = load_config(None)
        self.assertEqual(cfg.general["profile"], "balanced")
        self.assertEqual(list(cfg.raw["profiles"]), ["quiet", "balanced", "performance"])

    def test_default_profiles_never_stop_fans_when_warm(self):
        cfg = Config()
        for name in cfg.raw["profiles"]:
            p = cfg.profile(name)
            for t in range(50, 101):
                self.assertGreater(interpolate(p.cpu_curve, t), 0, f"{name} bei {t} °C")
        self.assertEqual(interpolate(cfg.profile("balanced").cpu_curve, 30), 20)

    def test_invalid_values_rejected(self):
        for values in ({"general": {"interval": 0}}, {"general": {"interval": "2"}},
                       {"general": {"critical_temp": 200}}, {"control": {"ramp_down_rate": -1}},
                       {"windows": {"max_level_cpu": 54.5}}, {"windows": {"mode": "turbo"}},
                       {"update": {"enabled": "ja"}}, {"general": 5}, {"general": {"profile": "office"}},
                       {"profiles": {"quiet": {"cpu_curve": [["a", 1]]}}}, []):
            with self.subTest(values=values), self.assertRaises(ValueError):
                config_from_values(values)

    def test_fixed_and_unknown_values_are_ignored(self):
        cfg = config_from_values({"general": {"backend": "simulate", "log_level": "DEBUG", "x": 1},
                                  "windows": {"keepalive": 5}, "toggle": {"release_percent": 90},
                                  "profiles": {"office": {"cpu_curve": [[0, 0], [90, 100]]}}})
        self.assertEqual(cfg.general["backend"], "windows")       # nur per Befehlszeile
        self.assertEqual(cfg.general["log_level"], "DEBUG")
        self.assertEqual(cfg.windows["keepalive"], 60.0)
        self.assertEqual(cfg.toggle["release_percent"], 55.0)
        self.assertNotIn("office", cfg.raw["profiles"])

    def test_invalid_curve_rejected(self):
        cfg = Config()
        cfg.raw["profiles"]["bad"] = {"cpu_curve": [[50, 10], [40, 20]]}
        with self.assertRaises(ValueError):
            cfg.profile("bad")


if __name__ == "__main__":
    unittest.main()
