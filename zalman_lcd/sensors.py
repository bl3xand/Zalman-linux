# -*- coding: utf-8 -*-
"""Съём системных метрик под Linux: CPU/GPU/RAM.

Мягкие зависимости: psutil (желательно), nvidia-smi (NVIDIA), sysfs (AMD).
Если источник недоступен — возвращается None, и метрика показывается как N/A.
"""

import glob
import os
import re
import shutil
import subprocess
import time

try:
    import psutil
except Exception:
    psutil = None


class Sensors:
    def __init__(self, prefer="auto"):
        self._gpu = _detect_gpu(prefer)
        # прогрев cpu_percent (первый вызов возвращает 0)
        if psutil:
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                pass
        self._nvidia_cache = (0.0, None)

    def retarget(self, prefer):
        """Переключить источник GPU (по выбору пользователя)."""
        self._gpu = _detect_gpu(prefer)
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
            return _drm_temp(self._gpu)
        return None

    def gpu_clock(self):
        if self._gpu == "nvidia":
            d = self._nvidia()
            return d["clock"] if d else None
        if self._gpu:
            return _drm_clock(self._gpu)
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


def _detect_gpu(prefer="auto"):
    """Вернуть 'nvidia' или путь /sys/class/drm/cardN. prefer — id из list_gpus()
    ('nvidia'/'card0'/'card1'/…) либо 'auto'. Неверный prefer -> авто."""
    prefer = prefer or "auto"
    if prefer != "auto":
        if prefer == "nvidia" and shutil.which("nvidia-smi"):
            return "nvidia"
        p = os.path.join("/sys/class/drm", prefer)
        if prefer.startswith("card") and os.path.isdir(p):
            return p
        # неверный выбор — падаем в авто
    if shutil.which("nvidia-smi"):
        return "nvidia"
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        dev = os.path.join(card, "device")
        if glob.glob(os.path.join(dev, "hwmon", "hwmon*", "temp1_input")):
            return card
    return None


def _card_pci(card):
    """PCI-адрес карты, напр. '0000:03:00.0'."""
    try:
        return os.path.basename(os.path.realpath(os.path.join(card, "device")))
    except OSError:
        return None


def _vulkan_names():
    """{pci_addr: чистое имя} из vulkaninfo — как в GNOME/Mission Center
    ('AMD Radeon RX 9070 XT'). Пусто, если vulkaninfo нет. Универсально
    (AMD/Intel/NVIDIA)."""
    if not shutil.which("vulkaninfo"):
        return {}
    try:
        out = subprocess.check_output(["vulkaninfo"], stderr=subprocess.DEVNULL,
                                      timeout=6).decode(errors="replace")
    except Exception:
        return {}
    res, name, pci = {}, None, {}
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("deviceName"):
            name = re.sub(r"\s*\(.*\)\s*$", "", s.split("=", 1)[1].strip())
            pci = {}
        elif "=" in s and s.split("=")[0].strip() in (
                "pciDomain", "pciBus", "pciDevice", "pciFunction"):
            try:
                pci[s.split("=")[0].strip()] = int(s.split("=")[1])
            except ValueError:
                pass
            if len(pci) == 4 and name:
                addr = "%04x:%02x:%02x.%x" % (pci["pciDomain"], pci["pciBus"],
                                              pci["pciDevice"], pci["pciFunction"])
                res.setdefault(addr, name)
    return res


def _lspci_name(addr):
    """Имя из lspci (fallback), напр. 'Radeon RX 9070/9070 XT/9070 GRE'."""
    if not addr or not shutil.which("lspci"):
        return None
    try:
        out = subprocess.check_output(["lspci", "-s", addr],
                                      stderr=subprocess.DEVNULL,
                                      timeout=2).decode(errors="replace")
        desc = out.strip().splitlines()[0].split("controller:", 1)[-1]
        desc = desc.split(":", 1)[-1].strip()
        br = re.findall(r"\[([^\]]+)\]", desc)     # маркетинговое имя в скобках
        name = br[-1] if br else desc
        return re.sub(r"\s*\(rev [0-9a-f]+\)\s*$", "", name).strip() or None
    except Exception:
        return None


def list_gpus():
    """Список доступных GPU для выбора: [(id, человекочитаемая метка)].
    id: 'nvidia' или 'cardN'. Имя берём как в системе: vulkaninfo (то же, что
    показывает GNOME/Mission Center) -> lspci -> драйвер. + текущая температура."""
    out = []
    if shutil.which("nvidia-smi"):
        out.append(("nvidia", "NVIDIA (nvidia-smi)"))
    vk = _vulkan_names()
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        cid = os.path.basename(card)
        addr = _card_pci(card)
        name = vk.get(addr) or _lspci_name(addr)
        if not name:
            try:
                for line in open(os.path.join(card, "device", "uevent")):
                    if line.startswith("DRIVER="):
                        name = line.split("=", 1)[1].strip()
                        break
            except OSError:
                name = "?"
        temp = None
        for t in glob.glob(os.path.join(card, "device", "hwmon", "hwmon*",
                                        "temp1_input")):
            try:
                temp = round(int(open(t).read().strip()) / 1000)
                break
            except Exception:
                pass
        label = "%s%s" % (name, "" if temp is None else "  (%d°C now)" % temp)
        out.append((cid, label))
    return out


def _drm_temp(card):
    for t in glob.glob(os.path.join(card, "device", "hwmon", "hwmon*",
                                    "temp1_input")):
        try:
            return round(int(open(t).read().strip()) / 1000)
        except Exception:
            continue
    return None


def _drm_clock(card):
    p = os.path.join(card, "device", "pp_dpm_sclk")
    try:
        for line in open(p):
            if "*" in line:                     # активный уровень
                mhz = line.split(":")[1].strip().split("Mhz")[0]
                return int(mhz)
    except Exception:
        pass
    return None
