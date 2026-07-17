# Zalman Alpha2 DS LCD Linux Driver

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

- **Python 3.7+** and **pipx** (`sudo pacman -S python python-pipx`). pipx pulls
  the Python deps (Pillow/numpy/psutil) automatically.
- **ffmpeg** — only for **video** backgrounds (`sudo pacman -S ffmpeg`).
- The USB serial driver `cdc_acm` is already in the kernel — nothing to install.

## Install

```bash
git clone https://github.com/bl3xand/Zalman-linux
cd Zalman-linux
pipx install .                # installs the command + deps + bundled font
zalman-display service install   # does the rest, automatically (see below)
```

`pipx install .` pulls in everything the tool needs (Pillow/numpy/psutil and the
bundled font) — nothing to copy by hand. `ffmpeg` is only needed for **video**
backgrounds (`sudo pacman -S ffmpeg`).

`zalman-display service install` then sets up **everything** in one go:
installs & enables the user service, starts it, turns on **linger** (so it runs
from boot, before login), and installs the **udev rule** if the device isn't
already accessible (asks for sudo once). Nothing else to do by hand.

> On Arch/PEP-668, if you prefer plain pip: `pip install --user --break-system-packages .`
> Or run straight from the source dir without installing: `python3 -m zalman_lcd <command>`

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
zalman-display --gpu list            # list GPUs; then e.g. --gpu card1 to pick one

zalman-display detect                # device present? + available GPUs
zalman-display log [-f]              # view (or follow) the diagnostic log
zalman-display run                   # run the display in the foreground
```

Every setting applies **live** — the running service picks up the change (a new
background/rotation is re-uploaded to flash; brightness/color/position update
immediately). Flags can be combined, e.g. `--rotate 90 --brightness 60`.

## Service

`zalman-display service install` (above) is a **user** systemd service — it runs
as you, no root daemon, reading your `~/.config`. It already enables **linger**
so it starts at boot and survives logout. Manage it:

```bash
zalman-display service start|stop|restart|status|uninstall
```

## Notes

- **Background is cached**: `--set` copies the file into
  `~/.config/zalman-lcd/media/` (exactly one file — the previous is deleted),
  so the original can be moved or removed.
- Config: `~/.config/zalman-lcd/config.json`.
- GPU metrics come from `nvidia-smi` (NVIDIA) or sysfs `/sys/class/drm` (AMD);
  shown as `--` if unavailable. With **multiple GPUs** (e.g. iGPU + dGPU) run
  `zalman-display --gpu list` and pick the right one with `--gpu cardN`
  (default `auto` takes the first with a temperature sensor).
- The separate USB device `0145:2001` is the pump/fan/RGB controller (HID); it
  is not handled here.
- The stats font (**JetBrains Mono Bold**, SIL OFL) is bundled in
  `zalman_lcd/fonts/` — no system font needed; monospace keeps the digits from
  shifting. The daemon falls back to a system sans if the bundled file is
  missing.

## Troubleshooting

**`zsh: command not found: zalman-display` after install.** `~/.local/bin`
(where pipx puts the command) isn't on your `PATH`. `pipx ensurepath` can be a
no-op here: it checks the `PATH` of the shell *it* runs in, and if that shell
already inherited `~/.local/bin` (e.g. from a login profile) it decides there is
nothing to do and writes **nothing** to your rc file — so your interactive shell
still can't find the command. Fix it explicitly:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc   # or ~/.bashrc
source ~/.zshrc                                            # reload current shell
zalman-display detect
```

Opening a new terminal also works once the line is in your rc file. Note that
the source repo is **not** needed at runtime: `pipx install .` copies the
package (incl. the bundled font) into its own venv, and `service install`
generates the systemd unit and udev rule from strings in the code — so after a
successful install you can delete the cloned directory. To update later,
re-clone and run `pipx install --force .`.

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
