# Confession Booth (Raspberry Pi 3B+)

A reality-TV "confession booth" for a party. Guests step in front of the webcam, press the big red
button, get a 3-2-1 countdown and a clapperboard **ACTION!**, say their piece, and press the button
again to stop. Every clip is saved as an MP4 to an external SSD **and** the microSD card, then checked.

![Screens: idle, countdown, ACTION, recording, TV switch-off, saving, hold to delete, confirm delete, saved](docs/screens.png)
*(Screenshots from the built-in test picture. On the booth it's the live camera.)*

- **Idle:** live camera (mirrored like a mirror) with a slightly see-through red button and
  `PRESS BUTTON TO RECORD`. The light in the button slowly breathes.
- **Press:** 3-2-1 film-leader countdown (the button flashes with each number), then a clapperboard
  snaps shut with a flash: **ACTION!**
- **Recording:** a small flashing red dot and the time. The button light flashes with the dot.
- **Press again:** the picture switches off like an old TV (squashes to a line, then a dot), 2 s of
  black, then **VIDEO SAVING** with a loading bar (at least 5 s) and
  `HOLD BUTTON 3 SEC TO DELETE`. Then **VIDEO SAVED** ✓ and back to the camera.
- **Delete:** on the saving screen, hold the button for 3 s (a red ring fills, the light flickers
  faster), let go, then press once more within 6 s (`PRESS AGAIN TO DELETE`, light blinking fast).
  The clip is removed from both drives: **VIDEO DELETED**. Not pressed = kept.
- **Bottom bar:** free space on the SSD (box icon) and microSD (card icon). Icons on the right
  appear only when something needs attention (see [Status bar](#status-bar)).
- **Hold the button for 30 s:** a ring fills up from 2 s; at 30 s the recording (if any) is saved,
  the SSD is unmounted and powered off (SSD icon turns green ✓), then the Pi shuts down.
  Let go before 30 s to cancel.

Starts by itself when the Pi is switched on. Recordings stop by themselves after 5 minutes.

## Parts

| Part | Notes |
|------|-------|
| Raspberry Pi 3B+ | Raspberry Pi OS **Lite** (Bookworm or newer, 32 or 64-bit) on a 32 GB+ microSD |
| USB webcam with a microphone | One that sends MJPEG (nearly all HD webcams: Logitech C270/C920/C922/Brio etc.) |
| Small screen | HDMI or the official DSI touchscreen work best (any resolution; 800×480 is ideal). SPI "GPIO" screens are slow |
| USB SSD | exFAT (readable on Mac and Windows) or ext4. Name it e.g. `CONFESSIONS` |
| Red momentary push button with a light | Arcade-style LED button (5 V or 12 V lamp), or a plain button plus a separate LED |
| NPN transistor + 1 kΩ resistor | To switch the button's light (e.g. BC337, 2N2222, or a small LED driver board) |
| Power | Official 5.1 V 2.5 A supply. **Use a powered USB hub for the SSD** if it's a 2.5" drive (see below) |

**Power matters.** The Pi 3B+ shares about 1.2 A between all four USB ports. A webcam plus a
2.5" SSD can pull more than that and brown out, which corrupts files. Use the official supply and
put the SSD (or both) on a powered USB hub. The booth checks for under-voltage and shows a yellow
⚡ in the status bar if it has happened since boot.

## Wiring

```
Button switch:   one leg ── GPIO17 (pin 11)        other leg ── GND (pin 9)

Button light (5 V LED button, through an NPN transistor):
   5 V (pin 2) ── light +
   light −     ── transistor collector (C)
   transistor emitter (E) ── GND (pin 14)
   GPIO27 (pin 13) ── 1 kΩ ── transistor base (B)
```

- No resistor is needed on the switch (the Pi's internal pull-up is used).
- A GPIO pin gives only 3.3 V and a few mA, so the button's lamp needs the transistor. For a
  **12 V** lamp, connect "light +" to the 12 V supply instead of pin 2 and join that supply's
  ground to the Pi's GND.
- A **plain small LED** can go straight on the pin: GPIO27 ── 330 Ω ── LED+ , LED− ── GND.
- Other pins: `button_pin` and `led_pin` in the settings (BCM numbers). `led_pin = -1` = no light.

## Setting up

1. Flash **Raspberry Pi OS Lite** with Raspberry Pi Imager. In its settings, set a username,
   password, Wi-Fi and enable SSH. Set the time zone too (the file names and the optional
   timestamp use the Pi's clock).
2. Copy this folder to the Pi (or `git clone` the repository on the Pi) and run:
   ```bash
   cd ConfessionBooth
   sudo ./install.sh
   sudo reboot
   ```
   This installs ffmpeg, pygame and gpiozero, copies the program to `/opt/confession-booth`,
   adds the `booth-camera` settings tool, and makes the booth start on the screen at power-on
   (no desktop, no login prompt, no boot text).
3. Plug in the webcam, SSD, screen and button. The booth appears on the screen within a minute.
   **The first time** (and after changing camera or resolution settings) there's a spinner for a
   minute or two while it tests which resolution this Pi can record smoothly (see below).

## Video files

| | Folder |
|---|---|
| SSD | `Confessions/` on the SSD (mounted on the Pi at `/mnt/booth-ssd`) |
| microSD | `/srv/confessions` |

Files are **MP4 (H.264 video + AAC sound)**, the same kind an iPhone records, so they play in
QuickTime, Photos, iMovie, on iPhones (AirDrop, Files) and in every editor. Each file is named
with just the date and time it was recorded, e.g. `2026-11-11_21-04-33.mp4`.

**Resolution:** the booth asks the webcam what it can do and uses its highest resolution, as long
as this Pi can record it smoothly. The Pi 3B+'s hardware video encoder goes up to 1080p, so a 4K
webcam records at 1920×1080. At first start each size is test-encoded for a few seconds (with the
timestamp on, if it's on) and the biggest one that runs comfortably faster than real time is used.
You can pick a fixed size with `sudo booth-camera` instead. `sudo booth-camera show` shows what
is being recorded now.

Each recording is written to **both drives at the same time** by one ffmpeg process, so if a guest
knocks the SSD cable out mid-recording the microSD copy keeps going (and the other way round). If
the SSD isn't there at all, recording carries on to the microSD alone and the clips are copied to
the SSD when it comes back. Roughly 4–5 GB per hour of recording at 1080p.

## Camera settings over SSH

```bash
ssh pi@<booth address>
sudo booth-camera
```

A menu to change:

- **Resolution / frame rate:** "best available" (automatic) or any size the camera lists.
- **Timestamp on the video:** on/off, which corner, format (e.g. `11/11/2026 21:04:33`), size.
  It's burnt into the recording only, not shown on the booth screen.
- **Picture controls:** whatever the webcam offers, typically brightness, contrast, saturation,
  sharpness, white balance (auto or a fixed temperature), exposure (auto or fixed), backlight
  compensation, focus, zoom and anti-flicker (`power_line_frequency`: 50 Hz in Australia/UK/Europe).
  These change on the booth screen straight away.
- **Mirror** the screen preview on/off, and **set the Pi's date and time** (it has no clock
  battery; with Wi-Fi it sets itself).

Resolution and timestamp changes need the booth restarted: the menu offers it (it won't restart
while someone is recording). Settings are kept in `/etc/confession-booth/camera.json`.
The same things work as one-liners:

```bash
sudo booth-camera show                    # settings, camera modes and all its controls
sudo booth-camera timestamp on            # or off
sudo booth-camera resolution max          # or 1280x720, 1920x1080@25
sudo booth-camera set brightness=140 white_balance_automatic=0 white_balance_temperature=3500
sudo booth-camera reset                   # picture controls back to the camera's defaults
sudo booth-camera time "2026-12-31 21:30"
sudo booth-camera restart
```

Other settings (texts on screen, timings, pins, drives) are in `/etc/confession-booth/config.toml`
(all options are in [`config.example.toml`](config.example.toml)); after changing them:
`sudo systemctl restart confession-booth`.

## How the files are checked

1. While recording, each drive gets `name.ts.part` (MPEG-TS survives a power cut). When the
   recording stops, each copy is turned into the `.mp4` (no re-encoding) and checked with
   `ffprobe`; this is what the loading bar waits for. After a power cut, leftover `.ts.part` files
   are turned into MP4s at the next start.
2. Once the guest has had the chance to delete it, each copy is read back **from the drive**
   (cached data dropped first). Its SHA-256 is taken and every audio/video packet is run through
   ffmpeg. The SSD and microSD copies must hold identical audio and video.
3. If a copy is missing or doesn't match, the copies are fully decoded to find the good one, the bad
   copy is kept as `name.mp4.bad-…`, and the good one is copied over and checked by SHA-256.
4. `SHA256SUMS` is updated next to the videos on both drives. On a computer:
   `cd Confessions && sha256sum -c SHA256SUMS` (Mac: `shasum -a 256 -c SHA256SUMS`).

While the booth runs it also checks, every 15 s: the SSD is still connected (it's remounted if it
was unplugged and plugged back in), neither drive has switched to read-only, the SSD has no new
I/O errors, and free space. Every 5 minutes (when nobody is recording) a test file is written and
read back on both drives. Every 10 minutes one saved video is re-read and compared with its
checksum. Every 30 minutes the SSD's SMART health is read, if its USB adapter supports it.
Background work is slowed down while someone is recording so it never affects the camera.

A record of every clip and every check (including deleted ones) is kept in
`/var/lib/confession-booth/manifest.json` (and copied to the SSD as `booth-manifest.json`).

## Status bar

| Shows | Means |
|-------|-------|
| White SSD / SD free space | All good |
| Yellow | Under 8 GB free |
| Red | Drive problem (read-only, I/O errors, write test failed, SMART failing). Recording uses the other drive |
| Grey `—` | SSD not connected. Recording goes to the microSD only, copied later |
| Spinning circle | Checking or copying recordings in the background |
| Microphone with a line through it | No microphone found. Recording without sound |
| ⚡ | Under-voltage since boot: stronger power supply or powered hub needed |
| Red ⚠ | A recording that couldn't be verified on any drive (see the log) |

Below 1 GB free a drive isn't used. If neither drive has room, pressing the button shows ✗ and the
button light double-blinks (it also double-blinks when there's no camera).

## Shutting down

Hold the button for **30 seconds**. The screen goes black with a power symbol and an SSD icon.
When the SSD icon turns green with a ✓, the SSD is unmounted and powered off. The Pi then shuts
down and the button light goes out. Wait for the Pi's green light to stop flashing before you
unplug the power.

If the SSD can't be unmounted (red ✗), the Pi still shuts down normally, which also unmounts it.

## Trying it on a laptop

No Pi needed (Linux or Mac with Python 3.11+, ffmpeg and `pip install pygame`):

```bash
python3 -m booth --dev               # window, test picture + tone, space bar = the button, Esc = quit
python3 -m booth.camera_setup --dev  # the settings menu, saving to ./booth-data/camera.json
```

Recordings go to `./booth-data/` (a plain folder stands in for the SSD).

Tests:

```bash
python3 tests/test_storage.py      # MP4 + names, verification, SSD unplugged then back, corruption repair, crash recovery, delete
python3 tests/test_devices.py      # reading the webcam's modes and controls, resolution choice
python3 tests/run_headless.py      # whole booth with no screen: keeps one clip, deletes one, shuts down; saves screenshots
```

## Troubleshooting

- **Log:** `journalctl -u confession-booth -f`, or `/var/lib/confession-booth/booth.log`.
  ffmpeg's own messages: `camera-ffmpeg.log` and `last-recording-ffmpeg.log` in the same folder.
  The resolution test results are in `video-check.json`.
- **Camera icon with a red line:** no webcam found, or it stopped. It retries every 2 s. Plugging in
  a different webcam restarts the booth so it re-tests the resolution.
- **Quiet sound:** `alsamixer`, then F6 to pick the webcam and F4 for capture levels.
- **Lips out of sync:** set `av_offset` (seconds, e.g. `0.1` if the picture is ahead of the sound).
- **Choppy preview:** set `preview_fps = 10`. Recordings are not affected.
- **Wrong time in file names / timestamp:** `sudo booth-camera time "2026-12-31 21:30"`, or give
  the Pi Wi-Fi for a minute.
- **Stop the booth to use the console:** `sudo systemctl stop confession-booth`
  (`disable` to stop it starting at boot).
