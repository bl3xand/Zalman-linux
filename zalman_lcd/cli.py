# -*- coding: utf-8 -*-
"""CLI for zalman-display: flags for scripting + interactive menu (no args)."""

import argparse
import os
import subprocess
import sys

from . import config as cfgmod
from . import device

SERVICE = "zalman-display.service"
USER_UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
UDEV_RULE = ('SUBSYSTEM=="usb", ATTR{idVendor}=="0483", '
             'ATTR{idProduct}=="5740", MODE="0666", TAG+="uaccess"')
ROTATIONS = (0, 90, 180, 270)


# ------------------------- settings -------------------------
def set_background(path):
    if path is None or str(path).lower() in ("none", "off", ""):
        cfgmod.clear_background_cache()
        cfgmod.update(background=None, bg_name=None)
        print("Background cleared (black).")
        return
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(p):
        print("File not found:", p)
        return
    cached = cfgmod.cache_background(p)     # copy into cache, old one removed
    cfgmod.update(background=cached, bg_name=os.path.basename(p))
    print("Background set:", os.path.basename(p), "(cached, previous removed)")


def set_rotate(v):
    if v not in ROTATIONS:
        print("Rotation must be 0/90/180/270"); return
    cfgmod.update(rotate=v); print("Rotation: %d°" % v)


def set_brightness(v):
    if not 0 <= v <= 100:
        print("Brightness must be 0..100"); return
    cfgmod.update(brightness=v); print("Brightness:", v)


def set_color(hexv):
    h = hexv.lstrip("#")
    if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
        print("Color must be 6 hex digits, e.g. 00FFAA"); return
    cfgmod.update(text_color=h.upper()); print("Text color:", h.upper())


def set_position(p):
    if p not in ("up", "down"):
        print("Position must be up / down"); return
    cfgmod.update(position=p); print("Stats position:", p)


# ------------------------------ commands ------------------------------
def cmd_detect():
    print("Device 0483:5740:", "FOUND" if device.available() else "NOT found")


def cmd_run(argv):
    ap = argparse.ArgumentParser(prog="zalman-display run")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    from .daemon import run
    run(verbose=not a.quiet)


def apply_flags(argv):
    ap = argparse.ArgumentParser(
        prog="zalman-display", add_help=True,
        description="Control the Zalman Alpha 2 LCD. Changes apply live "
                    "(the running service picks them up).")
    ap.add_argument("--set", "--image", dest="image", metavar="PATH",
                    help="background: path to image / gif / video (or 'none')")
    ap.add_argument("--rotate", type=int, choices=ROTATIONS, metavar="0|90|180|270",
                    help="screen rotation")
    ap.add_argument("--brightness", type=int, metavar="0-100", help="0..100")
    ap.add_argument("--text-color", dest="color", metavar="HEX",
                    help="stats text color, e.g. 00FFAA")
    ap.add_argument("--position", choices=("up", "down"),
                    help="where the stats text goes")
    ap.add_argument("--stats", choices=("on", "off"),
                    help="show / hide the monitoring line")
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
        print("Stats line:", a.stats)
    if not did:
        ap.print_help()
        print("\nService: zalman-display service install|start|stop|restart|status"
              "\nOther:   zalman-display detect | log [-f]")


# ------------------------------ service ------------------------------
def systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args]).returncode
    except FileNotFoundError:
        print("systemctl not found"); return 1


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
    print("Service installed and started (autostart enabled).")
    print("Tip: `loginctl enable-linger $USER` to run before you log in.")
    print("\nNo USB access? Install the udev rule (needs sudo):")
    print("  echo '%s' | sudo tee /etc/udev/rules.d/99-zalman-lcd.rules"
          % UDEV_RULE)
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
        print("Service removed.")
    elif action in ("start", "stop", "restart", "status"):
        systemctl(action, SERVICE)


# ------------------------------ status ------------------------------
def print_status():
    c = cfgmod.load()
    bg = c.get("bg_name") or (os.path.basename(c["background"])
                              if c["background"] else "none")
    print("=== Zalman Display ===")
    print(" device: %s | service: %s"
          % ("found" if device.available() else "NOT found", service_status()))
    print(" background=%s  brightness=%d  rotation=%d°  color=%s  stats=%s(%s)\n"
          % (bg, c["brightness"], c["rotate"], c["text_color"],
             "on" if c["show_stats"] else "off", c["position"]))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    head = argv[0] if argv else ""
    if head == "run":
        return cmd_run(argv[1:])
    if head == "service":
        return cmd_service(argv[1:])
    if head == "detect":
        return cmd_detect()
    if head == "log":
        return cmd_log(argv[1:])
    if not argv:                    # без аргументов — статус + справка по флагам
        print_status()
        return apply_flags([])
    return apply_flags(argv)


def cmd_log(argv):
    """Показать диагностический лог (для отладки зависаний)."""
    from . import dbg
    path = dbg.LOG_PATH
    if not os.path.isfile(path):
        print("no log yet:", path)
        return 0
    if argv and argv[0] in ("-f", "--follow"):
        os.execvp("tail", ["tail", "-n", "80", "-f", path])
    print(path, "\n" + "-" * 60)
    with open(path) as f:
        lines = f.readlines()
    sys.stdout.write("".join(lines[-200:]))
    return 0


if __name__ == "__main__":
    main()
