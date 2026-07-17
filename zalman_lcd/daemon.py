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
MAX_FRAMES = 360            # флеш-буфер устройства ~6МБ -> ограничиваем набор
MAX_UPLOAD_BYTES = 5_000_000  # суммарно кадров не больше ~5МБ (буфер ~6МБ)
STATS_INTERVAL = 1.0        # раз в секунду: present(0x00) + оверлей (как Windows)
STALL_ESCALATE = 12         # столько кадров подряд застряло -> reconnect+usb_reset
HEARTBEAT = 30.0            # как часто писать строку пульса в лог


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
        self._frames = None         # список JPEG-кадров для заливки в флеш
        self._fps = 10
        self._need_upload = True
        self._last_brightness = None
        self._ov_cache = None       # (текст, u32) кэш оверлея
        self._ov_key = None
        self._blank = None          # прозрачный оверлей для стирания статов

    def log(self, *a):
        if self.verbose:
            print("[zalman-display]", *a, flush=True)

    def _rot_img(self, img):
        r = _ROT.get(int(self.cfg.get("rotate", 0)) % 360)
        return img.transpose(r) if r is not None else img

    def _prepare(self):
        """Собрать набор кадров фона (список JPEG) для ЗАЛИВКИ В ФЛЕШ.
        Меняем self._frames/self._fps и ставим self._need_upload только когда
        фон реально изменился (иначе не перезаливаем)."""
        bg = self.cfg.get("background")
        mt = os.path.getmtime(bg) if bg and os.path.isfile(bg) else 0
        rot = int(self.cfg.get("rotate", 0)) % 360
        key = (bg, mt, rot)
        if key == self._prep_key:
            return
        self._prep_key = key
        self._ov_key = None
        ext = os.path.splitext(bg)[1].lower() if bg else ""
        try:
            if bg and ext in sources.VIDEO_EXT:
                self._frames, self._fps = self._frames_video(bg)
                self.log("фон: видео", os.path.basename(bg), "| кадров:",
                         len(self._frames))
            elif bg and os.path.isfile(bg):
                self._frames, self._fps = self._frames_image(bg)
                self.log("фон:", os.path.basename(bg), "| кадров:",
                         len(self._frames))
            else:
                self._frames = [_jpeg(Image.new("RGB", (320, 320), (0, 0, 0)))]
                self._fps = 1
        except Exception as e:
            self.log("фон не открылся (%s), чёрный" % e)
            self._frames = [_jpeg(Image.new("RGB", (320, 320), (0, 0, 0)))]
            self._fps = 1
        self._need_upload = True

    def _cap_total(self, frames):
        """Не даём набору превысить флеш-буфер (~6МБ) — режем по сумме байт."""
        out, total = [], 0
        for f in frames:
            total += len(f)
            if total > MAX_UPLOAD_BYTES and out:
                self.log("набор обрезан по размеру буфера на %d кадрах" % len(out))
                break
            out.append(f)
        return out

    def _frames_image(self, path):
        """Картинка/GIF -> список JPEG (по одному кадру за раз, без хранения RGB)."""
        jpegs, durs = [], []
        with Image.open(path) as im:
            total = getattr(im, "n_frames", 1)
            take = min(total, MAX_FRAMES)
            step = total / take if take else 1
            want = {int(i * step) for i in range(take)}
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
        return self._cap_total(jpegs), fps

    def _frames_video(self, path):
        """Видео -> список JPEG (до MAX_FRAMES кадров) через ffmpeg."""
        fps = int(self.cfg.get("fps", 20)) or 20
        src = sources.VideoSource(path, fps=fps)
        jpegs = []
        try:
            for _ in range(MAX_FRAMES):
                img = src.next()
                jpegs.append(_jpeg(self._rot_img(img)))
        finally:
            src.close()
        if not jpegs:
            jpegs = [_jpeg(Image.new("RGB", (320, 320), (0, 0, 0)))]
        return self._cap_total(jpegs), fps

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
        """Оверлей строки; перекодируем только когда значения изменились.
        self._ov_key меняется ровно тогда, когда меняется картинка оверлея —
        по нему демон шлёт оверлей лишь при реальном изменении (меньше мерцаний)."""
        from .render import _lines as fmt
        ts = time.time()
        lines = tuple(fmt(self.sensors))
        sd = time.time() - ts
        if sd >= 0.2:
            dbg.log("SLOW-SENSORS %.3fs" % sd)
        key = (lines, self.cfg.get("rotate", 0), self.cfg.get("text_color"),
               self.cfg.get("position"), self.cfg.get("stats_bg"))
        if key != self._ov_key:
            self._ov_cache = to_u32(self._rot_img(self.stats.image()))
            self._ov_key = key
        return self._ov_cache

    def _blank_u32(self):
        """Полностью прозрачный оверлей — стирает текст статов на устройстве."""
        if self._blank is None:
            self._blank = to_u32(Image.new("RGBA", (320, 320), (0, 0, 0, 0)))
        return self._blank

    def _upload(self, dev):
        """Залить фон в флеш устройства: 0x02(fps,count) -> count× 0x05 -> 0x06.
        Дальше устройство само зацикленно проигрывает его из флеша, а мы шлём
        только оверлей статов. Ошибка тут пробрасывается -> run() переподключит
        и повторит заливку (кадры нельзя пропускать — иначе счётчик разъедется)."""
        frames = self._frames or [_jpeg(Image.new("RGB", (320, 320), (0, 0, 0)))]
        total = sum(len(f) for f in frames)
        fps = max(1, min(255, int(self._fps)))
        dbg.log("upload: %d кадров @ %dfps, %d байт" % (len(frames), fps, total))
        dev.video_download(fps, len(frames))
        for f in frames:
            dev.send_jpeg(f)
        dev.video_over()
        self._need_upload = False
        dbg.log("upload done -> устройство проигрывает из флеша")

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
        self.log("остановлен")

    def _session(self):
        ov_count = 0
        sess_t0 = time.time()
        dev = device.Display()
        self.log("устройство открыто (cdc)")
        try:
            self._prepare()
            self._last_brightness = None
            self._apply_brightness(dev)
            # ЗАЛИВАЕМ фон в флеш (как Windows) — дальше устройство крутит его
            # само, непрерывного 0x05 нет, копить нечего -> не виснет.
            self._upload(dev)
            last_ov = 0.0
            hb_t = sess_t0
            hb_frames = 0          # оверлеев за окно пульса
            consec = 0
            stalls_total = 0
            ov_sent = object()     # ключ последнего РЕАЛЬНО отправленного оверлея
            cleared = False        # прозрачный оверлей уже отправлен (статы off)
            dbg.log("session begin: fps=%s stats=%s frames=%d rss=%.0fMB"
                    % (self._fps, self.stats.show,
                       len(self._frames or []), dbg.rss_mb()))
            while self.running:
                self._reload_if_changed()
                # смена фона -> перезалить в флеш
                if self._need_upload:
                    self._apply_brightness(dev)
                    self._upload(dev)
                    ov_sent = object()      # после заливки оверлей нужен заново
                    cleared = False
                now = time.time()
                # На залипании НЕ рвём соединение (close добивает декодер).
                # Как Windows: flush TX и продолжаем на том же дескрипторе.
                try:
                    self._apply_brightness(dev)
                    # раз в секунду: present(0x00) + оверлей — в порядке Windows
                    if now - last_ov >= STATS_INTERVAL:
                        dev.present()                   # 0x00 сначала (как Windows)
                        if self.stats.show:
                            cleared = False
                            # оверлей шлём ТОЛЬКО когда картинка изменилась
                            # (иначе лишние полноэкранные перерисовки -> мерцание)
                            u = self._overlay_u32()
                            if self._ov_key != ov_sent:
                                dev.send_overlay(u)
                                ov_sent = self._ov_key
                                ov_count += 1
                                hb_frames += 1
                        elif not cleared:
                            # статы выключили -> один раз стираем текст
                            dev.send_overlay(self._blank_u32())
                            cleared = True
                            ov_sent = object()
                        last_ov = now
                    consec = 0
                except device.DeviceError as e:
                    consec += 1
                    stalls_total += 1
                    dbg.log("stall #%d (total %d): %s -> flush+continue"
                            % (consec, stalls_total, e))
                    dev.flush_tx()
                    if consec >= STALL_ESCALATE:
                        dbg.log("%d стопоров подряд -> эскалация (reconnect+reset)"
                                % consec)
                        raise
                    self._sleep(0.1)
                    continue
                # пульс в лог (редко — чтобы не забивать диск)
                if now - hb_t >= HEARTBEAT:
                    dbg.log("hb ov=%d stalls=%d rss=%.0fMB | %s"
                            % (hb_frames, stalls_total, dbg.rss_mb(),
                               dbg.usb_state()))
                    hb_t = now
                    hb_frames = 0
                self._sleep(0.2)
        finally:
            dur = time.time() - sess_t0
            dbg.log("session end: ov=%d dur=%.1fs" % (ov_count, dur))
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
