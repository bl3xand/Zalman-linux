# -*- coding: utf-8 -*-
"""CLI для zalman-display: флаги для скриптов + интерактивное меню без аргументов."""

import argparse
import os
import subprocess
import sys

from . import config as cfgmod
from . import device

SERVICE = "zalman-display.service"
USER_UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
UDEV_RULE = ('SUBSYSTEM=="usb", ATTRS{idVendor}=="0483", '
             'ATTRS{idProduct}=="5740", MODE="0666"\n')
ROTATIONS = (0, 90, 180, 270)


# ------------------------- применение настроек -------------------------
def set_background(path):
    if path is None or str(path).lower() in ("none", "off", ""):
        cfgmod.clear_background_cache()
        cfgmod.update(background=None)
        print("Фон убран (чёрный), кэш очищен.")
        return
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(p):
        print("Файл не найден:", p)
        return
    cached = cfgmod.cache_background(p)     # копия в кэш, старый удаляется
    cfgmod.update(background=cached)
    print("Фон:", os.path.basename(p), "(кэширован, старый удалён)")


def set_rotate(v):
    if v not in ROTATIONS:
        print("Поворот: 0/90/180/270"); return
    cfgmod.update(rotate=v); print("Поворот:", v)


def set_brightness(v):
    if not 0 <= v <= 100:
        print("Яркость: 0..100"); return
    cfgmod.update(brightness=v); print("Яркость:", v)


def set_color(hexv):
    h = hexv.lstrip("#")
    if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
        print("Цвет: 6 hex-символов, напр. 00FFAA"); return
    cfgmod.update(text_color=h.upper()); print("Цвет текста:", h.upper())


def set_position(p):
    if p not in ("up", "down"):
        print("Положение: up / down"); return
    cfgmod.update(position=p); print("Положение строки:", p)


# ------------------------------ команды ------------------------------
def cmd_detect():
    print("Устройство 0483:5740:", "найдено" if device.available() else "НЕ найдено")


def cmd_run(argv):
    ap = argparse.ArgumentParser(prog="zalman-display run")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    from .daemon import run
    run(verbose=not a.quiet)


def apply_flags(argv):
    ap = argparse.ArgumentParser(prog="zalman-display", add_help=True,
                                 description="Управление дисплеем Zalman Alpha 2")
    ap.add_argument("--set", "--image", dest="image", metavar="PATH",
                    help="фон: путь к картинке/видео/gif (или 'none')")
    ap.add_argument("--rotate", type=int, choices=ROTATIONS)
    ap.add_argument("--brightness", type=int, metavar="0-100")
    ap.add_argument("--text-color", dest="color", metavar="HEX")
    ap.add_argument("--position", choices=("up", "down"))
    ap.add_argument("--stats", choices=("on", "off"))
    ap.add_argument("--strip", choices=("on", "off"),
                    help="полупрозрачная подложка под текстом")
    a = ap.parse_args(argv)
    did = False
    if a.image is not None:
        set_background(a.image); did = True
    if a.rotate is not None:
        set_rotate(a.rotate); did = True
    if a.brightness is not None:
        set_brightness(a.brightness); did = True
    if a.color is not None:
        set_color(a.color); did = True
    if a.position is not None:
        set_position(a.position); did = True
    if a.stats is not None:
        cfgmod.update(show_stats=(a.stats == "on")); did = True
        print("Строка параметров:", a.stats)
    if a.strip is not None:
        cfgmod.update(text_bg=(a.strip == "on")); did = True
        print("Подложка:", a.strip)
    if not did:
        ap.print_help()


# ------------------------------ сервис ------------------------------
def systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args]).returncode
    except FileNotFoundError:
        print("systemctl не найден"); return 1


def service_status():
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", SERVICE],
                           capture_output=True, text=True)
        return r.stdout.strip() or "unknown"
    except FileNotFoundError:
        return "n/a"


def install_service():
    os.makedirs(USER_UNIT_DIR, exist_ok=True)
    workdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    unit = (
        "[Unit]\nDescription=Zalman Alpha 2 display\n"
        "After=graphical-session.target\n\n"
        "[Service]\nType=simple\nWorkingDirectory=%s\n"
        "ExecStart=%s -m zalman_lcd run --quiet\nRestart=always\nRestartSec=3\n\n"
        "[Install]\nWantedBy=default.target\n" % (workdir, sys.executable))
    open(os.path.join(USER_UNIT_DIR, SERVICE), "w").write(unit)
    systemctl("daemon-reload")
    systemctl("enable", "--now", SERVICE)
    print("Сервис установлен и запущен (автозапуск включён).")
    print("Совет: `loginctl enable-linger $USER` — работать без входа в сессию.")
    if not os.access("/dev/bus/usb", os.R_OK) or True:
        print("\nЕсли нет прав на USB — установи udev-правило (sudo):")
        print("  echo '%s' | sudo tee /etc/udev/rules.d/99-zalman-lcd.rules"
              % UDEV_RULE.strip())
        print("  sudo udevadm control --reload && sudo udevadm trigger")


def cmd_service(argv):
    action = argv[0] if argv else "status"
    if action == "install":
        install_service()
    elif action == "uninstall":
        systemctl("disable", "--now", SERVICE)
        p = os.path.join(USER_UNIT_DIR, SERVICE)
        if os.path.isfile(p):
            os.remove(p)
        print("Сервис удалён.")
    elif action in ("start", "stop", "restart", "status"):
        systemctl(action, SERVICE)


# --------------------------- интерактив ---------------------------
def _ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def menu():
    while True:
        c = cfgmod.load()
        print("\n=== Zalman Display ===")
        print(" устройство: %s | сервис: %s"
              % ("найдено" if device.available() else "НЕ найдено",
                 service_status()))
        print(" фон=%s  яркость=%d  поворот=%d  цвет=%s  строка=%s(%s)"
              % (c["background"] or "нет", c["brightness"], c["rotate"],
                 c["text_color"], "вкл" if c["show_stats"] else "выкл",
                 c["position"]))
        print("""  1) Задать фон (картинка/видео/gif)
  2) Убрать фон (чёрный)
  3) Яркость (0-100)
  4) Поворот (0/90/180/270)
  5) Цвет текста (HEX)
  6) Положение строки (вверх/вниз)
  7) Строка параметров вкл/выкл
  8) Подложка под текстом вкл/выкл
  9) Сервис: 1старт 2стоп 3перезапуск 4статус
 10) Установить автозапуск сервиса
  0) Выход""")
        ch = _ask("Выбор: ")
        if ch == "0" or ch == "":
            break
        elif ch == "1":
            set_background(_ask("Путь к файлу: "))
        elif ch == "2":
            set_background(None)
        elif ch == "3":
            v = _ask("Яркость 0-100: ")
            if v.isdigit():
                set_brightness(int(v))
        elif ch == "4":
            v = _ask("Поворот 0/90/180/270: ")
            if v.isdigit():
                set_rotate(int(v))
        elif ch == "5":
            set_color(_ask("Цвет HEX (напр. 00FFAA): "))
        elif ch == "6":
            v = _ask("Положение [1] вверх / [2] вниз: ")
            set_position("up" if v == "1" else "down")
        elif ch == "7":
            cur = cfgmod.load()["show_stats"]
            cfgmod.update(show_stats=not cur)
            print("Строка параметров:", "выкл" if cur else "вкл")
        elif ch == "8":
            cur = cfgmod.load()["text_bg"]
            cfgmod.update(text_bg=not cur)
            print("Подложка:", "выкл" if cur else "вкл")
        elif ch == "9":
            s = _ask("  [1]старт [2]стоп [3]перезапуск [4]статус: ")
            cmd_service({"1": ["start"], "2": ["stop"], "3": ["restart"],
                         "4": ["status"]}.get(s, ["status"]))
        elif ch == "10":
            install_service()
    print("Готово.")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return menu()
    head = argv[0]
    if head == "run":
        return cmd_run(argv[1:])
    if head == "service":
        return cmd_service(argv[1:])
    if head == "detect":
        return cmd_detect()
    if head in ("menu", "-i"):
        return menu()
    return apply_flags(argv)


if __name__ == "__main__":
    main()
