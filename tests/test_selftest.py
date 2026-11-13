import unittest

from victus_fan import selftest


class FakeBios:
    """Nachbau eines HP-BIOS: Max wirkt; feste Drehzahlen nur in ``level_modes``
    (None = immer). Kleine Stufen hebt es ggf. auf ``floor`` an."""

    def __init__(self, level_modes=("default", "cool", "performance"), max_works=True, floor=0):
        self.level_modes = level_modes
        self.max_works = max_works
        self.floor = floor
        self.max_cpu, self.max_gpu = 54, 52
        self.fan_mode = None           # nach dem Systemstart: kein Modus gesetzt
        self.mode_calls = []
        self.rpm = [2700, 0]

    def read_fan_rpm(self):
        return list(self.rpm)

    def restore_auto(self):
        self.rpm = [2700, 0]

    def set_max(self, enabled):
        if self.max_works:
            self.rpm = [5400, 5200] if enabled else [2700, 0]

    def set_fan_mode(self, name):
        self.fan_mode = name
        self.mode_calls.append(name)

    def set_levels(self, levels):
        if self.level_modes is None or self.fan_mode in self.level_modes:
            self.rpm = [0 if lvl == 0 else max(self.floor, lvl) * 100 for lvl in levels]


def run(bios):
    return selftest.run(bios, settle=0, say=lambda *a, **k: None)


class SelftestTest(unittest.TestCase):
    def test_victus_bv8d4ea(self):
        # Wie gemessen: jeder gesetzte Modus schaltet frei -> "default" genügt.
        bios = FakeBios()
        r = run(bios)
        self.assertTrue(r.max_works)
        self.assertEqual(r.best_fan_mode, "default")
        self.assertEqual(list(r.mode_levels), ["default"])   # bricht nach Erfolg ab
        self.assertEqual(r.recommendation, {"mode": "level", "fan_mode": "default",
                                            "max_level_cpu": 54, "max_level_gpu": 52, "min_level": 0})
        self.assertEqual(r.level_zero, [0, 0])
        self.assertEqual(r.start_from_stop, [1100, 1100])
        self.assertEqual(bios.mode_calls[-1], "default")     # am Ende zurückgesetzt

    def test_falls_back_to_next_mode(self):
        r = run(FakeBios(level_modes=("performance",)))
        self.assertEqual(list(r.mode_levels), ["default", "cool", "performance"])
        self.assertEqual(r.recommendation["fan_mode"], "performance")

    def test_toggle_when_levels_never_work(self):
        r = run(FakeBios(level_modes=()))
        self.assertEqual(r.recommendation["mode"], "toggle")
        self.assertEqual(r.recommendation["fan_mode"], "default")
        self.assertNotIn("min_level", r.recommendation)

    def test_floor_detected(self):
        r = run(FakeBios(floor=22))
        self.assertEqual(r.recommendation["min_level"], 22)

    def test_nothing_works(self):
        r = run(FakeBios(level_modes=(), max_works=False))
        self.assertNotIn("mode", r.recommendation)

    def test_backend_without_modes(self):
        class NoModes:
            """Backend ohne BIOS-Modi und ohne set_levels (wie die Simulation)."""
            supports_level = True

            def __init__(self):
                self.rpm = [2000, 2000]

            def read_fan_rpm(self):
                return list(self.rpm)

            def restore_auto(self):
                self.rpm = [2000, 2000]

            def set_max(self, enabled):
                self.rpm = [5400, 5200] if enabled else [2000, 2000]

            def set_percent(self, percent):
                self.rpm = [int(percent * 100)] * 2

        r = run(NoModes())
        self.assertEqual(list(r.mode_levels), ["-"])
        self.assertEqual(r.recommendation["mode"], "level")
        self.assertNotIn("fan_mode", r.recommendation)
    def test_fan_does_not_start_from_standstill(self):
        class Sticky(FakeBios):
            def set_levels(self, levels):
                before = list(self.rpm)
                super().set_levels(levels)
                # stehende Lüfter laufen unter Stufe 20 nicht an
                self.rpm = [0 if b == 0 and lvl < 20 else r for b, lvl, r in zip(before, levels, self.rpm)]
        r = run(Sticky())
        self.assertEqual(r.start_from_stop, [0, 0])
        self.assertEqual(r.recommendation["mode"], "level")



class RecommendationTest(unittest.TestCase):
    def test_implausible_measurements_are_not_recommended(self):
        from victus_fan.config import Config, validate
        from victus_fan.selftest import Result, _recommend

        good = Result(max_on=[5400, 5200], max_works=True, level_works=True, best_fan_mode="default")
        self.assertEqual(_recommend(good, True)["max_level_cpu"], 54)
        bad = Result(max_on=[100, 9900], max_works=True, level_works=True, best_fan_mode="default",
                     level_min_test=[5000, 5000])
        rec = _recommend(bad, True)
        self.assertNotIn("max_level_cpu", rec)
        self.assertEqual(rec["min_level"], 0)
        raw = Config().raw
        raw["windows"].update(rec)
        validate(raw)                     # Empfehlung ergibt immer eine gültige Konfiguration


if __name__ == "__main__":
    unittest.main()
