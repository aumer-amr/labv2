#!/usr/bin/env python3
"""Local Surface kiosk runtime; Python standard library only."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen


CONFIG = Path.home() / ".config/surface-dashboard/config.json"
VIDEO_DEVICE = "/dev/video42"


def validate(config):
    url = urlsplit(config["dashboard_url"])
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment
            or any(char.isspace() for char in config["dashboard_url"])):
        raise ValueError("Use an HTTPS dashboard URL without credentials, query, or fragment")
    url.port  # Reject malformed ports before installing anything.
    name = config["camera_name"]
    if not isinstance(name, str) or not name or any(ord(char) < 32 for char in name):
        raise ValueError("camera_name must be the front camera ID from cam --list")
    for key, maximum in (("idle_seconds", 3600), ("motion_threshold", 76800)):
        if type(config[key]) is not int or not 1 <= config[key] <= maximum:
            raise ValueError(f"{key} must be an integer between 1 and {maximum}")
    return config


def camera_command(config):
    name = config["camera_name"].replace("\\", "\\\\").replace('"', '\\"')
    return [
        "gst-launch-1.0", "-e", "libcamerasrc", f'camera-name="{name}"',
        "!", "video/x-raw,width=1280,height=720,format=NV12",
        "!", "videorate", "drop-only=true", "!", "video/x-raw,framerate=5/1",
        "!", "videoconvert", "!", "videoscale",
        "!", "video/x-raw,width=320,height=240,format=YUY2,framerate=5/1",
        "!", "queue", "max-size-buffers=2", "leaky=downstream",
        "!", "v4l2sink", f"device={VIDEO_DEVICE}", "sync=false",
    ]


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=15).stdout


def runtime_dir():
    path = Path(os.environ["XDG_RUNTIME_DIR"]) / "surface-dashboard"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def motion_config(config, directory):
    marker = shlex.quote(str(directory / "motion"))
    return f"""daemon off
video_device {VIDEO_DEVICE}
width 320
height 240
framerate 5
threshold {config['motion_threshold']}
minimum_motion_frames 2
lightswitch_percent 50
event_gap 5
picture_output off
picture_output_motion off
movie_output off
movie_output_motion off
snapshot_interval 0
stream_port 0
webcontrol_port 0
log_level 5
on_motion_detected /usr/bin/touch {marker}
on_camera_lost /usr/bin/systemctl --user --no-block restart surface-dashboard-camera.service
"""


class Display:
    def __init__(self, now):
        self.last_motion = now
        self.marker = None

    def desired(self, now, input_idle, marker, timeout):
        if marker is not None and marker != self.marker:
            self.last_motion = now
            self.marker = marker
        return "on" if min(input_idle, now - self.last_motion) < timeout else "off"


def display(config):
    directory = runtime_dir()
    run("xset", "s", "off")
    run("xset", "+dpms")
    run("xset", "dpms", "0", "0", "0")
    controller = Display(time.monotonic())
    try:
        while True:
            try:
                marker = (directory / "motion").stat().st_mtime_ns
            except FileNotFoundError:
                marker = None
            idle = int(run("xprintidle").strip()) / 1000
            desired = controller.desired(time.monotonic(), idle, marker, config["idle_seconds"])
            status = run("xset", "q")
            actual = re.search(r"Monitor is (On|Off|Standby|Suspend)", status)
            if not actual:
                raise RuntimeError("X11 DPMS is unavailable; verify the backlight manually")
            if actual.group(1).lower() != desired:
                run("xset", "dpms", "force", desired)
            time.sleep(0.5)
    finally:
        run("xset", "dpms", "force", "on")


def capture_frames(count):
    # exclusive_caps exposes capture only after the GStreamer producer is ready.
    for _ in range(40):
        try:
            info = run("v4l2-ctl", f"--device={VIDEO_DEVICE}", "--all")
            if "Video Capture" in info.split("Device Caps", 1)[-1]:
                frames = subprocess.run([
                    "v4l2-ctl", f"--device={VIDEO_DEVICE}", "--stream-mmap",
                    f"--stream-count={count}", "--stream-to=-",
                ], check=True, capture_output=True, timeout=20)
                # v4l2-ctl can exit zero before the producer starts streaming.
                if len(frames.stdout) == count * 320 * 240 * 2:
                    return
        except subprocess.CalledProcessError:
            pass
        time.sleep(0.5)
    raise RuntimeError("Camera bridge did not deliver the requested 320x240 YUY2 frames")


def camera_check(config):
    """Require real frames from the complete bridge, rather than an open device."""
    bridge = subprocess.Popen(camera_command(config), start_new_session=True)
    try:
        capture_frames(10)
        print("PASS: ten real frames captured through the front-camera bridge", flush=True)
    finally:
        if bridge.poll() is None:
            os.killpg(bridge.pid, signal.SIGINT)
            try:
                bridge.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(bridge.pid, signal.SIGKILL)
                bridge.wait()


def check(config):
    if os.environ.get("XDG_SESSION_TYPE") != "x11" or not os.environ.get("DISPLAY"):
        raise RuntimeError("Run from the tablet's X11 desktop, not SSH or Wayland")
    for command in ("gst-launch-1.0", "gst-inspect-1.0", "motion", "v4l2-ctl", "cam",
                    "xset", "xprintidle", "firefox", "systemctl"):
        if not shutil.which(command):
            raise RuntimeError(f"Missing {command}; run the Ansible prepare stage first")
    run("gst-inspect-1.0", "libcamerasrc")
    if config["camera_name"] not in run("cam", "--list"):
        raise RuntimeError("Configured front camera is absent from cam --list")
    if "DPMS is Enabled" not in run("xset", "q"):
        raise RuntimeError("Enable DPMS with xset +dpms before checking")
    int(run("xprintidle").strip())
    camera_check(config)
    print("Turning display off for five seconds; confirm the backlight goes out", flush=True)
    try:
        run("xset", "dpms", "force", "off")
        time.sleep(5)
    finally:
        run("xset", "dpms", "force", "on")
    print("Software checks passed; physical backlight and motion still need acceptance checks")


def browser(config):
    profile = Path.home() / ".local/share/surface-dashboard/firefox"
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    (profile / "user.js").write_text(
        'user_pref("dom.screenwakelock.enabled", false);\n'
        'user_pref("browser.chrome.toolbar_tips", false);\n'
        'user_pref("media.autoplay.default", 0);\n'
        'user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);\n'
    )
    chrome = profile / "chrome"
    chrome.mkdir(mode=0o700, exist_ok=True)
    (chrome / "userContent.css").write_text('* { cursor: none !important; }\n')
    while True:
        try:
            with urlopen(config["dashboard_url"], timeout=10):
                break
        except HTTPError as error:
            if error.code < 500:
                break  # Open the browser so the user can authenticate.
        except (URLError, TimeoutError):
            pass
        print("Waiting for Home Assistant HTTPS connectivity", flush=True)
        time.sleep(5)
    os.execvp("firefox", ["firefox", "--no-remote", "--profile", str(profile),
                           "--kiosk", config["dashboard_url"] + "?BrowserID=surface"])


def stop(*_):
    raise SystemExit(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "check", "camera", "motion", "display", "browser", "session"))
    parser.add_argument("values", nargs="*")
    args = parser.parse_args()
    if args.action == "configure":
        if len(args.values) != 2:
            parser.error("configure requires HTTPS_DASHBOARD_URL FRONT_CAMERA_ID")
        config = validate(dict(dashboard_url=args.values[0], camera_name=args.values[1],
                               idle_seconds=120, motion_threshold=1500))
        if CONFIG.exists():
            existing = validate(json.loads(CONFIG.read_text()))
            if any(existing[key] != config[key] for key in ("dashboard_url", "camera_name")):
                raise RuntimeError("Config exists with another URL/camera; edit it explicitly")
            return
        CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(CONFIG, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
            json.dump(config, file, indent=2)
            file.write("\n")
        return
    config = validate(json.loads(CONFIG.read_text()))
    if args.action == "check":
        check(config)
    elif args.action == "camera":
        command = camera_command(config)
        os.execvp(command[0], command)
    elif args.action == "motion":
        try:
            capture_frames(1)
        except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            run("systemctl", "--user", "--no-block", "restart", "surface-dashboard-camera.service")
            raise
        path = runtime_dir() / "motion.conf"
        path.write_text(motion_config(config, path.parent))
        os.execvp("motion", ["motion", "-n", "-c", str(path)])
    elif args.action == "display":
        signal.signal(signal.SIGTERM, stop)
        display(config)
    elif args.action == "browser":
        browser(config)
    elif args.action == "session":
        if os.environ.get("XDG_SESSION_TYPE") != "x11":
            raise RuntimeError("The kiosk requires an X11 desktop session")
        names = [name for name in ("DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE") if name in os.environ]
        run("systemctl", "--user", "import-environment", *names)
        run("systemctl", "--user", "restart", "surface-dashboard.target")


if __name__ == "__main__":
    main()
