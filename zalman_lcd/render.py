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
    return ["%s  %s" % (cpu, gpu), ram]     # CPU+GPU на одной строке, RAM на второй


_MAX_SIZE = 24


def _fixed_size(sensors):
    """Наибольший кегль (<= _MAX_SIZE), при котором «худшие» строки (макс. цифры)
    влезают по ширине. Считается ОДИН РАЗ и кэшируется — поэтому текст НЕ
    сжимается при 6°->10° и т.п."""
    if _fixed_size._cache:
        return _fixed_size._cache
    _, total = sensors.ram_gb()
    tmpl = ["CPU 100% 100°  GPU 100% 100°",
            "RAM %.1f / %.1f GB" % (total or 999.9, total or 999.9)]
    probe = ImageDraw.Draw(Image.new("RGBA", (2, 2)))
    size = _MAX_SIZE
    while size > 10:
        f = _font(size)
        if max(probe.textlength(t, font=f) for t in tmpl) <= SCREEN[0] - 8:
            break
        size -= 1
    _fixed_size._cache = size
    return size


_fixed_size._cache = 0


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
        """RGBA-оверлей: прозрачный фон + 2 строки параметров."""
        lines = _lines(self.sensors)
        img = Image.new("RGBA", SCREEN, (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        f = _font(_fixed_size(self.sensors))    # постоянный размер
        asc, desc = f.getmetrics()
        lh = asc + desc                 # полная высота строки (без приплюснутости)
        pad, gap = 5, 2                 # плотный межстрочный интервал
        barh = pad * 2 + lh * len(lines) + gap * (len(lines) - 1)
        y0 = 0 if self.position == "up" else SCREEN[1] - barh
        # подложка под текстом (30% альфа) — off / white / black
        if self.bg in ("white", "black"):
            col = (255, 255, 255) if self.bg == "white" else (0, 0, 0)
            d.rectangle([0, y0, SCREEN[0], y0 + barh], fill=col + (77,))  # ~30%
        y = y0 + pad
        for t in lines:
            w = d.textlength(t, font=f)
            x = (SCREEN[0] - w) // 2
            # тонкая обводка для контраста на любом фоне
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2),
                           (-2, -2), (2, 2), (-2, 2), (2, -2)):
                d.text((x + dx, y + dy), t, font=f, fill=(0, 0, 0, 230))
            d.text((x, y), t, font=f, fill=self.color + (255,))
            y += lh + gap
        return img


def to_u32(img):
    """PIL RGBA -> numpy uint32 (A<<24)|(R<<16)|(G<<8)|B."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    a = np.asarray(img, dtype=np.uint32)
    R, G, B, A = a[:, :, 0], a[:, :, 1], a[:, :, 2], a[:, :, 3]
    return ((A << 24) | (R << 16) | (G << 8) | B).reshape(-1).astype(np.uint32)
