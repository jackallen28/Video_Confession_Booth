"""Will a video play on an iPhone / in Safari?

    sudo booth-check-video              the newest recording
    sudo booth-check-video FILE.mp4     a particular one
"""

import glob
import json
import os
import subprocess
import sys

from . import config, media


def main():
    cfg = config.load()
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        files = glob.glob(os.path.join(cfg["sd_dir"], "*.mp4")) + glob.glob(
            os.path.join(cfg["ssd_mount"], cfg["ssd_subdir"], "*.mp4"))
        if not files:
            sys.exit("No recordings yet.")
        path = max(files, key=os.path.getmtime)
    print(path)
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,codec_tag_string,profile,level,pix_fmt,"
                        "width,height,avg_frame_rate,extradata_size:format=duration,size",
                        "-of", "json", path], capture_output=True)
    info = json.loads(r.stdout or b"{}")
    for s in info.get("streams", []):
        print("  " + ", ".join(f"{k}={v}" for k, v in s.items()))
    print("  " + ", ".join(f"{k}={v}" for k, v in info.get("format", {}).items()))
    try:
        with open(os.path.join(cfg["state_dir"], "video-check.json")) as f:
            print("  recorded with:", json.load(f)["choice"])
    except (OSError, ValueError, KeyError):
        pass
    issues = media.iphone_issues(path)
    print("\nOK for iPhone/Safari" if not issues else
          "\nMay NOT play on iPhone/Safari:\n  - " + "\n  - ".join(issues))


if __name__ == "__main__":
    main()
