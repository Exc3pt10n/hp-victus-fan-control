"""Programmsymbol und Tray-Icon (mit Temperaturanzeige), gezeichnet mit Pillow."""

from __future__ import annotations

from .theme import ACCENT, OFF, temp_color

# --------------------------------------------------------------------------
# Tray-Icon zeichnen
# --------------------------------------------------------------------------
def _font(size: int):
    from PIL import ImageFont

    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def make_fan_image(size: int = 64, color: str = ACCENT):
    """Lüftersymbol (für Fenster und Tray ohne Messwerte)."""
    from PIL import Image, ImageDraw

    s = size * 4  # überabtasten für glatte Kanten
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=color)
    c, r = s / 2, s * 0.36
    for angle in (0, 90, 180, 270):
        d.pieslice((c - r, c - r, c + r, c + r), angle + 10, angle + 70, fill="white")
    hub = s * 0.11
    d.ellipse((c - hub, c - hub, c + hub, c + hub), fill=color, outline="white", width=s // 32)
    return img.resize((size, size), Image.LANCZOS)


def make_tray_image(temp: float | None, mode: str, running: bool):
    """Tray-Icon: höchste Temperatur (CPU/GPU) als Zahl auf farbigem Grund.

    Grün < 70 °C, Orange < 85 °C, Rot darüber; grau = pausiert/kein Dienst.
    """
    from PIL import Image, ImageDraw

    if not running or temp is None:
        return make_fan_image(64, OFF)
    color = OFF if mode == "paused" else temp_color(temp)
    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=48, fill=color)
    text = str(min(99, round(temp)))
    font = _font(190)
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((s - w) / 2 - box[0], (s - h) / 2 - box[1]), text, font=font, fill="white")
    return img.resize((64, 64), Image.LANCZOS)


