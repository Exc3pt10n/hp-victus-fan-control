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


if __name__ == "__main__":
    unittest.main()
