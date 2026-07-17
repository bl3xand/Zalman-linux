# zalman-display

Driver and CLI service for the **Zalman Alpha 2** AIO **LCD** (320×320) on
**Linux** — a replacement for the Windows-only «Zalman OZ». Shows an
**image / GIF / video** fullscreen plus a **system-monitoring** line
(CPU / GPU / RAM), with brightness and rotation control, and runs as an
autostart service.

Protocol reverse-engineered from scratch — see [PROTOCOL.md](PROTOCOL.md).

- Background is streamed as **JPEG** (compressed → smooth, ~20–30 fps).
- Stats line is a transparent overlay on top, refreshed once per second.

## Requirements

```bash
# Arch
sudo pacman -S python python-pillow python-numpy python-psutil ffmpeg
#   Pillow/numpy/psutil — Python deps; ffmpeg — video only (cdc_acm is in the kernel)
```

## Install

```bash
git clone https://github.com/bl3xand/Zalman-linux
cd Zalman-linux
pip install --user .          # provides the `zalman-display` command
# or run from source: python3 -m zalman_lcd <command>
```

Device access without root (udev rule):

```bash
sudo cp 99-zalman-lcd.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

## Quick start

```bash
zalman-display                       # interactive menu (below)
zalman-display detect                # find the device
zalman-display --set ~/clip.gif      # background: image / gif / video
zalman-display --brightness 80       # brightness 0..100
zalman-display --rotate 90           # rotation 0/90/180/270
zalman-display --position up         # stats line at top (or down)
zalman-display --text-color 00FFAA   # stats color (HEX)
zalman-display run                   # run the display (Ctrl+C to stop)
```

While the service/daemon is running, any command above applies **live** (the
daemon watches the config file).

## Interactive menu

Run with no arguments:

```bash
zalman-display
```

```
=== Zalman Display ===
 device: found | service: active
 background=…  brightness=80  rotation=0°  color=FFFFFF  stats=on(down)
  1) Set background (image / gif / video)   ← asks for a file path
  2) Clear background (black)
  3) Brightness (0-100)
  4) Rotate 90°                             ← each press turns +90°
  5) Text color (HEX)
  6) Stats position (top/bottom)
  7) Stats line on/off
  8) Text strip on/off
  9) Service: start / stop / restart / status
 10) Install autostart service
  0) Exit
```

## Autostart (service on boot)

```bash
zalman-display service install       # installs & enables the user service
loginctl enable-linger $USER         # so it runs before you log in
```

Manage it: `zalman-display service start|stop|restart|status`.

## Notes

- **Background is cached**: `--set` copies the file into
  `~/.config/zalman-lcd/media/` (exactly one file — the previous is deleted),
  so the original can be moved or removed.
- Config: `~/.config/zalman-lcd/config.json`.
- GPU metrics come from `nvidia-smi` (NVIDIA) or sysfs `/sys/class/drm` (AMD);
  shown as `--` if unavailable.
- The separate USB device `0145:2001` is the pump/fan/RGB controller (HID); it
  is not handled here.

## Troubleshooting

**The screen freezes / stops updating.** The device has a strict hardware JPEG
decoder that **hangs on any non-standard marker**. The classic trigger is a
`COM` (comment) marker: image libraries copy the source file's metadata (GIFs
almost always carry a comment) into every encoded frame, and the decoder locks
up on it — it stops draining the USB pipe and the whole link wedges. This driver
therefore re-encodes every frame as a **clean baseline JPEG** (no comment/EXIF,
4:2:0), byte-structurally identical to what the Windows app sends. If you still
see a freeze:

- The daemon **auto-recovers**: on a stall it performs a kernel-level USB reset
  (`USBDEVFS_RESET`) and reconnects — no physical unplugging needed. For that to
  work without root, install the udev rule below (it grants access to the USB
  node, not just the tty).
- If the display is **already stuck** from a previous session, a USB reset
  re-establishes the control link but may not clear a hung decoder — in that
  case **cut power once** (full PC shutdown, or unplug the cooler's USB header)
  to reset the display MCU. A warm reboot keeps the USB bus powered and does
  **not** clear it.
- If a background looks pixelated, it is being cropped to 320×320; use a roughly
  square source for best results.

**Diagnosing a freeze.** The daemon writes a timestamped log to
`~/.config/zalman-lcd/zalman.log` (always on, even under the service). It records
each stall with the exact stage it hung on (`bg-hdr` / `bg-body` / `bg-term` /
`ov-*` / `bright` / `vrfy*`), how many bytes went out, the elapsed time, a 5-second
heartbeat (frames, fps, memory), `SLOW-WRITE` early-warnings, and every USB-reset
recovery. After a freeze, view it with:

```bash
zalman-display log        # last ~200 lines
zalman-display log -f     # live tail
```

A `stage=bg-term` stall means the display's JPEG decoder hung on the frame it just
received (see the COM-marker note above); a stall on `bg-hdr`/`bright` means the
link was already wedged before that frame.

License: MIT.
