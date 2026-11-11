import unittest

from victus_fan import selftest


class FakeBios:
    """Verhält sich wie ein BIOS, das SetFanLevel annimmt, aber ignoriert."""

    def __init__(self, levels_work: bool, max_works: bool = True):
        self.levels_work = levels_work
        self.max_works = max_works
        self.max_cpu, self.max_gpu = 55, 57
        self.rpm = [2700, 0]
        self.supports_level = True

    def read_fan_rpm(self):
        return list(self.rpm)

    def restore_auto(self):
        self.rpm = [2700, 0]

    def set_max(self, enabled):
        if self.max_works:
            self.rpm = [5100, 5300] if enabled else [2700, 0]

    def set_levels(self, levels):
        if self.levels_work:
            self.rpm = [lvl * 100 for lvl in levels]


class FakeBiosWithModes(FakeBios):
    """Wie das BIOS des Victus BV8D4EA: feste Drehzahlen wirken nur in
    bestimmten Leistungsmodi; kleine Stufen hebt das BIOS ggf. an."""

    def __init__(self, level_modes=("performance",), floor=0):
        super().__init__(levels_work=False)
        self.level_modes = level_modes
        self.floor = floor
        self.fan_mode = "default"
        self.mode_calls = []

    def set_fan_mode(self, name):
        self.fan_mode = name
        self.mode_calls.append(name)
        self.rpm = [2200, 2100] if name in ("cool", "performance") else [2700, 0]

    def set_levels(self, levels):
        if self.fan_mode in self.level_modes:
            self.rpm = [0 if lvl == 0 else max(self.floor, lvl) * 100 for lvl in levels]


class SelftestTest(unittest.TestCase):
    def run_test(self, bios):
        return selftest.run(bios, settle=0, say=lambda *a, **k: None)

    def test_levels_ignored_recommends_toggle(self):
        r = self.run_test(FakeBios(levels_work=False))
        self.assertTrue(r.max_works)
        self.assertFalse(r.level_works)
        self.assertEqual(r.recommendation["mode"], "toggle")
        self.assertEqual(r.recommendation["max_level_cpu"], 51)

    def test_levels_work_recommends_level(self):
        r = self.run_test(FakeBios(levels_work=True))
        self.assertTrue(r.level_works)
        self.assertEqual(r.recommendation["mode"], "level")
        self.assertEqual(r.level_high_set, [43, 45])

    def test_nothing_works(self):
        r = self.run_test(FakeBios(levels_work=False, max_works=False))
        self.assertNotIn("mode", r.recommendation)

    def test_levels_only_in_performance_mode(self):
        bios = FakeBiosWithModes(level_modes=("performance",))
        r = self.run_test(bios)
        self.assertFalse(r.level_works)
        self.assertFalse(r.mode_levels["cool"]["works"])
        self.assertTrue(r.mode_levels["performance"]["works"])
        self.assertEqual(r.recommendation["mode"], "level")
        self.assertEqual(r.recommendation["fan_mode"], "performance")
        self.assertEqual(r.recommendation["min_level"], 0)
        self.assertEqual(r.level_zero, [0, 0])
        self.assertEqual(bios.mode_calls[-1], "default")  # zurückgesetzt

    def test_cool_preferred_when_it_works(self):
        r = self.run_test(FakeBiosWithModes(level_modes=("cool", "performance")))
        self.assertEqual(r.recommendation["fan_mode"], "cool")

    def test_floor_detected(self):
        r = self.run_test(FakeBiosWithModes(floor=22))
        self.assertEqual(r.level_min_test, [2200, 2200])
        self.assertEqual(r.recommendation["min_level"], 22)

    def test_modes_tested_but_levels_never_work(self):
        r = self.run_test(FakeBiosWithModes(level_modes=()))
        self.assertEqual(r.recommendation["mode"], "toggle")
        self.assertEqual(r.recommendation["fan_mode"], "")
        self.assertNotIn("min_level", r.recommendation)

    def test_quick_skips_modes(self):
        bios = FakeBiosWithModes()
        r = selftest.run(bios, settle=0, say=lambda *a, **k: None, extended=False)
        self.assertEqual(bios.mode_calls, [])
        self.assertEqual(r.recommendation["mode"], "toggle")

if __name__ == "__main__":
    unittest.main()
