"""Settings that can be changed from the web page (and `booth-camera`).

They are saved in /etc/confession-booth/settings.json, which overrides
config.toml for these keys.

apply: "live"    - used straight away (texts, timings, next recording)
       "camera"  - the camera preview restarts for a second
       "restart" - the booth restarts (new resolution is test-encoded first)
"""

CORNERS = ["bottom-right", "bottom-left", "top-left", "top-right"]
TS_FORMATS = ["%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %I:%M %p",
              "%a %d %b %Y %H:%M", "%H:%M:%S"]

FIELDS = [
    # --- recording / camera
    dict(key="orientation", group="Recording", label="Orientation", type="select",
         options=["landscape", "portrait"], apply="restart",
         help="Changes the videos and the booth screen"),
    dict(key="picture_rotation", group="Recording", label="Portrait: camera picture",
         type="select", options=["right", "left", "crop"], apply="restart",
         help="Camera on its side: turn right or left (swap if upside down)"),
    dict(key="screen_rotation", group="Recording", label="Portrait: booth screen",
         type="select", options=["right", "left", "none"], apply="restart",
         help="Screen on its side: turn right or left (swap if upside down)"),
    dict(key="video_size", group="Recording", label="Resolution", type="resolution",
         apply="restart", help="Best available = the biggest size this Pi records smoothly"),
    dict(key="framerate", group="Recording", label="Frame rate", type="select", apply="restart",
         options=["max", 15, 20, 24, 25, 30]),
    dict(key="timestamp", group="Recording", label="Date/time on the video", type="bool",
         apply="live"),
    dict(key="timestamp_corner", group="Recording", label="Date/time corner", type="select",
         options=CORNERS, apply="live"),
    dict(key="timestamp_format", group="Recording", label="Date/time format", type="strftime",
         options=TS_FORMATS, apply="live"),
    dict(key="timestamp_size", group="Recording", label="Date/time size", type="select",
         options=["small", "medium", "large"], apply="live"),
    dict(key="storage_mode", group="Recording", label="Where videos are saved", type="select",
         options=["ssd_first", "both"], apply="live",
         help="SSD first: SSD, then the microSD when the SSD is full. Both: a copy on each "
              "(a backup, but half the space)"),
    dict(key="max_record_seconds", group="Recording", label="Longest recording (seconds)",
         type="int", min=10, max=1800, apply="live"),
    dict(key="av_offset", group="Recording", label="Sound/picture sync (seconds)", type="float",
         min=-1.0, max=1.0, step=0.02, apply="live",
         help="Positive if the picture is ahead of the sound"),
    # --- booth screen
    dict(key="mirror_preview", group="Screen", label="Mirror the screen (like a mirror)",
         type="bool", apply="camera", help="Recordings are never mirrored"),
    dict(key="input", group="Screen", label="Start/stop with", type="select",
         options=["touch", "button", "both"], apply="restart",
         help="Tap anywhere on the screen, the physical red button (and its light), or either"),
    dict(key="waiting_theme", group="Screen", label="Waiting screen (physical button)",
         type="select", options=["confessional", "neon", "spotlight", "drama", "hand", "fessup"],
         apply="live"),
    dict(key="waiting_arrow", group="Screen", label="Arrow pointing at the button", type="bool",
         apply="live"),
    dict(key="button_hint_x", group="Screen", label="Arrow to the button: position across",
         type="float", min=0.0, max=1.0, step=0.01, apply="live",
         help="0 = left edge, 0.5 = middle, 1 = right edge (physical button)"),
    dict(key="start_animation", group="Screen", label="Before recording", type="select",
         options=["countdown", "clapperboard", "both", "none"], apply="live"),
    dict(key="countdown_from", group="Screen", label="Countdown from", type="int", min=1, max=10,
         apply="live"),
    dict(key="status_bar", group="Screen", label="Free-space bar", type="select", apply="live",
         options=["when_full", "problems", "always"]),
    dict(key="low_free_mb", group="Screen", label="'Nearly full' below (MB)", type="int",
         min=500, max=200000, apply="live"),
    # --- after STOP
    dict(key="black_seconds", group="After recording", label="Black after the TV switch-off (s)",
         type="float", min=0, max=10, step=0.5, apply="live"),
    dict(key="saving_min_seconds", group="After recording", label="Saving bar at least (s)",
         type="float", min=0, max=30, step=0.5, apply="live"),
    dict(key="delete_hold_seconds", group="After recording", label="Hold to delete (s)",
         type="float", min=1, max=10, step=0.5, apply="live"),
    dict(key="delete_confirm", group="After recording",
         label="After the hold, press once more to confirm the delete", type="bool",
         apply="live"),
    dict(key="delete_confirm_seconds", group="After recording",
         label="Time to press again to delete (s)", type="float", min=2, max=30, step=0.5,
         apply="live"),
    dict(key="hold_shutdown_seconds", group="After recording", label="Hold to shut down (s)",
         type="int", min=5, max=120, apply="live"),
    # --- words on the screen
    dict(key="touch_idle_text", group="Words on screen", label="Waiting (touch screen)",
         type="text", apply="live"),
    dict(key="touch_delete_hint_text", group="Words on screen",
         label="Delete hint (touch screen)", type="text", apply="live"),
    dict(key="waiting_title", group="Words on screen", label="Waiting screen: big line",
         type="text", apply="live", help="Not used by Fess Up! (its words are part of the artwork)"),
    dict(key="waiting_subtitle", group="Words on screen", label="Waiting screen: small line",
         type="text", apply="live"),
    dict(key="touch_delete_confirm_text", group="Words on screen",
         label="Delete: tap again (touch screen)", type="text", apply="live"),
    dict(key="idle_text", group="Words on screen", label="Waiting (button)", type="text",
         apply="live"),
    dict(key="action_text", group="Words on screen", label="Clapperboard", type="text",
         apply="live"),
    dict(key="saving_text", group="Words on screen", label="Saving", type="text", apply="live"),
    dict(key="saved_text", group="Words on screen", label="Saved", type="text", apply="live"),
    dict(key="recording_text", group="Words on screen", label="Recording", type="text",
         apply="live"),
    dict(key="failed_text", group="Words on screen", label="Not saved", type="text",
         apply="live"),
    dict(key="delete_hint_text", group="Words on screen", label="Delete hint (button)", type="text",
         apply="live"),
    dict(key="delete_confirm_text", group="Words on screen", label="Delete: press again (button)",
         type="text", apply="live"),
    dict(key="deleted_text", group="Words on screen", label="Deleted", type="text",
         apply="live"),
]

BY_KEY = {f["key"]: f for f in FIELDS}
# camera picture controls are saved too (dict name -> value)
EDITABLE_KEYS = tuple(BY_KEY) + ("v4l2_controls",)


def validate(key, value):
    """Returns the cleaned value or raises ValueError."""
    f = BY_KEY.get(key)
    if f is None:
        raise ValueError(f"unknown setting {key}")
    t = f["type"]
    if t == "bool":
        if isinstance(value, str):
            return value.lower() in ("1", "true", "on", "yes")
        return bool(value)
    if t in ("int", "float"):
        v = float(value)
        if v != v:
            raise ValueError("not a number")
        v = max(f.get("min", v), min(f.get("max", v), v))
        return int(round(v)) if t == "int" else round(v, 3)
    if t == "select":
        for o in f["options"]:
            if str(o) == str(value):
                return o
        raise ValueError(f"{key} must be one of {f['options']}")
    if t == "resolution":
        value = str(value)
        if value == "max":
            return value
        w, _, h = value.partition("x")
        if not (w.isdigit() and h.isdigit()):
            raise ValueError("resolution like 1280x720 or max")
        return value
    if t == "strftime":
        value = str(value)[:60]
        if "%" not in value:
            raise ValueError("needs strftime codes, e.g. %d/%m/%Y %H:%M:%S")
        return value
    value = str(value).strip()[:60]
    if not value and key not in ("action_text",):
        raise ValueError("can't be empty")
    return value
