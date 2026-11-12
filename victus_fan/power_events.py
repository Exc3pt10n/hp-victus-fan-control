"""Benachrichtigungen über Standby und Bildschirm an/aus (Windows).

Im (modernen) Standby übernimmt beim Victus BV8D4EA die Firmware die Lüfter und
ignoriert Drehzahlbefehle; nach dem Aufwachen muss die Steuerung den
Leistungsmodus neu setzen. Dieses Modul meldet die nötigen Ereignisse.

Umsetzung ohne Zusatzpakete über ctypes: ein unsichtbares Fenster in einem
eigenen Thread empfängt WM_POWERBROADCAST (Standby/Aufwachen) und - per
RegisterPowerSettingNotification - Änderungen von GUID_CONSOLE_DISPLAY_STATE
(Bildschirm aus/an; Moderner Standby schaltet ihn immer aus).
"""

from __future__ import annotations

import logging
import sys
import threading
from collections import deque

log = logging.getLogger(__name__)

SUSPEND, RESUME, DISPLAY_OFF, DISPLAY_ON = "suspend", "resume", "display_off", "display_on"

WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_POWERSETTINGCHANGE = 0x8013
# GUID_CONSOLE_DISPLAY_STATE {6FE69556-704A-47A0-8F24-C28D936FDA47}
DISPLAY_GUID = (0x6FE69556, 0x704A, 0x47A0, (0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47))


class PowerEvents:
    """Sammelt Ereignisse aus dem Fenster-Thread; ``pop()`` holt sie ab."""

    def __init__(self):
        self._events: deque[str] = deque(maxlen=50)
        self._lock = threading.Lock()
        self.active = False

    def push(self, event: str) -> None:
        with self._lock:
            self._events.append(event)

    def pop(self) -> list[str]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        ready = threading.Event()
        thread = threading.Thread(target=self._run, args=(ready,), name="power-events", daemon=True)
        thread.start()
        ready.wait(5)
        return self.active

    # ------------------------------------------------------------------
    def _run(self, ready: threading.Event) -> None:  # pragma: no cover - nur unter Windows
        try:
            self._message_loop(ready)
        except Exception as exc:  # noqa: BLE001
            log.warning("Standby-Erkennung nicht verfügbar: %s", exc)
            ready.set()

    def _message_loop(self, ready: threading.Event) -> None:  # pragma: no cover - nur unter Windows
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        class POWERBROADCAST_SETTING(ctypes.Structure):
            _fields_ = [("PowerSetting", GUID), ("DataLength", wintypes.DWORD), ("Data", ctypes.c_ubyte * 4)]

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR)]

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
        user32.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE, ctypes.POINTER(GUID), wintypes.DWORD]

        d1, d2, d3, d4 = DISPLAY_GUID
        display_guid = GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_POWERBROADCAST:
                if wparam == PBT_APMSUSPEND:
                    self.push(SUSPEND)
                elif wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                    self.push(RESUME)
                elif wparam == PBT_POWERSETTINGCHANGE and lparam:
                    setting = ctypes.cast(lparam, ctypes.POINTER(POWERBROADCAST_SETTING)).contents
                    if bytes(setting.PowerSetting) == bytes(display_guid):
                        state = setting.Data[0]          # 0 = aus, 1 = an, 2 = gedimmt
                        self.push(DISPLAY_OFF if state == 0 else DISPLAY_ON)
                return 1
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)          # Referenz halten (sonst Absturz)
        hinstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=hinstance, lpszClassName="VictusFanPowerEvents")
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise OSError(ctypes.get_last_error(), "RegisterClassW")
        # Unsichtbares Fenster der obersten Ebene (keine Message-only-Fenster:
        # die erhalten WM_POWERBROADCAST nicht).
        hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "VictusFanPowerEvents", 0,
                                      0, 0, 0, 0, None, None, hinstance, None)
        if not hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW")
        if not user32.RegisterPowerSettingNotification(hwnd, ctypes.byref(display_guid), 0):
            log.warning("Bildschirm-Benachrichtigung nicht verfügbar (Fehler %s)", ctypes.get_last_error())
        self.active = True
        log.info("Standby-Erkennung aktiv")
        ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
