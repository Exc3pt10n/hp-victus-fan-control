"""Ist die NVIDIA-Grafikkarte gerade wach? - ohne sie dabei aufzuwecken.

Auf Notebooks mit umschaltbarer Grafik (Optimus) schläft die dedizierte GPU,
solange nichts sie braucht (Gerätezustand D3). Jeder Aufruf von ``nvidia-smi``
weckt sie dagegen auf - regelmäßig abgefragt, käme sie nie mehr zur Ruhe
(mehr Stromverbrauch, Wärme, weniger Akkulaufzeit).

Den aktuellen Gerätezustand kennt Windows aber selbst: Die Geräteverwaltung
zeigt ihn unter Details > "Energiedaten" an. Er stammt aus dem Plug-and-Play-
Manager und wird ohne Zugriff auf die Grafikkarte gelesen (SetupAPI,
SPDRP_DEVICE_POWER_DATA).
"""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

NVIDIA_VENDOR = "VEN_10DE"
DIGCF_PRESENT = 0x2
SPDRP_HARDWAREID = 0x1
SPDRP_DEVICE_POWER_DATA = 0x1E
POWER_DEVICE_D0 = 1           # DEVICE_POWER_STATE: 0 = unbekannt, 1 = D0 (an) ... 4 = D3


if sys.platform == "win32":
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", GUID), ("DevInst", wintypes.DWORD),
                    ("Reserved", ctypes.c_size_t)]

    class CM_POWER_DATA(ctypes.Structure):
        _fields_ = [("PD_Size", wintypes.DWORD), ("PD_MostRecentPowerState", ctypes.c_int),
                    ("PD_Capabilities", wintypes.DWORD), ("PD_D1Latency", wintypes.DWORD),
                    ("PD_D2Latency", wintypes.DWORD), ("PD_D3Latency", wintypes.DWORD),
                    ("PD_PowerStateMapping", ctypes.c_int * 7), ("PD_DeepestSystemWake", ctypes.c_int)]

    # {4d36e968-e325-11ce-bfc1-08002be10318}: Geräteklasse "Grafikkarten"
    GUID_DEVCLASS_DISPLAY = GUID(0x4D36E968, 0xE325, 0x11CE,
                                 (ctypes.c_ubyte * 8)(0xBF, 0xC1, 0x08, 0x00, 0x2B, 0xE1, 0x03, 0x18))

    _setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    _GetClassDevs = _setupapi.SetupDiGetClassDevsW
    _GetClassDevs.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    _GetClassDevs.restype = ctypes.c_void_p
    _EnumDeviceInfo = _setupapi.SetupDiEnumDeviceInfo
    _EnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
    _EnumDeviceInfo.restype = wintypes.BOOL
    _GetProperty = _setupapi.SetupDiGetDeviceRegistryPropertyW
    _GetProperty.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD)]
    _GetProperty.restype = wintypes.BOOL
    _DestroyList = _setupapi.SetupDiDestroyDeviceInfoList
    _DestroyList.argtypes = [ctypes.c_void_p]
    _DestroyList.restype = wintypes.BOOL
    _INVALID_HANDLE = ctypes.c_void_p(-1).value


def _power_states() -> list[int]:
    """Gerätezustände aller NVIDIA-Grafikkarten (leer = keine gefunden)."""
    handle = _GetClassDevs(ctypes.byref(GUID_DEVCLASS_DISPLAY), None, None, DIGCF_PRESENT)
    if not handle or handle == _INVALID_HANDLE:
        raise OSError(ctypes.get_last_error(), "SetupDiGetClassDevs fehlgeschlagen")
    states = []
    try:
        index = 0
        while True:
            info = SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            if not _EnumDeviceInfo(handle, index, ctypes.byref(info)):
                break
            index += 1
            ids = ctypes.create_unicode_buffer(1024)
            if not _GetProperty(handle, ctypes.byref(info), SPDRP_HARDWAREID, None, ids,
                                ctypes.sizeof(ids), None):
                continue
            # REG_MULTI_SZ: mehrere IDs, durch Nullzeichen getrennt
            if NVIDIA_VENDOR not in ctypes.wstring_at(ids, len(ids)).upper():
                continue
            power = CM_POWER_DATA()
            power.PD_Size = ctypes.sizeof(CM_POWER_DATA)
            if _GetProperty(handle, ctypes.byref(info), SPDRP_DEVICE_POWER_DATA, None,
                            ctypes.byref(power), ctypes.sizeof(power), None):
                states.append(power.PD_MostRecentPowerState)
    finally:
        _DestroyList(handle)
    return states


class GpuPower:
    """Merkt sich, ob die Abfrage funktioniert; bei Problemen gilt "unbekannt"."""

    def __init__(self):
        self._broken = sys.platform != "win32"

    def awake(self) -> bool | None:
        """True = wach (D0), False = schläft, None = unbekannt/keine NVIDIA-GPU."""
        if self._broken:
            return None
        try:
            states = _power_states()
        except Exception as exc:  # noqa: BLE001 - dann wie bisher immer abfragen
            log.info("Energiezustand der GPU nicht lesbar (%s) - frage die Temperatur immer ab", exc)
            self._broken = True
            return None
        known = [s for s in states if s > 0]
        if not known:
            return None
        return any(s == POWER_DEVICE_D0 for s in known)
