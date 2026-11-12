import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from victus_fan import autostart


class FakeWinreg(types.SimpleNamespace):
    """Minimaler Nachbau von winreg (nur HKEY_CURRENT_USER)."""

    def __init__(self):
        super().__init__(HKEY_CURRENT_USER="HKCU", REG_SZ=1, REG_BINARY=3, store={})

    class _Key:
        def __init__(self, store, path):
            self.store, self.path = store, path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, root, path):
        if path not in self.store:
            raise OSError("fehlt")
        return self._Key(self.store, path)

    def CreateKey(self, root, path):
        self.store.setdefault(path, {})
        return self._Key(self.store, path)

    def QueryValueEx(self, key, name):
        if name not in key.store[key.path]:
            raise OSError("fehlt")
        return key.store[key.path][name], 1

    def SetValueEx(self, key, name, _reserved, _type, value):
        key.store[key.path][name] = value

    def DeleteValue(self, key, name):
        if name not in key.store[key.path]:
            raise OSError("fehlt")
        del key.store[key.path][name]


class AutostartTest(unittest.TestCase):
    def setUp(self):
        self.reg = FakeWinreg()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (mock.patch.object(autostart, "_winreg", lambda: self.reg),
                  mock.patch.object(autostart, "settings_file", lambda: Path(tmp.name) / "settings.json"),
                  mock.patch.object(sys, "executable", r"C:\VictusFan\venv\Scripts\python.exe")):
            p.start()
            self.addCleanup(p.stop)

    def run_value(self):
        return self.reg.store.get(autostart.RUN_KEY, {}).get(autostart.VALUE_NAME)

    def test_default_on_after_first_start(self):
        autostart.sync()
        self.assertTrue(autostart.is_enabled())
        self.assertIn("pythonw.exe", self.run_value())       # ohne Konsolenfenster
        self.assertTrue(self.run_value().endswith("-m victus_fan gui --hidden"))

    def test_switch_off_and_on(self):
        autostart.sync()
        autostart.set_enabled(False)
        self.assertFalse(autostart.is_enabled())
        self.assertIsNone(self.run_value())
        autostart.sync()                                     # gemerkte Wahl bleibt aus
        self.assertFalse(autostart.is_enabled())
        autostart.set_enabled(True)
        self.assertTrue(autostart.is_enabled())

    def test_disabled_in_windows_settings_is_respected(self):
        autostart.sync()
        self.reg.store[autostart.APPROVED_KEY] = {autostart.VALUE_NAME: bytes([0x03] + [0] * 11)}
        self.assertFalse(autostart.is_enabled())
        autostart.sync()                                     # nicht wieder einschalten
        self.assertFalse(autostart.is_enabled())
        self.assertFalse(autostart.wanted())

    def test_enabling_overrides_windows_setting(self):
        autostart.sync()
        self.reg.store[autostart.APPROVED_KEY] = {autostart.VALUE_NAME: bytes([0x03] + [0] * 11)}
        autostart.set_enabled(True)                          # ausdrücklich im Tray-Menü
        self.assertTrue(autostart.is_enabled())

    def test_not_windows(self):
        with mock.patch.object(autostart, "_winreg", lambda: None):
            self.assertIsNone(autostart.is_enabled())
            autostart.sync()                                 # keine Fehler


if __name__ == "__main__":
    unittest.main()
