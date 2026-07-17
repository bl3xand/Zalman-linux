# zalman-display

Driver and CLI service for the **Zalman Alpha 2** AIO **LCD** (320×320) on
**Linux** — a replacement for the Windows-only «Zalman OZ». Shows an
**image / GIF / video** fullscreen plus a **system-monitoring** line
(CPU / GPU / RAM), with brightness and rotation control, and runs as an
autostart service.

Protocol reverse-engineered from scratch — see [PROTOCOL.md](PROTOCOL.md).

- The background (image/GIF/video) is **uploaded to the device's flash once**;
  the device then loops it on its own. This matches the Windows app and avoids
  the freeze that continuous streaming causes (the device's `0x05` buffer is
  ~6 MB and wedges if streamed to indefinitely).
- The stats line is a transparent overlay drawn on top, refreshed every 2 s.
- Animated backgrounds are limited to ~360 frames / ≈5 MB (the flash buffer).

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
pipx install .                # recommended (provides the `zalman-display` command)
# On Arch/PEP-668 systems `pip install --user .` is blocked; use pipx, or:
#   pip install --user --break-system-packages .
# Or skip install entirely and run from the source dir:
#   python3 -m zalman_lcd <command>
```

Device access without root (udev rule):

```bash
sudo cp 99-zalman-lcd.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

## Usage

```bash
zalman-display                       # show current status + this help
zalman-display --set ~/clip.gif      # background: image / gif / video (or 'none')
zalman-display --rotate 90           # rotation 0/90/180/270
zalman-display --brightness 70       # brightness 0..100
zalman-display --text-color 00FFAA   # stats text color (HEX)
zalman-display --position up         # stats text at top (or 'down')
zalman-display --stats off           # hide / show the monitoring line
zalman-display --stats-bg black      # strip behind the text: off / white / black (30% alpha)

zalman-display detect                # is the device present?
zalman-display log [-f]              # view (or follow) the diagnostic log
zalman-display run                   # run the display in the foreground
```

Every setting applies **live** — the running service picks up the change (a new
background/rotation is re-uploaded to flash; brightness/color/position update
immediately). Flags can be combined, e.g. `--rotate 90 --brightness 60`.

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
- The stats font (**JetBrains Mono Bold**, SIL OFL) is bundled in
  `zalman_lcd/fonts/` — no system font needed; monospace keeps the digits from
  shifting. The daemon falls back to a system sans if the bundled file is
  missing.

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
`ov-*` / `bright` / `vrfy*`), how many bytes went out, the elapsed time, a 30-second
heartbeat (overlays, memory), `SLOW-WRITE` early-warnings, and every USB-reset
recovery. After a freeze, view it with:

```bash
zalman-display log        # last ~200 lines
zalman-display log -f     # live tail
```

A `stage=bg-term` stall means the display's JPEG decoder hung on the frame it just
received (see the COM-marker note above); a stall on `bg-hdr`/`bright` means the
link was already wedged before that frame. The log is capped (~1 MB, one `.1`
rollover) so it never fills the disk.

License: MIT. Bundled font **JetBrains Mono** is under the SIL Open Font License
(see `zalman_lcd/fonts/OFL.txt`).
