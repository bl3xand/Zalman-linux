# -*- coding: utf-8 -*-
"""Конфиг zalman-display (JSON в ~/.config/zalman-lcd/config.json)."""

import copy
import glob
import json
import os
import shutil

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "zalman-lcd")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
CACHE_DIR = os.path.join(CONFIG_DIR, "media")   # ровно 1 файл фона

DEFAULTS = {
    "background": None,        # путь к картинке/gif/видео (None => чёрный фон)
    "brightness": 80,          # 0..100
    "rotate": 0,               # 0/90/180/270
    "fps": 20,                 # частота для видео/gif
    "show_stats": True,        # показывать строку параметров
    "text_color": "FFFFFF",    # цвет строки, HEX
    "position": "down",        # up / down
    "text_bg": False,          # полупрозрачная подложка под текстом
}


def load():
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except Exception:
        data = {}
    cfg = copy.deepcopy(DEFAULTS)
    for k in DEFAULTS:                 # только известные ключи
        if k in data:
            cfg[k] = data[k]
    return cfg


def save(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


def update(**kw):
    cfg = load()
    cfg.update(kw)
    save(cfg)
    return cfg


def mtime():
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return 0.0


def cache_background(src):
    """Скопировать фон в кэш как ЕДИНСТВЕННЫЙ файл (старый удаляется).
    Возвращает путь к кэшированной копии. Оригинал больше не нужен."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    for old in glob.glob(os.path.join(CACHE_DIR, "*")):
        try:
            os.remove(old)
        except OSError:
            pass
    ext = os.path.splitext(src)[1].lower() or ".img"
    dst = os.path.join(CACHE_DIR, "background" + ext)
    shutil.copy2(src, dst)
    return dst


def clear_background_cache():
    for old in glob.glob(os.path.join(CACHE_DIR, "*")):
        try:
            os.remove(old)
        except OSError:
            pass
