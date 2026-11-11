# Confession Booth (Raspberry Pi 3B+)

A reality-TV "confession booth" for a party. Guests step in front of the webcam, press the big red
button, get a 3-2-1 countdown and a clapperboard **ACTION!**, say their piece, and press the button
again to stop. Every clip is saved to an external SSD **and** the microSD card, then checked.

![Screens: idle, countdown, ACTION, recording, saved, hold to shut down](docs/screens.png)
*(Screenshots from the built-in test picture. On the booth it's the live camera.)*

- **Idle:** live camera (mirrored like a mirror) with a slightly see-through red button and
  `PRESS BUTTON TO RECORD`.
- **Press:** 3-2-1 film-leader countdown, then a clapperboard snaps shut with a flash: **ACTION!**
- **Recording:** a small flashing red dot and the time, nothing else.
- **Press again:** a spinner while the file is closed, then ✓ (or ✗ if it failed).
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
| USB webcam with a microphone | One that sends MJPEG at 720p (most do: Logitech C270/C920/C922 etc.) |
| Small screen | HDMI or the official DSI touchscreen work best (any resolution; 800×480 is ideal). SPI "GPIO" screens are slow |
| USB SSD | exFAT (readable on Mac and Windows) or ext4. Name it e.g. `CONFESSIONS` |
| Red momentary push button | Arcade button or panel push button |
| Power | Official 5.1 V 2.5 A supply. **Use a powered USB hub for the SSD** if it's a 2.5" drive (see below) |

**Power matters.** The Pi 3B+ shares about 1.2 A between all four USB ports. A webcam plus a
2.5" SSD can pull more than that and brown out, which corrupts files. Use the official supply and
put the SSD (or both) on a powered USB hub. The booth checks for under-voltage and shows a yellow
⚡ in the status bar if it has happened since boot.

## Wiring the button

```
Button leg 1 ── GPIO17 (physical pin 11)
Button leg 2 ── GND    (physical pin 9)
```

No resistor needed (the Pi's internal pull-up is used). A different pin can be set with
`button_pin` in the settings (BCM numbering).

## Setting up

1. Flash **Raspberry Pi OS Lite** with Raspberry Pi Imager. In its settings, set a username,
   password, Wi-Fi (for the install only) and enable SSH.
2. Copy this folder to the Pi (or `git clone` the repository on the Pi) and run:
   ```bash
   cd ConfessionBooth
   sudo ./install.sh
   sudo reboot
   ```
   This installs ffmpeg, pygame and gpiozero, copies the program to `/opt/confession-booth`,
   and makes it start on the screen at power-on (no desktop, no login prompt, no boot text).
3. Plug in the webcam, SSD and button. The booth appears on the screen within a minute of power-on.

Settings live in `/etc/confession-booth/config.toml` (all options are in
[`config.example.toml`](config.example.toml)). After changing them:
`sudo systemctl restart confession-booth`. Worth setting:

- `v4l2_controls = { power_line_frequency = 1 }` stops light flicker on 50 Hz mains
  (Australia, UK, Europe); use `2` for 60 Hz.
- `max_record_seconds`, `countdown_from`, `idle_text`, `action_text`.
- `video_size = "1920x1080"` if your webcam does MJPEG at 1080p (then `preview_lowres = 2`).

The Pi has no clock battery, so with no internet the date in file names can be wrong. The
files are numbered (`booth_0001_…`, `booth_0002_…`), so their order is always right.

## Where the videos go

| | Folder |
|---|---|
| SSD | `Confessions/` on the SSD (mounted on the Pi at `/mnt/booth-ssd`) |
| microSD | `/srv/confessions` |

Each recording is written to **both drives at the same time** by one ffmpeg process, so if a guest
knocks the SSD cable out mid-recording the microSD copy keeps going (and the other way round). If
the SSD isn't there at all, recording carries on to the microSD alone and the clips are copied to
the SSD when it comes back.

Files are `.mkv` (Motion-JPEG video straight from the webcam, so the Pi does no video encoding,
plus uncompressed sound). They play in VLC and import into DaVinci Resolve. For phones, QuickTime,
iMovie, Premiere or CapCut, convert them on a laptop after the party:

```bash
tools/convert-to-mp4.sh /Volumes/CONFESSIONS/Confessions      # makes Confessions/mp4/*.mp4
```

Roughly 7–15 GB per hour of recording at 720p, depending on the webcam.

## How the files are checked

After every recording, in the background:

1. The file is closed and renamed from `name.mkv.part` to `name.mkv` (a `.part` file means a
   recording that was never finished; after a power cut these are recovered at the next start).
2. A quick `ffprobe` decides the ✓ or ✗ the guest sees.
3. Each copy is then read back **from the drive** (the cached copy is dropped first). Its SHA-256 is
   taken and every audio/video packet is run through ffmpeg. The SSD and microSD copies must hold
   identical audio and video.
4. If a copy is missing or doesn't match, the copies are fully decoded to find the good one, the bad
   copy is kept as `name.mkv.bad-…`, and the good one is copied over and checked by SHA-256.
5. `SHA256SUMS` is updated next to the videos on both drives. On a computer:
   `cd Confessions && sha256sum -c SHA256SUMS` (Mac: `shasum -a 256 -c SHA256SUMS`).

While the booth runs it also checks, every 15 s: the SSD is still connected (it's remounted if it
was unplugged and plugged back in), neither drive has switched to read-only, the SSD has no new
I/O errors, and free space. Every 5 minutes (when nobody is recording) a test file is written and
read back on both drives. Every 10 minutes one saved video is re-read and compared with its
checksum. Every 30 minutes the SSD's SMART health is read, if its USB adapter supports it.
Background work is slowed down while someone is recording so it never affects the camera.

A record of every clip and every check is kept in `/var/lib/confession-booth/manifest.json`
(and copied to the SSD as `booth-manifest.json`).

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

Below 1 GB free a drive isn't used. If neither drive has room, pressing the button shows ✗.

## Shutting down

Hold the button for **30 seconds**. The screen goes black with a power symbol and an SSD icon.
When the SSD icon turns green with a ✓, the SSD is unmounted and powered off. The Pi then shuts
down. Wait for the Pi's green light to stop flashing before you unplug the power.

If the SSD can't be unmounted (red ✗), the Pi still shuts down normally, which also unmounts it.

## Trying it on a laptop

No Pi needed (Linux or Mac with Python 3.11+, ffmpeg and `pip install pygame`):

```bash
python3 -m booth --dev        # window, test picture + tone, space bar = the button, Esc = quit
```

Recordings go to `./booth-data/` (a plain folder stands in for the SSD).

Tests:

```bash
python3 tests/test_storage.py      # verification, SSD unplugged then back, corruption repair, crash recovery
python3 tests/run_headless.py      # whole booth with no screen: records, saves screenshots, shuts down
```

## Troubleshooting

- **Log:** `journalctl -u confession-booth -f`, or `/var/lib/confession-booth/booth.log`.
  ffmpeg's own messages: `camera-ffmpeg.log` and `last-recording-ffmpeg.log` in the same folder.
- **Camera icon with a red line:** no webcam found, or it stopped. It retries every 2 s.
  `v4l2-ctl --list-devices` and `v4l2-ctl -d /dev/video0 --list-formats-ext` show what it can do.
- **Quiet sound:** `alsamixer`, then F6 to pick the webcam and F4 for capture levels.
- **Lips out of sync:** set `av_offset` (seconds, e.g. `0.1` if the picture is ahead of the sound).
- **Choppy preview:** set `preview_fps = 10`, or `preview_lowres = 2`. Recordings are not affected
  (they're the camera's own frames).
- **Stop the booth to use the console:** `sudo systemctl stop confession-booth`
  (`disable` to stop it starting at boot).
