"""Eigene tkinter-Widgets: Karte, Umschaltfeld, Schalter, Messwert und Verlauf."""

from __future__ import annotations

import time
import tkinter as tk
from collections import deque

from .theme import C, CPU_COLOR, FAN_COLOR, FONT, GPU_COLOR, fmt_temp

HISTORY_SECONDS = 600

# --------------------------------------------------------------------------
# Bausteine
# --------------------------------------------------------------------------
class Card(tk.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, bg=C.CARD, highlightbackground=C.BORDER, highlightthickness=1, **kw)


class Segmented(tk.Frame):
    """Umschaltfeld im Stil von Windows 11: eine Leiste, die gewählte Option hervorgehoben."""

    def __init__(self, master, command):
        super().__init__(master, bg=C.TRACK, padx=3, pady=3)
        self.command = command
        self.value: str | None = None
        self.enabled = True
        self.items: dict[str, tk.Label] = {}

    def set_options(self, options: list[tuple[str, str]]) -> None:
        if [v for v, _ in options] == list(self.items):
            return
        for w in self.items.values():
            w.destroy()
        self.items = {}
        for value, label in options:
            w = tk.Label(self, text=label, font=(FONT, 9), padx=12, pady=5, cursor="hand2")
            w.pack(side="left", padx=(0, 2))
            w.bind("<Button-1>", lambda _e, v=value: self.enabled and self.command(v))
            w.bind("<Enter>", lambda _e, v=value: self._paint(hover=v))
            w.bind("<Leave>", lambda _e: self._paint())
            self.items[value] = w
        self._paint()

    def set(self, value: str, enabled: bool = True) -> None:
        if value != self.value or enabled != self.enabled:
            self.value, self.enabled = value, enabled
            self._paint()

    def _paint(self, hover: str | None = None) -> None:
        for value, w in self.items.items():
            if value == self.value:
                bg = C.SELECTED if self.enabled else C.HOVER
                fg = C.SELECTED_TEXT if self.enabled else C.MUTED
            else:
                bg = C.HOVER if value == hover and self.enabled else C.TRACK
                fg = C.TEXT if self.enabled else C.MUTED
            w.config(bg=bg, fg=fg, activebackground=bg, cursor="hand2" if self.enabled else "arrow")


class Switch(tk.Canvas):
    """Ein/Aus-Schalter im Stil von Windows 11."""

    W, H = 44, 22

    def __init__(self, master, command, bg: str):
        super().__init__(master, width=self.W, height=self.H, bg=bg, highlightthickness=0, cursor="hand2")
        self.command = command
        self.on = True
        self.bind("<Button-1>", lambda _e: self.command(not self.on))
        self._draw()

    def set(self, on: bool) -> None:
        if on != self.on:
            self.on = on
            self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h, r = self.W, self.H, self.H / 2
        fill = C.SELECTED if self.on else C.TRACK
        outline = C.SELECTED if self.on else C.MUTED
        self.create_oval(1, 1, h - 1, h - 1, fill=fill, outline=outline)
        self.create_oval(w - h + 1, 1, w - 1, h - 1, fill=fill, outline=outline)
        self.create_rectangle(r, 1, w - r, h - 1, fill=fill, outline="")
        self.create_line(r, 1, w - r, 1, fill=outline)
        self.create_line(r, h - 1, w - r, h - 1, fill=outline)
        knob = 5
        x = w - r if self.on else r
        self.create_oval(x - r + knob, knob, x + r - knob, h - knob,
                         fill=C.SELECTED_TEXT if self.on else C.MUTED, outline="")


class Metric(Card):
    """Kachel: Titel, großer Wert, optional Balken, kleine Zeile."""

    def __init__(self, master, title: str, accent: str, bar: bool = False):
        super().__init__(master)
        head = tk.Frame(self, bg=C.CARD)
        head.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(head, text="●", bg=C.CARD, fg=accent, font=(FONT, 8)).pack(side="left")
        tk.Label(head, text=title, bg=C.CARD, fg=C.MUTED, font=(FONT, 9)).pack(side="left", padx=(4, 0))
        self.value = tk.Label(self, text="–", bg=C.CARD, fg=C.TEXT, font=(FONT, 20, "bold"), anchor="w")
        self.value.pack(fill="x", padx=12)
        self.bar = None
        if bar:
            self.bar = tk.Canvas(self, height=6, width=40, bg=C.CARD, highlightthickness=0)
            self.bar.pack(fill="x", padx=12, pady=(2, 0))
            self.bar.bind("<Configure>", lambda _e: self._draw_bar())
        self.sub = tk.Label(self, text=" ", bg=C.CARD, fg=C.MUTED, font=(FONT, 8), anchor="w")
        self.sub.pack(fill="x", padx=12, pady=(2, 10))
        self._fraction = 0.0
        self._target: float | None = None
        self._bar_color = accent

    def set(self, value: str, sub: str = " ", color: str | None = None,
            fraction: float | None = None, target: float | None = None) -> None:
        self.value.config(text=value, fg=color or C.TEXT)
        self.sub.config(text=sub)
        if self.bar is not None:
            self._fraction = max(0.0, min(1.0, fraction or 0.0))
            self._target = None if target is None else max(0.0, min(1.0, target))
            self._draw_bar()

    def _draw_bar(self) -> None:
        if self.bar is None:
            return
        c, w = self.bar, self.bar.winfo_width()
        c.delete("all")
        if w < 10:
            return
        c.create_rectangle(0, 0, w, 6, fill=C.TRACK, width=0)
        if self._fraction > 0:
            c.create_rectangle(0, 0, max(3, w * self._fraction), 6, fill=self._bar_color, width=0)
        if self._target is not None:      # Markierung: Zielwert der Steuerung
            x = min(w - 1, max(1, w * self._target))
            c.create_line(x, 0, x, 6, fill=C.TEXT, width=2)


class Chart(tk.Canvas):
    """Verlauf der letzten 10 Minuten: CPU, GPU (°C) und Lüfter (% der Maximaldrehzahl).

    Mit der Maus darüber: Messpunkt mit Uhrzeit, Werten und Lüfterprofil.
    Gestrichelte Linien markieren, wann das Profil gewechselt hat.
    """

    PAD_L, PAD_R, PAD_T, PAD_B = 30, 8, 8, 18
    SERIES = (("fan", FAN_COLOR), ("gpu", GPU_COLOR), ("cpu", CPU_COLOR))

    def __init__(self, master, history: deque):
        super().__init__(master, bg=C.CARD, height=150, width=300, highlightthickness=0)
        self.history = history
        self.hover_x: int | None = None
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)

    def _on_motion(self, event) -> None:
        self.hover_x = event.x
        self.redraw()

    def _on_leave(self, _event) -> None:
        self.hover_x = None
        self.redraw()

    def _xy(self, t, v, now, w, h):
        pw, ph = w - self.PAD_L - self.PAD_R, h - self.PAD_T - self.PAD_B
        return (self.PAD_L + pw * (1 - (now - t) / HISTORY_SECONDS),
                self.PAD_T + ph * (1 - max(0, min(100, v)) / 100))

    def _visible(self, now: float) -> list[dict]:
        return [e for e in self.history if now - e["t"] <= HISTORY_SECONDS]

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50:
            return
        now = time.time()
        entries = self._visible(now)
        for v in (0, 50, 100):
            _, y = self._xy(now, v, now, w, h)
            self.create_line(self.PAD_L, y, w - self.PAD_R, y, fill=C.BORDER)
            self.create_text(self.PAD_L - 6, y, text=str(v), anchor="e", fill=C.MUTED, font=(FONT, 8))
        for minutes, anchor in ((10, "sw"), (5, "s"), (0, "se")):
            x, _ = self._xy(now - minutes * 60, 0, now, w, h)
            self.create_text(x, h - 2, text="jetzt" if minutes == 0 else f"−{minutes} min",
                             anchor=anchor, fill=C.MUTED, font=(FONT, 8))

        # Wechsel des Lüfterprofils: gestrichelte senkrechte Linie
        for prev, cur in zip(entries, entries[1:]):
            if prev["fan_label"] != cur["fan_label"]:
                x, _ = self._xy(cur["t"], 0, now, w, h)
                self.create_line(x, self.PAD_T, x, h - self.PAD_B, fill=C.MUTED, dash=(2, 3))

        drawn = False
        for key, color in self.SERIES:
            points = []
            for e in entries:
                if e[key] is not None:
                    points.extend(self._xy(e["t"], e[key], now, w, h))
            if len(points) >= 4:
                self.create_line(*points, fill=color, width=2, smooth=True)
                drawn = True
        if not drawn:
            self.create_text(w / 2, h / 2 - 6, text="Verlauf erscheint nach einigen Sekunden",
                             fill=C.MUTED, font=(FONT, 9))
            return
        if self.hover_x is not None and self.PAD_L <= self.hover_x <= w - self.PAD_R:
            self._draw_hover(entries, now, w, h)

    def _draw_hover(self, entries: list[dict], now: float, w: int, h: int) -> None:
        nearest = min(entries, key=lambda e: abs(self._xy(e["t"], 0, now, w, h)[0] - self.hover_x))
        x, _ = self._xy(nearest["t"], 0, now, w, h)
        self.create_line(x, self.PAD_T, x, h - self.PAD_B, fill=C.TEXT, width=1)
        for key, color in self.SERIES:
            if nearest[key] is not None:
                _, y = self._xy(nearest["t"], nearest[key], now, w, h)
                self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline=C.CARD, width=2)

        lines = [time.strftime("%H:%M:%S", time.localtime(nearest["t"])),
                 f"CPU {fmt_temp(nearest['cpu'])} · GPU {fmt_temp(nearest['gpu'])}"]
        rpm = nearest.get("rpm") or []
        fan = f"{nearest['fan']:.0f} %" if nearest["fan"] is not None else "–"
        lines.append(f"Lüfter {fan}" + (f" · {' / '.join(f'{r:,}'.replace(',', '.') for r in rpm)} U/min"
                                        if rpm else ""))
        lines.append(f"Profil: {nearest['fan_label']}")

        # Infofeld neben der Linie, auf der Seite mit mehr Platz: Uhrzeit fett, darunter die Werte
        head = self.create_text(0, 0, text=lines[0], anchor="nw", fill=C.TEXT, font=(FONT, 8, "bold"))
        body = self.create_text(0, 0, text="\n".join(lines[1:]), anchor="nw", fill=C.TEXT,
                                font=(FONT, 8), justify="left")
        hx1, hy1, hx2, hy2 = self.bbox(head)
        bx1, by1, bx2, by2 = self.bbox(body)
        bw = max(hx2 - hx1, bx2 - bx1) + 16
        bh = (hy2 - hy1) + 2 + (by2 - by1) + 12
        left = x + 10 if x + 10 + bw <= w - self.PAD_R else x - 10 - bw
        top = self.PAD_T
        self.create_rectangle(left, top, left + bw, top + bh, fill=C.TRACK, outline=C.BORDER)
        self.coords(head, left + 8, top + 6)
        self.coords(body, left + 8, top + 6 + (hy2 - hy1) + 2)
        self.tag_raise(head)
        self.tag_raise(body)


