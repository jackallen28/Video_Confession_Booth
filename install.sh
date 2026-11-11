#!/bin/bash
# Installs the confession booth on Raspberry Pi OS Lite (Bookworm or newer)
# and makes it start on the screen at power-on.
#
#   sudo ./install.sh
#
# Run it again after changing the code to update the installed copy.
# Your settings in /etc/confession-booth/config.toml are kept.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo:  sudo ./install.sh"
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/confession-booth

echo "== Installing packages (takes a few minutes)"
apt-get update
apt-get install -y ffmpeg python3-pygame python3-gpiozero v4l-utils alsa-utils \
                   exfatprogs util-linux fonts-dejavu-core
# for the optional booth Wi-Fi network (sudo booth-wifi on)
apt-get install -y dnsmasq-base iw || echo "   (Wi-Fi network packages not installed, carrying on)"
for pkg in python3-lgpio smartmontools ntfs-3g; do
  apt-get install -y "$pkg" || echo "   (optional package $pkg not installed, carrying on)"
done

echo "== Copying the program to $APP"
mkdir -p "$APP"
rm -rf "$APP/booth"
cp -r "$HERE/booth" "$APP/"
cp "$HERE/config.example.toml" "$APP/"
find "$APP" -name __pycache__ -prune -exec rm -rf {} +

mkdir -p /etc/confession-booth /srv/confessions /var/lib/confession-booth /mnt/booth-ssd
if [[ ! -f /etc/confession-booth/config.toml ]]; then
  cp "$HERE/config.example.toml" /etc/confession-booth/config.toml
  echo "   settings: /etc/confession-booth/config.toml"
fi

# Camera settings over SSH: sudo booth-camera
printf '#!/bin/sh\ncd /opt/confession-booth && exec python3 -m booth.camera_setup "$@"\n' \
  > /usr/local/bin/booth-camera
chmod 755 /usr/local/bin/booth-camera
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
echo "Done. Plug in the webcam, SSD, screen and button, then:  sudo reboot"
echo "Camera settings (over SSH):  sudo booth-camera"
echo "Booth Wi-Fi for your phone:  sudo booth-wifi on"
echo "Live log:  journalctl -u confession-booth -f"
