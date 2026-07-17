# -*- coding: utf-8 -*-
"""Оверлей системных параметров (прозрачный слой поверх фона, cmd 0x07).

Две строки:
    CPU 54% 63°   GPU 92% 71°
    RAM 12.4 / 32.0 GB
"""

import os
import shutil
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont

SCREEN = (320, 320)


def _find_font_file():
    # 1) вложенный в проект JetBrains Mono Bold (моноширинный, OFL) — есть всегда
    bundled = os.path.join(os.path.dirname(__file__), "fonts",
                           "JetBrainsMono-Bold.ttf")
    if os.path.isfile(bundled):
        return bundled
    # 2) фолбэк на системные шрифты
    for c in ("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/liberation/LiberationSans-Bold.ttf"):
        if os.path.isfile(c):
            return c
    if shutil.which("fc-match"):
        try:
            out = subprocess.check_output(["fc-match", "-f", "%{file}",
                                           "sans:bold"],
                                          stderr=subprocess.DEVNULL,
                                          timeout=2).decode().strip()
            if out and os.path.isfile(out):
                return out
        except Exception:
            pass
    return None


_FONT_FILE = _find_font_file()
_font_cache = {}


def _font(px):
    if px not in _font_cache:
        _font_cache[px] = (ImageFont.truetype(_FONT_FILE, px) if _FONT_FILE
                           else ImageFont.load_default())
    return _font_cache[px]


def hex_color(s, default=(255, 255, 255)):
    s = (s or "").lstrip("#")
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return default


def _lines(sensors):
    ct, cl = sensors.cpu_temp(), sensors.cpu_load()
    gt, gl = sensors.gpu_temp(), sensors.gpu_load()
    used, total = sensors.ram_gb()
    cpu = "CPU %s%% %s" % (cl if cl is not None else "--",
                          "--" if ct is None else "%d°" % ct)
    gpu = "GPU %s%% %s" % (gl if gl is not None else "--",
                          "--" if gt is None else "%d°" % gt)
    ram = "RAM %.1f / %.1f GB" % (used, total) if total else "RAM --"
    return ["%s %s" % (cpu, gpu), ram]      # CPU+GPU на одной строке, RAM на второй


_MAX_SIZE = 28


def _sizes(sensors):
    """(размер строки CPU/GPU, размер строки RAM). Кегль считается по «худшему»
    тексту с 3 цифрами (100% 999°) и кэшируется -> текст НИКОГДА не сжимается
    от смены значений. CPU/GPU растянуты под ширину (крупнее), RAM чуть меньше."""
    if _sizes._cache:
        return _sizes._cache
    _, total = sensors.ram_gb()
    top_t = "CPU 100% 999° GPU 100% 999°"       # худший случай: 3 цифры
    ram_t = "RAM %.1f / %.1f GB" % (total or 999.9, total or 999.9)
    probe = ImageDraw.Draw(Image.new("RGBA", (2, 2)))

    def fit(s):
        sz = _MAX_SIZE
        while sz > 10 and probe.textlength(s, font=_font(sz)) > SCREEN[0] - 6:
            sz -= 1
        return sz

    top = fit(top_t)
    ram = min(fit(ram_t), top - 1)              # RAM чуть меньше CPU/GPU
    _sizes._cache = (top, ram)
    return _sizes._cache


_sizes._cache = None


class StatsBar:
    def __init__(self, cfg, sensors):
        self.sensors = sensors
        self.update(cfg)

    def update(self, cfg):
        self.color = hex_color(cfg.get("text_color", "FFFFFF"))
        self.position = cfg.get("position", "down")
        self.show = bool(cfg.get("show_stats", True))
        self.bg = cfg.get("stats_bg", "off")        # off / white / black

    def image(self):
        """RGBA-оверлей: прозрачный фон + строки параметров.
        Строка CPU/GPU — крупнее, RAM — чуть меньше; межстрочный минимальный."""
        lines = _lines(self.sensors)
        top_sz, ram_sz = _sizes(self.sensors)
        fonts = [_font(top_sz)] + [_font(ram_sz)] * (len(lines) - 1)
        heights = [sum(fn.getmetrics()) for fn in fonts]
        pad, gap = 3, 1                 # минимальный межстрочный
        barh = pad * 2 + sum(heights) + gap * (len(lines) - 1)
        y0 = 0 if self.position == "up" else SCREEN[1] - barh
        img = Image.new("RGBA", SCREEN, (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if self.bg in ("white", "black"):       # подложка 30% альфа
            col = (255, 255, 255) if self.bg == "white" else (0, 0, 0)
            d.rectangle([0, y0, SCREEN[0], y0 + barh], fill=col + (77,))
        y = y0 + pad
        for t, fn, h in zip(lines, fonts, heights):
            w = d.textlength(t, font=fn)
            x = (SCREEN[0] - w) // 2
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2),      # обводка
                           (-2, -2), (2, 2), (-2, 2), (2, -2)):
                d.text((x + dx, y + dy), t, font=fn, fill=(0, 0, 0, 230))
            d.text((x, y), t, font=fn, fill=self.color + (255,))
            y += h + gap
        return img


def to_u32(img):
    """PIL RGBA -> numpy uint32 (A<<24)|(R<<16)|(G<<8)|B."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    a = np.asarray(img, dtype=np.uint32)
    R, G, B, A = a[:, :, 0], a[:, :, 1], a[:, :, 2], a[:, :, 3]
    return ((A << 24) | (R << 16) | (G << 8) | B).reshape(-1).astype(np.uint32)
