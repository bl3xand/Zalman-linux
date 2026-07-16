# -*- coding: utf-8 -*-
"""Съём системных метрик под Linux: CPU/GPU/RAM.

Мягкие зависимости: psutil (желательно), nvidia-smi (NVIDIA), sysfs (AMD).
Если источник недоступен — возвращается None, и метрика показывается как N/A.
"""

import glob
import os
import shutil
import subprocess
import time

try:
    import psutil
except Exception:
    psutil = None


class Sensors:
    def __init__(self):
        self._gpu = _detect_gpu()
        # прогрев cpu_percent (первый вызов возвращает 0)
        if psutil:
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                pass
        self._nvidia_cache = (0.0, None)

    # ---- CPU ----
    def cpu_temp(self):
        if psutil and hasattr(psutil, "sensors_temperatures"):
            try:
                temps = psutil.sensors_temperatures()
            except Exception:
                temps = {}
            for key in ("k10temp", "coretemp", "zenpower", "acpitz"):
                if key in temps and temps[key]:
                    for e in temps[key]:
                        if e.label in ("Tctl", "Tdie", "Package id 0", ""):
                            return round(e.current)
                    return round(temps[key][0].current)
            for arr in temps.values():
                if arr:
                    return round(arr[0].current)
        return _hwmon_temp()

    def cpu_load(self):
        if psutil:
            try:
                return round(psutil.cpu_percent(interval=None))
            except Exception:
                pass
        return None

    def cpu_clock(self):
        if psutil:
            try:
                f = psutil.cpu_freq()
                if f and f.current:
                    return round(f.current)
            except Exception:
                pass
        try:
            vals = []
            for p in glob.glob(
                    "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
                vals.append(int(open(p).read().strip()))
            if vals:
                return round(max(vals) / 1000)
        except Exception:
            pass
        return None

    # ---- RAM ----
    def ram_load(self):
        if psutil:
            try:
                return round(psutil.virtual_memory().percent)
            except Exception:
                pass
        try:
            info = {}
            for line in open("/proc/meminfo"):
                k, v = line.split(":")
                info[k] = int(v.strip().split()[0])
            total = info["MemTotal"]
            avail = info.get("MemAvailable", info["MemFree"])
            return round((total - avail) * 100 / total)
        except Exception:
            return None

    def ram_gb(self):
        """(использовано, всего) в ГБ."""
        if psutil:
            try:
                m = psutil.virtual_memory()
                return ((m.total - m.available) / 2**30, m.total / 2**30)
            except Exception:
                pass
        try:
            info = {}
            for line in open("/proc/meminfo"):
                k, v = line.split(":")
                info[k] = int(v.strip().split()[0])          # kB
            total = info["MemTotal"]
            avail = info.get("MemAvailable", info["MemFree"])
            return ((total - avail) / 2**20, total / 2**20)   # kB -> GB
        except Exception:
            return (0.0, 0.0)

    # ---- GPU ----
    def _nvidia(self):
        now = time.time()
        if now - self._nvidia_cache[0] < 0.5 and self._nvidia_cache[1]:
            return self._nvidia_cache[1]
        try:
            out = subprocess.check_output(
                ["nvidia-smi",
                 "--query-gpu=temperature.gpu,clocks.gr,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                stderr=subprocess.DEVNULL, timeout=2).decode()
            t, clk, load = [x.strip() for x in out.strip().splitlines()[0].split(",")]
            data = {"temp": int(float(t)), "clock": int(float(clk)),
                    "load": int(float(load))}
            self._nvidia_cache = (now, data)
            return data
        except Exception:
            return None

    def gpu_temp(self):
        if self._gpu == "nvidia":
            d = self._nvidia()
            return d["temp"] if d else None
        if self._gpu:
            return _amd_temp(self._gpu)
        return None

    def gpu_clock(self):
        if self._gpu == "nvidia":
            d = self._nvidia()
            return d["clock"] if d else None
        if self._gpu:
            return _amd_clock(self._gpu)
        return None

    def gpu_load(self):
        if self._gpu == "nvidia":
            d = self._nvidia()
            return d["load"] if d else None
        if self._gpu:
            try:
                p = os.path.join(self._gpu, "device", "gpu_busy_percent")
                return int(open(p).read().strip())
            except Exception:
                return None
        return None

    def read(self, metric):
        """Вернуть (значение, единица) для ключа метрики."""
        m = {
            "cpu_temp":  (self.cpu_temp, "°"),
            "cpu_load":  (self.cpu_load, "%"),
            "cpu_clock": (self.cpu_clock, "MHz"),
            "gpu_temp":  (self.gpu_temp, "°"),
            "gpu_load":  (self.gpu_load, "%"),
            "gpu_clock": (self.gpu_clock, "MHz"),
            "ram_load":  (self.ram_load, "%"),
        }
        if metric not in m:
            return (None, "")
        fn, unit = m[metric]
        try:
            return (fn(), unit)
        except Exception:
            return (None, unit)


def _hwmon_temp():
    for p in sorted(glob.glob("/sys/class/hwmon/hwmon*/")):
        try:
            name = open(os.path.join(p, "name")).read().strip()
        except Exception:
            name = ""
        if name in ("k10temp", "coretemp", "zenpower"):
            for t in sorted(glob.glob(os.path.join(p, "temp*_input"))):
                try:
                    return round(int(open(t).read().strip()) / 1000)
                except Exception:
                    continue
    return None


def _detect_gpu():
    if shutil.which("nvidia-smi"):
        return "nvidia"
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        dev = os.path.join(card, "device")
        if glob.glob(os.path.join(dev, "hwmon", "hwmon*", "temp1_input")):
            return card
    return None


def _amd_temp(card):
    for t in glob.glob(os.path.join(card, "device", "hwmon", "hwmon*",
                                    "temp1_input")):
        try:
            return round(int(open(t).read().strip()) / 1000)
        except Exception:
            continue
    return None


def _amd_clock(card):
    p = os.path.join(card, "device", "pp_dpm_sclk")
    try:
        for line in open(p):
            if "*" in line:                     # активный уровень
                mhz = line.split(":")[1].strip().split("Mhz")[0]
                return int(mhz)
    except Exception:
        pass
    return None
