import unittest

from victus_fan import sensors
from victus_fan.sensors import Sensor, Trace


def trace(sid, idle, load, cool=(), gpu=False):
    t = Trace(Sensor(sid, sid, lambda: None, gpu=gpu))
    t.idle = list(idle)
    t.load = [(float(i), v) for i, v in enumerate(load)]
    t.cool = list(cool)
    return t.summary()


class SensorsTest(unittest.TestCase):
    def test_summary(self):
        r = trace("x", [45, 45], [45, 47, 50, 60, 70])
        self.assertEqual(r["idle"], 45)
        self.assertEqual(r["peak"], 70)
        self.assertEqual(r["rise"], 25)
        self.assertEqual(r["react_s"], 2.0)
        self.assertFalse(r["static"])

    def test_static_sensor_detected(self):
        self.assertTrue(trace("acpi", [46.1] * 3, [46.1] * 10, [46.1] * 3)["static"])

    def test_recommend_fastest_cpu_sensor(self):
        results = [
            trace("acpi", [46.1] * 3, [46.1] * 10),                     # statisch
            trace("perf:slow", [44, 44], [44, 44, 45, 46, 47, 48, 50, 52]),
            trace("perf:cpu", [45, 45], [45, 62, 75, 80]),
            trace("nvidia-smi", [40, 40], [40, 60, 70], gpu=True),       # GPU zählt nicht
        ]
        self.assertEqual(sensors.recommend(results)["id"], "perf:cpu")

    def test_recommend_none_if_nothing_reacts(self):
        self.assertIsNone(sensors.recommend([trace("a", [40, 40], [40, 41, 42])]))

    def test_measure_with_fake_sensors(self):
        values = iter(range(40, 400))
        found = {"fake": Sensor("fake", "Fake", lambda: float(next(values)))}
        results = sensors.measure(found, idle=0.3, load=0.5, cool=0.2, interval=0.1, burn=False,
                                  say=lambda *a, **k: None)
        self.assertEqual(results[0]["id"], "fake")
        self.assertGreater(results[0]["rise"], 0)


if __name__ == "__main__":
    unittest.main()
