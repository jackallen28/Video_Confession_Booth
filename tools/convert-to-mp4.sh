#!/bin/bash
# Turns the booth's .mkv recordings into .mp4 files (H.264 + AAC) that play on
# phones, QuickTime, iMovie, Premiere and CapCut. Run it on a laptop after the
# party (needs ffmpeg: "brew install ffmpeg" on a Mac), not on the Pi.
#
#   ./convert-to-mp4.sh /Volumes/SSD/Confessions            -> .../Confessions/mp4/
#   ./convert-to-mp4.sh /Volumes/SSD/Confessions ~/Desktop/booth
set -euo pipefail

IN="${1:-.}"
OUT="${2:-$IN/mp4}"
mkdir -p "$OUT"

for f in "$IN"/*.mkv; do
  [[ -e "$f" ]] || { echo "No .mkv files in $IN"; exit 1; }
  out="$OUT/$(basename "${f%.mkv}").mp4"
  if [[ -e "$out" ]]; then
    echo "already done: $out"
    continue
  fi
  echo "-> $out"
  ffmpeg -hide_banner -loglevel error -stats -i "$f" \
    -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p -r 30 \
    -c:a aac -b:a 160k -ac 2 -movflags +faststart "$out.part.mp4"
  mv "$out.part.mp4" "$out"
done
