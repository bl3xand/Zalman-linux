# -*- coding: utf-8 -*-
"""Драйвер дисплея Zalman Alpha 2 — «живой» режим (SetDisplayInfo, cmd 0x07).

Протокол восстановлен реверсом LcdComm.dll + USB-дампом (см. PROTOCOL.md).

Транспорт: libusb напрямую в bulk OUT ep 0x02 (в обход cdc_acm).
Устройство: USB CDC "USB Display", VID 0x0483 / PID 0x5740.

Кадр (три отдельные bulk-передачи):
  1) 16 байт 0x00                       — очистка/синхронизация кадрового буфера
  2) 16 байт заголовок:
       [07, sub, 0, 0] + clen(u32 LE) + x(u16) y(u16) w(u16) h(u16)
     sub чередует 0x03/0x04; x=y=0, w=h=320; clen — длина тела (с терминатором)
  3) тело: RLE по пикселям BGRA + терминатор 0x00000000

Пиксель = (A<<24)|(R<<16)|(G<<8)|B. A — АЛЬФА: 0xFF непрозрачный, 0x00 прозрачный
(устройство альфа-накладывает кадр на фон-тему). RLE-токены:
  RUN     0x02000000|count  + 1 пиксель (повторить count раз)
  LITERAL 0x01000000|count  + count пикселей
"""

import ctypes as C
import glob
import os
import random
import struct
import time

import numpy as np

VID, PID = 0x0483, 0x5740
EP_OUT = 0x02
EP_IN = 0x82
IFACE = 1
W = H = 320
N = W * H

CMD_ROTATE = 0x08


class DeviceError(Exception):
    pass


# ---------------------------------------------------------------------------
# libusb через ctypes (без сторонних пакетов)
# ---------------------------------------------------------------------------
def _load_libusb():
    for name in ("libusb-1.0.so.0", "libusb-1.0.so", "libusb.so"):
        try:
            return C.CDLL(name)
        except OSError:
            continue
    raise DeviceError("libusb-1.0 не найдена (установите libusb)")


_lib = None


def _lib_init():
    global _lib
    if _lib is not None:
        return _lib
    lib = _load_libusb()
    lib.libusb_open_device_with_vid_pid.restype = C.c_void_p
    lib.libusb_open_device_with_vid_pid.argtypes = [C.c_void_p, C.c_uint16,
                                                    C.c_uint16]
    lib.libusb_bulk_transfer.argtypes = [C.c_void_p, C.c_ubyte, C.c_void_p,
                                         C.c_int, C.POINTER(C.c_int), C.c_uint]
    _lib = lib
    return lib


def available():
    """Есть ли устройство на шине (по sysfs)."""
    for d in glob.glob("/sys/bus/usb/devices/*/idProduct"):
        base = os.path.dirname(d)
        try:
            if (open(os.path.join(base, "idVendor")).read().strip() == "0483"
                    and open(d).read().strip() == "5740"):
                return True
        except OSError:
            continue
    return False


class Display:
    def __init__(self):
        self.lib = _lib_init()
        self.ctx = C.c_void_p()
        if self.lib.libusb_init(C.byref(self.ctx)) != 0:
            raise DeviceError("libusb_init fail")
        h = self.lib.libusb_open_device_with_vid_pid(self.ctx, VID, PID)
        if not h:
            raise DeviceError(
                "дисплей 0483:5740 не открылся (подключён? права на "
                "/dev/bus/usb/*? см. udev-правило)")
        self.h = C.c_void_p(h)
        # авто-открепление ядрового драйвера (cdc_acm) + захват интерфейса
        self.lib.libusb_set_auto_detach_kernel_driver(self.h, 1)
        for i in (0, 1):
            if self.lib.libusb_kernel_driver_active(self.h, i) == 1:
                self.lib.libusb_detach_kernel_driver(self.h, i)
        if self.lib.libusb_claim_interface(self.h, IFACE) != 0:
            raise DeviceError("не удалось захватить интерфейс")
        self._sub = 3
        self.wake()

    def _read(self, length=64, timeout=300):
        buf = (C.c_ubyte * length)()
        n = C.c_int(0)
        r = self.lib.libusb_bulk_transfer(self.h, EP_IN, buf, length,
                                          C.byref(n), timeout)
        return bytes(buf[:n.value]) if r == 0 else b""

    def wake(self):
        """Verify-хэндшейк — будит экран после холодного старта (тёмный экран)."""
        try:
            self._bulk(b"\x01HWCX-TECH-VRFY0"); self._read(); time.sleep(0.02)
            self._bulk(b"\x01HWCX-TECH-VRFY1"); self._read(); time.sleep(0.02)
            b = bytearray(16); b[0] = 1
            for k in range(1, 16):
                b[k] = random.randint(0, 255)
            b[8] = ((b[5] + b[6]) & 0xFF) ^ b[7]
            b[4] = ((b[2] + b[3]) & 0xFF) ^ b[1]
            b[9] = (~b[10]) & 0xFF
            b[11] = (-(sum(b[1:11]) & 0xFF)) & 0xFF
            self._bulk(bytes(b)); self._read()
        except DeviceError:
            pass

    def _bulk(self, data, timeout=5000):
        # большой таймаут: НЕЛЬЗЯ прерывать передачу посреди кадра (иначе
        # недосыл -> рассинхрон -> залипание endpoint -> нужен power cycle)
        buf = (C.c_ubyte * len(data)).from_buffer_copy(data)
        n = C.c_int(0)
        r = self.lib.libusb_bulk_transfer(self.h, EP_OUT, buf, len(data),
                                          C.byref(n), timeout)
        if r != 0:
            raise DeviceError("bulk_transfer error %d" % r)
        return n.value

    def send_jpeg(self, jpg):
        """Кадр ФОНА: JPEG через cmd 0x05 (сжато ~10КБ -> плавно).
        header [05,00,00,00, len32, 0×8] + JPEG + добивка нулями до 64."""
        pad = (-len(jpg)) % 64
        self._bulk(bytes([0x05, 0, 0, 0]) + struct.pack("<I", len(jpg))
                   + b"\x00" * 8)
        self._bulk(jpg + b"\x00" * pad)

    def send_overlay(self, u32):
        """Слой ОВЕРЛЕЯ: cmd 0x07 (RLE + альфа). Прозрачные пиксели (A=0)
        пропускают фон, непрозрачные (A=0xFF) рисуются поверх. Держится и
        композитится над кадрами фона."""
        body = _rle(u32) + b"\x00\x00\x00\x00"
        self._send(body, 0x03)

    # --- кадр ---
    def _send(self, body, sub):
        clen = len(body)
        header = bytes([0x07, sub, 0, 0]) + struct.pack("<I", clen) \
            + struct.pack("<HHHH", 0, 0, W, H)
        self._bulk(header)
        off = 0
        while off < clen:
            self._bulk(body[off:off + 64])
            off += 64

    def brightness(self, value, rotate=0xFF):
        """Яркость 0..100 (0xFF = не менять); rotate 0..3 или 0xFF."""
        v = 0xFF if value is None else (0xFF if value > 100
                                        else (value * 90) // 100 + 10)
        self._bulk(bytes([CMD_ROTATE, rotate & 0xFF, v & 0xFF]) + b"\x00" * 13)

    def set_rotate(self, rotate):
        self._bulk(bytes([CMD_ROTATE, rotate & 0xFF, 0xFF]) + b"\x00" * 13)

    def close(self):
        try:
            self.lib.libusb_release_interface(self.h, IFACE)
            self.lib.libusb_attach_kernel_driver(self.h, IFACE)
            self.lib.libusb_close(self.h)
            self.lib.libusb_exit(self.ctx)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ---------------------------------------------------------------------------
# RLE-энкодер (совпадает байт-в-байт с оригиналом)
# ---------------------------------------------------------------------------
def _rle(a):
    out = bytearray()
    ne = np.nonzero(a[1:] != a[:-1])[0] + 1
    bounds = np.concatenate(([0], ne, [len(a)]))
    starts = bounds[:-1]
    runs = np.diff(bounds)
    k = 0
    m = len(starts)
    while k < m:
        st = int(starts[k])
        run = int(runs[k])
        if run >= 5:
            out += struct.pack("<II", 0x02000000 | run, int(a[st]))
            k += 1
        else:
            first = st
            total = 0
            while k < m and int(runs[k]) < 5:
                total += int(runs[k])
                k += 1
            out += struct.pack("<I", 0x01000000 | total)
            out += a[first:first + total].tobytes()
    return bytes(out)


# ---------------------------------------------------------------------------
# Изображение -> пиксельный буфер
# ---------------------------------------------------------------------------
def rgba_to_u32(img):
    """PIL RGBA (или RGB) 320x320 -> numpy uint32[N] = (A<<24)|(R<<16)|(G<<8)|B.

    RGB трактуется как полностью непрозрачный (A=0xFF)."""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    a = np.asarray(img, dtype=np.uint32)          # HxWx4
    R, G, B, A = a[:, :, 0], a[:, :, 1], a[:, :, 2], a[:, :, 3]
    return ((A << 24) | (R << 16) | (G << 8) | B).reshape(-1).astype(np.uint32)
