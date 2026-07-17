# -*- coding: utf-8 -*-
"""Диагностическое логирование. Пишет в файл ~/.config/zalman-lcd/zalman.log
(и в stderr/journal при verbose). Цель — поймать ТОЧНЫЙ момент и причину
зависания: какой кадр, какая запись, сколько байт ушло, за сколько времени."""

import os
import sys
import time

_fh = None
_to_stderr = False
_path = None
_written = 0
LOG_PATH = os.path.expanduser("~/.config/zalman-lcd/zalman.log")
MAX_LOG_BYTES = 1_000_000       # кап на файл лога; при превышении — ротация


def enable(to_stderr=True, path=LOG_PATH):
    global _fh, _to_stderr, _path, _written
    _to_stderr = to_stderr
    _path = path
    if _fh is None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _fh = open(path, "a", buffering=1)  # line-buffered
            _written = _fh.tell()
        except OSError:
            _fh = None
    log("=== log started (pid %d) ===" % os.getpid())


def _rotate():
    """Файл лога переполнен — сохраняем одну прошлую копию (.1) и начинаем заново.
    Диск ограничен ~2×MAX_LOG_BYTES."""
    global _fh, _written
    try:
        _fh.close()
        os.replace(_path, _path + ".1")
    except OSError:
        pass
    try:
        _fh = open(_path, "a", buffering=1)
        _written = 0
    except OSError:
        _fh = None


def log(*a):
    if _fh is None and not _to_stderr:
        return
    t = time.time()
    ts = time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % int((t % 1) * 1000)
    msg = ts + " " + " ".join(str(x) for x in a)
    if _fh is not None:
        try:
            _fh.write(msg + "\n")
            global _written
            _written += len(msg) + 1
            if _written >= MAX_LOG_BYTES:
                _rotate()
        except OSError:
            pass
    if _to_stderr:
        print("[zalman]", msg, file=sys.stderr, flush=True)


def rss_mb():
    """Резидентная память процесса, МБ (для отслеживания утечек)."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * (os.sysconf("SC_PAGE_SIZE") / 1048576.0)
    except Exception:
        return -1


def usb_state():
    """runtime_status/control USB-устройства 0483:5740 — что с ним в момент сбоя."""
    import glob
    for d in glob.glob("/sys/bus/usb/devices/*/idProduct"):
        base = os.path.dirname(d)
        try:
            if (open(base + "/idVendor").read().strip() == "0483"
                    and open(d).read().strip() == "5740"):
                st = open(base + "/power/runtime_status").read().strip()
                ctl = open(base + "/power/control").read().strip()
                return "runtime=%s control=%s" % (st, ctl)
        except OSError:
            pass
    return "device-absent"
