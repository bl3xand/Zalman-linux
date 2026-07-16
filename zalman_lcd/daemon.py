# -*- coding: utf-8 -*-
"""Демон: плавный фон (JPEG-стрим, cmd 0x05) + оверлей статистики (cmd 0x07).

Фон (картинка/gif/видео) кодируется в JPEG и стримится покадрово — сжато,
поэтому плавно. Строка параметров рисуется прозрачным оверлеем поверх и
обновляется ~раз в секунду. Оба слоя поворачиваются вместе.
"""

import io
import signal
import time

from PIL import Image

from . import config as cfgmod
from . import device
from . import sources
from .render import StatsBar, to_u32
from .sensors import Sensors

_ROT = {0: None, 90: Image.ROTATE_90, 180: Image.ROTATE_180,
        270: Image.ROTATE_270}
STATS_INTERVAL = 1.0        # сек между обновлениями строки
KEEPALIVE = 4.0             # переслать статичный фон раз в N сек


def _jpeg(img, quality=82):
    b = io.BytesIO()
    img.convert("RGB").save(b, "JPEG", quality=quality)
    return b.getvalue()


class Daemon:
    def __init__(self, verbose=True):
        self.verbose = verbose
        self.running = True
        self.sensors = Sensors()
        self.cfg = cfgmod.load()
        self._cfg_mtime = cfgmod.mtime()
        self.source = None
        self.stats = StatsBar(self.cfg, self.sensors)
        self._src_key = None
        self._last_brightness = None
        self._bg_dirty = True

    def log(self, *a):
        if self.verbose:
            print("[zalman-display]", *a, flush=True)

    def _ensure_source(self):
        import os
        bg = self.cfg.get("background")
        mt = os.path.getmtime(bg) if bg and os.path.isfile(bg) else 0
        key = (bg, self.cfg.get("fps"), mt)   # mtime -> смена файла => перезагрузка
        if key == self._src_key and self.source is not None:
            return
        if self.source:
            self.source.close()
            self.source = None            # освободить старые кадры из RAM
        try:
            self.source = sources.open_source(self.cfg.get("background"),
                                               fps=int(self.cfg.get("fps", 20)))
        except Exception as e:
            self.log("фон не открылся (%s), чёрный фон" % e)
            self.source = sources.SolidSource()
        self._src_key = key
        self._bg_dirty = True
        self.log("фон:", self.cfg.get("background") or "нет",
                 "| анимация:", self.source.animated)

    def _reload_if_changed(self):
        m = cfgmod.mtime()
        if m != self._cfg_mtime:
            self._cfg_mtime = m
            self.cfg = cfgmod.load()
            self.stats.update(self.cfg)
            self._ensure_source()
            self._last_brightness = None
            self.log("конфиг перезагружен")

    def _rot(self, img):
        r = _ROT.get(int(self.cfg.get("rotate", 0)) % 360)
        return img.transpose(r) if r is not None else img

    def _apply_brightness(self, dev):
        b = int(self.cfg.get("brightness", 80))
        if b != self._last_brightness:
            dev.brightness(b)
            self._last_brightness = b

    def _send_stats(self, dev):
        if not self.cfg.get("show_stats", True):
            return
        ov = self._rot(self.stats.image())
        dev.send_overlay(to_u32(ov))

    def run(self):
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        while self.running:
            try:
                self._session()
            except device.DeviceError as e:
                self.log("устройство недоступно:", e, "— повтор через 3с")
                self._sleep(3)
            except Exception as e:
                self.log("ошибка:", e, "— повтор через 3с")
                self._sleep(3)
        if self.source:
            self.source.close()
        self.log("остановлен")

    def _session(self):
        dev = device.Display()
        self.log("устройство открыто (libusb)")
        try:
            self._ensure_source()
            self._last_brightness = None
            last_stats = 0.0
            last_bg = 0.0
            fps = max(1, min(30, int(getattr(self.source, "fps", 15))))
            while self.running:
                self._reload_if_changed()
                self._apply_brightness(dev)
                now = time.time()
                animated = self.source.animated
                if animated:
                    dev.send_jpeg(_jpeg(self._rot(self.source.next())))
                    last_bg = now
                elif self._bg_dirty or now - last_bg >= KEEPALIVE:
                    dev.send_jpeg(_jpeg(self._rot(self.source.next())))
                    self._bg_dirty = False
                    last_bg = now
                if now - last_stats >= STATS_INTERVAL:
                    self._send_stats(dev)
                    last_stats = now
                fps = max(1, min(30, int(getattr(self.source, "fps", 15))))
                self._sleep((1.0 / fps) if animated else 0.2)
        finally:
            dev.close()

    def _stop(self, *a):
        self.running = False

    def _sleep(self, dur):
        end = time.time() + dur
        while self.running and time.time() < end:
            time.sleep(min(0.1, end - time.time()))


def run(verbose=True, **kw):
    Daemon(verbose=verbose).run()
