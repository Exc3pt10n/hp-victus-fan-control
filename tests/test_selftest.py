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
    """Feste Drehzahlen wirken nur im Modus performance."""

    def __init__(self, perf_levels_work: bool):
        super().__init__(levels_work=False)
        self.perf_levels_work = perf_levels_work
        self.fan_mode = "default"
        self.mode_calls = []

    def set_fan_mode(self, name):
        self.fan_mode = name
        self.mode_calls.append(name)
        self.rpm = {"cool": [3500, 2000], "performance": [3200, 1500]}.get(name, [2700, 0])

    def set_levels(self, levels):
        if self.perf_levels_work and self.fan_mode == "performance":
            self.rpm = [lvl * 100 for lvl in levels]


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
        bios = FakeBiosWithModes(perf_levels_work=True)
        r = self.run_test(bios)
        self.assertFalse(r.level_works)
        self.assertTrue(r.perf_level_works)
        self.assertEqual(r.recommendation["mode"], "level")
        self.assertEqual(r.recommendation["fan_mode"], "performance")
        self.assertEqual(bios.mode_calls[-1], "default")  # zurückgesetzt
        self.assertEqual(r.fan_modes["cool"], [3500, 2000])

    def test_modes_tested_but_levels_never_work(self):
        r = self.run_test(FakeBiosWithModes(perf_levels_work=False))
        self.assertEqual(r.recommendation["mode"], "toggle")
        self.assertEqual(r.recommendation["fan_mode"], "")

    def test_quick_skips_modes(self):
        bios = FakeBiosWithModes(perf_levels_work=True)
        r = selftest.run(bios, settle=0, say=lambda *a, **k: None, extended=False)
        self.assertEqual(bios.mode_calls, [])
        self.assertEqual(r.recommendation["mode"], "toggle")


if __name__ == "__main__":
    unittest.main()
