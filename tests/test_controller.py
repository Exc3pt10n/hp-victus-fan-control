import unittest

from victus_fan.config import DEFAULTS, Config, load_config
from victus_fan.controller import FanController, Reading, ToggleAdapter, interpolate


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
        self.profile = Config().profile("balanced")

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
        cfg = load_config("config.example.toml")
        self.assertEqual(cfg.general["profile"], "balanced")
        self.assertIn("quiet", cfg.raw["profiles"])

    def test_invalid_curve_rejected(self):
        cfg = Config()
        cfg.raw["profiles"]["bad"] = {"cpu_curve": [[50, 10], [40, 20]]}
        with self.assertRaises(ValueError):
            cfg.profile("bad")


if __name__ == "__main__":
    unittest.main()
