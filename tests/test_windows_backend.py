import unittest
from unittest import mock

from victus_fan.backends import windows
from victus_fan.config import DEFAULTS


class FakeHpBios:
    def __init__(self):
        self.calls = []
        self.rpm_levels = [27, 26]      # gemeldete Drehzahl in 100 U/min

    def call(self, command_type, data=None, out_size=128, command=windows.CMD_DEFAULT):
        self.calls.append((command_type, list(data or [])))
        if command_type == windows.GET_FAN_LEVEL:
            return list(self.rpm_levels)
        return []


def make(**settings):
    cfg = dict(DEFAULTS["windows"], **settings)
    with mock.patch.object(windows, "HpBios", FakeHpBios):
        return windows.WindowsBackend(cfg)


class WindowsBackendTest(unittest.TestCase):
    def test_fan_mode_applied_and_reset(self):
        b = make(mode="level", fan_mode="performance", max_level_cpu=54, max_level_gpu=52)
        self.assertIn((windows.SET_FAN_MODE, [0xFF, 0x31]), b.bios.calls)
        b.shutdown()
        self.assertEqual(b.bios.calls[-1], (windows.SET_FAN_MODE, [0xFF, 0x30]))
        b.reconfigure(dict(DEFAULTS["windows"], mode="level", fan_mode="performance"))
        self.assertEqual(b.bios.calls[-1], (windows.SET_FAN_MODE, [0xFF, 0x31]))

    def test_levels_and_min_level(self):
        b = make(mode="level", max_level_cpu=54, max_level_gpu=52, min_level=15)
        self.assertEqual(b.levels_for(100), [54, 52])
        self.assertEqual(b.levels_for(10), [15, 15])
        self.assertEqual(b.levels_for(0), [0, 0])
        self.assertIsNone(b.expected_rpm(0))
        b.set_percent(50)
        self.assertEqual(b.bios.calls[-1], (windows.SET_FAN_LEVEL, [27, 26]))

    def test_defaults_set_standard_mode(self):
        b = make()
        self.assertIn((windows.SET_FAN_MODE, [0xFF, 0x30]), b.bios.calls)
        self.assertTrue(b.supports_level)
        self.assertEqual(b.levels_for(100), [54, 52])

    def test_toggle_mode(self):
        self.assertFalse(make(mode="toggle").supports_level)

    def test_bios_temperature_checked_only_once(self):
        b = make()
        with mock.patch.object(b, "_acpi_temp", return_value=45.0):
            self.assertEqual(b.read_cpu_temp(), 45.0)
            self.assertEqual(b.read_cpu_temp(), 45.0)
        temp_calls = [c for c in b.bios.calls if c[0] == windows.GET_TEMP]
        self.assertEqual(len(temp_calls), 1)

    def test_mode_is_refreshed_with_levels(self):
        clock = [1000.0]
        with mock.patch.object(windows.time, "monotonic", lambda: clock[0]):
            b = make()                                   # setzt "default" beim Start
            b.set_levels([20, 20])
            modes = lambda: [c for c in b.bios.calls if c[0] == windows.SET_FAN_MODE]
            self.assertEqual(len(modes()), 1)            # noch frisch
            clock[0] += 60
            b.set_levels([20, 20])
            self.assertEqual(len(modes()), 2)            # nach 50 s erneut gesetzt
            self.assertEqual(modes()[-1], (windows.SET_FAN_MODE, [0xFF, 0x30]))
            self.assertEqual(b.bios.calls[-1][0], windows.SET_FAN_LEVEL)

    def test_kick_starts_stalled_fan(self):
        b = make()
        b.bios.rpm_levels = [27, 0]                       # GPU-Lüfter steht
        b.set_levels([12, 11])
        self.assertEqual(b.bios.calls[-1], (windows.SET_FAN_LEVEL, [12, windows.KICK_LEVEL]))
        self.assertTrue(b.kicking)
        b.maintain()                                      # dreht noch nicht -> bleibt
        self.assertEqual(b.bios.calls[-1][0], windows.GET_FAN_LEVEL)
        b.bios.rpm_levels = [12, 25]                      # läuft jetzt
        b.maintain()
        self.assertEqual(b.bios.calls[-1], (windows.SET_FAN_LEVEL, [12, 11]))
        self.assertFalse(b.kicking)

    def test_no_kick_for_zero_or_running(self):
        b = make()
        b.bios.rpm_levels = [0, 0]
        b.set_levels([0, 0])                              # Stillstand gewollt
        self.assertEqual(b.bios.calls[-1], (windows.SET_FAN_LEVEL, [0, 0]))
        b.bios.rpm_levels = [0, 0]
        b.set_levels([12, 11], kick=False)                # Hardware-Test: ungeschönt
        self.assertEqual(b.bios.calls[-1], (windows.SET_FAN_LEVEL, [12, 11]))

    def test_kick_gives_up_after_timeout(self):
        clock = [0.0]
        with mock.patch.object(windows.time, "monotonic", lambda: clock[0]):
            b = make()
            b.bios.rpm_levels = [0, 0]
            b.set_levels([12, 11])
            clock[0] += windows.KICK_TIMEOUT + 1
            b.maintain()
            self.assertFalse(b.kicking)
            self.assertEqual(b.bios.calls[-1], (windows.SET_FAN_LEVEL, [12, 11]))


if __name__ == "__main__":
    unittest.main()
