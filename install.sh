#!/bin/bash
# Installs the confession booth on Raspberry Pi OS (Bookworm or newer)
# and makes it start on the screen at power-on.
#
#   sudo ./install.sh
#
# Run it again to update the installed copy (or use Software update on the web
# page). Your settings in /etc/confession-booth/ are kept.
#
# Videos are never touched: this only replaces the program in
# /opt/confession-booth. The new program is swapped in at once (a power cut
# can't leave half of it), and the previous one is kept as booth.prev.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo:  sudo ./install.sh"
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/confession-booth
STATUS=/var/lib/confession-booth/status.json

# Don't change anything while a guest is recording or a clip is being saved.
if [[ -z ${BOOTH_FROM_UPDATER:-} && -f $STATUS ]] && systemctl is-active --quiet confession-booth; then
  for i in $(seq 1 300); do
    state=$(python3 -c "import json;print(json.load(open('$STATUS')).get('state',''))" 2>/dev/null || true)
    case "$state" in
      countdown|recording|stopping|review|confirm|deleted)
        [[ $i -eq 1 ]] && echo "The booth is busy ($state); waiting for it to finish saving..."
        sleep 2 ;;
      *) break ;;
    esac
  done
fi

echo "== Checking packages"
NEEDED="ffmpeg python3-pygame python3-gpiozero v4l-utils alsa-utils exfatprogs util-linux fonts-dejavu-core dnsmasq-base iw git"
# fonts for the reality-TV waiting screens (falls back to a plain font without them)
OPTIONAL="python3-lgpio smartmontools ntfs-3g fonts-lobster fonts-bebas-neue fonts-montserrat"
missing() { for p in "$@"; do dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "ok installed" || echo "$p"; done; }
# shellcheck disable=SC2086
MISS=$(missing $NEEDED)
# shellcheck disable=SC2086
MISS_OPT=$(missing $OPTIONAL)
if [[ -n $MISS || -n $MISS_OPT ]]; then
  echo "   installing:" $MISS $MISS_OPT "(takes a few minutes)"
  apt-get update
  # shellcheck disable=SC2086
  [[ -z $MISS ]] || apt-get install -y $MISS
  for pkg in $MISS_OPT; do
    apt-get install -y "$pkg" || echo "   (optional package $pkg not installed, carrying on)"
  done
else
  echo "   all there"
fi

echo "== Installing the program to $APP"
mkdir -p "$APP"
rm -rf "$APP/booth.new"
cp -r "$HERE/booth" "$APP/booth.new"
find "$APP/booth.new" -name __pycache__ -prune -exec rm -rf {} +
sync
if [[ -d $APP/booth ]]; then
  rm -rf "$APP/booth.prev"
  mv "$APP/booth" "$APP/booth.prev"
  [[ -f $APP/VERSION ]] && cp "$APP/VERSION" "$APP/VERSION.prev"
fi
mv "$APP/booth.new" "$APP/booth"
cp "$HERE/config.example.toml" "$APP/"
git -C "$HERE" -c safe.directory="$HERE" log -1 --format='%h %cd' --date=format:'%d/%m/%Y' \
  > "$APP/VERSION" 2>/dev/null || echo "unknown" > "$APP/VERSION"
sync
echo "   version $(cat "$APP/VERSION")"

mkdir -p /etc/confession-booth /srv/confessions /var/lib/confession-booth /mnt/booth-ssd
if [[ ! -f /etc/confession-booth/config.toml ]]; then
  cp "$HERE/config.example.toml" /etc/confession-booth/config.toml
  echo "   settings: /etc/confession-booth/config.toml"
fi
# where updates come from (Software update on the web page)
echo "$HERE" > /etc/confession-booth/source

# Camera settings over SSH: sudo booth-camera
printf '#!/bin/sh\ncd /opt/confession-booth && exec python3 -m booth.camera_setup "$@"\n' \
  > /usr/local/bin/booth-camera
chmod 755 /usr/local/bin/booth-camera
# Will a recording play on iPhone/Safari: sudo booth-check-video
printf '#!/bin/sh\ncd /opt/confession-booth && exec python3 -m booth.checkvideo "$@"\n' \
  > /usr/local/bin/booth-check-video
chmod 755 /usr/local/bin/booth-check-video
# Update from the internet (or the saved phone hotspot): sudo booth-update
printf '#!/bin/sh\ncd /opt/confession-booth && exec python3 -m booth.updater "$@"\n' \
  > /usr/local/bin/booth-update
chmod 755 /usr/local/bin/booth-update
# The booth's own Wi-Fi network for phones: sudo booth-wifi on
install -m 755 "$HERE/tools/booth-wifi" /usr/local/bin/booth-wifi

echo "== Starting the booth at power-on"
install -m 644 "$HERE/systemd/confession-booth.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable confession-booth.service
# Boot to the console, not a desktop: the booth draws straight to the screen.
systemctl set-default multi-user.target

# Quiet screen at boot: no blanking, no Raspberry logos, no blinking cursor.
CMDLINE=/boot/firmware/cmdline.txt
[[ -f $CMDLINE ]] || CMDLINE=/boot/cmdline.txt
if [[ -f $CMDLINE ]]; then
  [[ -f $CMDLINE.before-booth ]] || cp "$CMDLINE" "$CMDLINE.before-booth"
  for opt in consoleblank=0 logo.nologo vt.global_cursor_default=0; do
    grep -qw -- "$opt" "$CMDLINE" || sed -i "1 s/\$/ $opt/" "$CMDLINE"
  done
fi

echo
if [[ -n ${BOOTH_FROM_UPDATER:-} ]]; then
  echo "Done."
else
  echo "Done. To start the new version:  sudo systemctl restart confession-booth"
  echo "  (first install: plug in the webcam, SSD, screen and button, then  sudo reboot)"
  echo "Camera settings (over SSH):  sudo booth-camera"
  echo "Booth Wi-Fi for your phone:  sudo booth-wifi on"
  echo "Live log:  journalctl -u confession-booth -f"
fi
