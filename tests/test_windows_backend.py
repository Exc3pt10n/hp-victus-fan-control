import unittest
from unittest import mock

from victus_fan.backends import windows
from victus_fan.config import DEFAULTS


class FakeHpBios:
    def __init__(self):
        self.calls = []

    def call(self, command_type, data=None, out_size=128, command=windows.CMD_DEFAULT):
        self.calls.append((command_type, list(data or [])))
        if command_type == windows.GET_FAN_LEVEL:
            return [27, 0]
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

    def test_no_fan_mode_by_default(self):
        b = make(mode="toggle")
        self.assertNotIn(windows.SET_FAN_MODE, [c[0] for c in b.bios.calls])
        self.assertFalse(b.supports_level)


if __name__ == "__main__":
    unittest.main()
