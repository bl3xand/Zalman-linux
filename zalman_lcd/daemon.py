# -*- coding: utf-8 -*-
"""Демон: фон (JPEG-стрим cmd 0x05) + строка параметров (оверлей cmd 0x07),
как это делает Windows-приложение: оверлей подновляется каждые несколько
кадров фона (иначе очередной кадр фона его перекрывает).
"""

import io
import os
import signal
import time

from PIL import Image, ImageSequence

from . import config as cfgmod
from . import dbg
from . import device
from . import sources
from .render import StatsBar, to_u32
from .sensors import Sensors

_ROT = {0: None, 90: Image.ROTATE_90, 180: Image.ROTATE_180,
        270: Image.ROTATE_270}
KEEPALIVE = 3.0
MAX_FRAMES = 200
STATS_INTERVAL = 1.0        # обновлять статы раз в ~секунду (как Windows)
STALL_ESCALATE = 12         # столько кадров подряд застряло -> reconnect+usb_reset


MAX_JPEG = 14000            # держим кадр в диапазоне Windows (~6..15КБ)


def _jpeg(img, quality=82, max_bytes=MAX_JPEG):
    """Кодирование кадра фона в JPEG, БАЙТ-СТРУКТУРНО как у Windows-приложения.

    Критично: пересобираем картинку через Image.new+paste, чтобы .info было
    ПУСТЫМ. Иначе PIL тащит метаданные исходника (у GIF в info есть 'comment')
    и вставляет в JPEG маркер 0xFE (COM). Аппаратный JPEG-декодер дисплея на
    неожиданном COM-маркере ЗАВИСАЕТ и перестаёт забирать данные с шины.
    Windows такой маркер никогда не шлёт. Также принудительно baseline + 4:2:0,
    без progressive/optimize/EXIF.

    Плюс держим РАЗМЕР кадра в диапазоне Windows (~10КБ): слишком большой JPEG
    дольше декодируется железным декодером и повышает шанс висяка. Снижаем
    quality, пока кадр не влезет в max_bytes (пол — 40)."""
    rgb = img.convert("RGB")
    clean = Image.new("RGB", rgb.size)
    clean.paste(rgb)
    q = quality
    while True:
        b = io.BytesIO()
        clean.save(b, "JPEG", quality=q, subsampling="4:2:0",
                   progressive=False, optimize=False)
        data = b.getvalue()
        if len(data) <= max_bytes or q <= 40:
            return data
        q -= 8


class Daemon:
    def __init__(self, verbose=True):
        self.verbose = verbose
        self.running = True
        self.sensors = Sensors()
        self.cfg = cfgmod.load()
        self._cfg_mtime = cfgmod.mtime()
        self.stats = StatsBar(self.cfg, self.sensors)
        self._prep_key = None
        self._jpegs = None
        self._video = None
        self._fps = 10
        self._animated = False
        self._idx = 0
        self._last_brightness = None
        self._ov_cache = None       # (текст, u32) кэш оверлея
        self._ov_key = None

    def log(self, *a):
        if self.verbose:
            print("[zalman-display]", *a, flush=True)

    def _rot_img(self, img):
        r = _ROT.get(int(self.cfg.get("rotate", 0)) % 360)
        return img.transpose(r) if r is not None else img

    def _prepare(self):
        bg = self.cfg.get("background")
        mt = os.path.getmtime(bg) if bg and os.path.isfile(bg) else 0
        fps = int(self.cfg.get("fps", 20))
        rot = int(self.cfg.get("rotate", 0)) % 360
        key = (bg, mt, fps, rot)
        if key == self._prep_key:
            return
        self._prep_key = key
        if self._video:
            self._video.close()
            self._video = None
        self._jpegs = None
        self._idx = 0
        self._ov_key = None
        ext = os.path.splitext(bg)[1].lower() if bg else ""
        try:
            if bg and ext in sources.VIDEO_EXT:
                self._video = sources.VideoSource(bg, fps=fps)
                self._fps = self._video.fps
                self._animated = True
                self.log("фон: видео", os.path.basename(bg))
            elif bg and os.path.isfile(bg):
                self._jpegs, self._fps = self._encode(bg)
                self._animated = len(self._jpegs) > 1
                self.log("фон:", os.path.basename(bg), "| кадров:",
                         len(self._jpegs))
            else:
                self._jpegs = [_jpeg(Image.new("RGB", (320, 320), (0, 0, 0)))]
                self._fps = 1
                self._animated = False
        except Exception as e:
            self.log("фон не открылся (%s), чёрный" % e)
            self._jpegs = [_jpeg(Image.new("RGB", (320, 320), (0, 0, 0)))]
            self._fps = 1
            self._animated = False
        self._bg_dirty = True

    def _encode(self, path):
        """Ленивое кодирование: кадр -> JPEG по одному, без хранения всех RGB
        (иначе gif на сотни кадров съедает >100МБ RAM)."""
        im = Image.open(path)
        total = getattr(im, "n_frames", 1)
        take = min(total, MAX_FRAMES)
        step = total / take if take else 1
        want = {int(i * step) for i in range(take)}
        jpegs = []
        durs = []
        for idx, fr in enumerate(ImageSequence.Iterator(im)):
            if idx in want:
                jpegs.append(_jpeg(self._rot_img(sources.fit(fr))))
                durs.append(max(20, fr.info.get("duration", 100)))
        if not jpegs:
            jpegs = [_jpeg(Image.new("RGB", (320, 320), (0, 0, 0)))]
        if len(jpegs) > 1 and durs:
            avg = sum(durs) / len(durs) / 1000.0
            fps = max(1, min(30, round(1.0 / avg))) if avg else 15
        else:
            fps = 1
        return jpegs, fps

    def _reload_if_changed(self):
        m = cfgmod.mtime()
        if m != self._cfg_mtime:
            self._cfg_mtime = m
            self.cfg = cfgmod.load()
            self.stats.update(self.cfg)
            self._prepare()
            self._last_brightness = None
            self.log("конфиг перезагружен")

    def _apply_brightness(self, dev):
        b = int(self.cfg.get("brightness", 80))
        if b != self._last_brightness:
            dev.brightness(b)
            self._last_brightness = b

    def _overlay_u32(self):
        """Оверлей строки; перекодируем только когда значения изменились."""
        from .render import _lines as fmt
        ts = time.time()
        lines = tuple(fmt(self.sensors))
        sd = time.time() - ts
        if sd >= 0.2:
            dbg.log("SLOW-SENSORS %.3fs" % sd)
        key = (lines, self.cfg.get("rotate", 0),
               self.cfg.get("text_color"), self.cfg.get("position"))
        if key != self._ov_key:
            self._ov_cache = to_u32(self._rot_img(self.stats.image()))
            self._ov_key = key
        return self._ov_cache

    def _next_bg(self):
        if self._video:
            return _jpeg(self._rot_img(self._video.next()))
        j = self._jpegs[self._idx]
        self._idx = (self._idx + 1) % len(self._jpegs)
        return j

    def run(self):
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        dbg.enable(to_stderr=self.verbose)
        dbg.log("daemon start | %s" % dbg.usb_state())
        fails = 0
        while self.running:
            try:
                self._session()
                fails = 0
            except device.DeviceError as e:
                fails += 1
                self.log("устройство отвалилось (%s)" % e)
                dbg.log("RECONNECT reason=%s fails=%d | %s"
                        % (e, fails, dbg.usb_state()))
                # Первый сбой — просто переоткрыть. Повторный — устройство
                # залипло: аппаратный сброс USB снимает залипание без
                # физического отключения питания.
                if fails >= 2 and device.available():
                    self.log("сброс USB-устройства…")
                    dbg.log("usb_reset attempt (fails=%d)" % fails)
                    if device.usb_reset():
                        p = device.wait_tty(8.0)
                        dbg.log("after reset: tty=%s | %s" % (p, dbg.usb_state()))
                        fails = 0
                    else:
                        self.log("сброс не удался (нет прав на /dev/bus/usb?)")
                self._sleep(0.5)
            except Exception as e:
                self.log("ошибка:", e, "— повтор через 2с")
                dbg.log("UNEXPECTED %s: %s" % (type(e).__name__, e))
                self._sleep(2)
        if self._video:
            self._video.close()
        self.log("остановлен")

    def _session(self):
        frames = 0
        ov_count = 0
        sess_t0 = time.time()
        dev = device.Display()
        self.log("устройство открыто (cdc)")
        try:
            self._prepare()
            self._last_brightness = None
            last_bg = 0.0
            last_ov = 0.0
            hb_t = sess_t0
            hb_frames = 0
            consec = 0          # подряд застрявших кадров
            stalls_total = 0
            dbg.log("session begin: animated=%s fps=%s stats=%s bg_frames=%s "
                    "rss=%.0fMB" % (self._animated, self._fps, self.stats.show,
                                    (len(self._jpegs) if self._jpegs else "vid"),
                                    dbg.rss_mb()))
            while self.running:
                self._reload_if_changed()
                now = time.time()
                # Застрявший кадр НЕ рвём соединение (close добивает декодер в
                # жёсткий висяк). Как Windows: flush TX и продолжаем на том же
                # дескрипторе, пропустив кадр. Только после MANY подряд —
                # эскалация (наверх -> usb_reset + переподключение).
                try:
                    self._apply_brightness(dev)
                    if self._animated:
                        te = time.time()
                        frame = self._next_bg()
                        enc = time.time() - te
                        if enc >= 0.15:
                            dbg.log("SLOW-ENCODE %.3fs size=%d" % (enc, len(frame)))
                        dev.send_jpeg(frame)
                        frames += 1
                        hb_frames += 1
                    elif self._bg_dirty or now - last_bg >= KEEPALIVE:
                        dev.send_jpeg(self._next_bg())
                        self._bg_dirty = False
                        last_bg = now
                        frames += 1
                        hb_frames += 1
                    # статы — раз в ~секунду (как Windows), оверлей держится сам
                    if self.stats.show and now - last_ov >= STATS_INTERVAL:
                        dev.send_overlay(self._overlay_u32())
                        last_ov = now
                        ov_count += 1
                    consec = 0
                except device.DeviceError as e:
                    consec += 1
                    stalls_total += 1
                    dbg.log("frame-stall #%d (total %d) at frame=%d: %s "
                            "-> flush+continue" % (consec, stalls_total, frames, e))
                    dev.flush_tx()
                    if consec >= STALL_ESCALATE:
                        dbg.log("%d подряд застрявших кадров -> эскалация "
                                "(reconnect+reset)" % consec)
                        raise
                    self._sleep(0.05)
                    continue
                # пульс раз в 5с: кадры, fps, память — видно деградацию
                if now - hb_t >= 5.0:
                    dbg.log("hb frames=%d ov=%d fps=%.1f stalls=%d rss=%.0fMB | %s"
                            % (frames, ov_count, hb_frames / (now - hb_t),
                               stalls_total, dbg.rss_mb(), dbg.usb_state()))
                    hb_t = now
                    hb_frames = 0
                self._sleep((1.0 / max(1, self._fps)) if self._animated else 0.25)
        finally:
            dur = time.time() - sess_t0
            dbg.log("session end: frames=%d ov=%d dur=%.1fs avg_fps=%.1f"
                    % (frames, ov_count, dur, frames / dur if dur else 0))
            tc = time.time()
            dev.close()
            dbg.log("closed in %.3fs" % (time.time() - tc))

    def _stop(self, *a):
        self.running = False

    def _sleep(self, dur):
        end = time.time() + dur
        while self.running and time.time() < end:
            time.sleep(min(0.1, end - time.time()))


def run(verbose=True, **kw):
    Daemon(verbose=verbose).run()
