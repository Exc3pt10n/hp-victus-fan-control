import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from victus_fan.backends.base import FanBackend
from victus_fan.config import Config
from victus_fan.service import FanService


class FakeBackend(FanBackend):
    name = "fake"

    def __init__(self, level=True, keepalive=None):
        self.supports_level = level
        self.supports_max = True
        self.keepalive = keepalive
        self.temp = 40.0
        self.calls = []

    def read_cpu_temp(self):
        return self.temp

    def read_gpu_temp(self):
        return None

    def on_battery(self):
        return False

    def set_percent(self, percent):
        self.calls.append(("level", percent))

    def set_max(self, enabled):
        self.calls.append(("max", enabled))

    def restore_auto(self):
        self.calls.append(("restore",))


class ServiceTest(unittest.TestCase):
    def test_level_mode_sends_only_changes_and_keepalive(self):
        b = FakeBackend(level=True, keepalive=60)
        s = FanService(Config(), b)
        with mock.patch("victus_fan.service.time.monotonic", side_effect=[0, 2, 70]):
            s.step(); s.step(); s.step()
        self.assertEqual(b.calls, [("level", 0), ("level", 0)])  # Start + Keepalive

    def test_toggle_mode_uses_max(self):
        b = FakeBackend(level=False)
        s = FanService(Config(), b)
        b.temp = 90.0
        s.step()
        self.assertEqual(b.calls[-1], ("max", True))

    def test_critical_in_level_mode_uses_max(self):
        b = FakeBackend(level=True)
        s = FanService(Config(), b)
        b.temp = 99.0
        s.step()
        self.assertEqual(b.calls[-1], ("max", True))

    def test_override_and_status_file(self):
        with tempfile.TemporaryDirectory() as d:
            override = Path(d) / "override.json"
            status = Path(d) / "status.json"
            override.write_text(json.dumps({"profile": "performance"}))
            s = FanService(Config(), FakeBackend(), status_file=status, override_file=override)
            s.step()
            data = json.loads(status.read_text())
            self.assertEqual(data["profile"], "performance")
            self.assertEqual(data["profile_reason"], "manuell")

    def test_dry_run_sets_nothing(self):
        b = FakeBackend()
        FanService(Config(), b, dry_run=True).step()
        self.assertEqual(b.calls, [])


if __name__ == "__main__":
    unittest.main()
