#!/usr/bin/env python3
"""Offline checks for idle behavior, camera wiring, and privacy boundaries."""

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files/surface-dashboard"))
import dashboard


class ProvisioningTests(unittest.TestCase):
    def test_kiosk_files_render_and_second_run_is_unchanged(self):
        executable = Path(sys.executable).parent / "ansible-playbook"
        if not executable.exists():
            located = shutil.which("ansible-playbook")
            if not located:
                self.skipTest("Install ansible/requirements.txt to check provisioning")
            executable = Path(located)
        import yaml

        root = Path(__file__).resolve().parents[1]
        play = yaml.safe_load((root / "surface-dashboard.yaml").read_text())[0]
        kiosk = next(task["block"] for task in play["tasks"] if task["name"] == "Configure the verified kiosk")
        tasks = copy.deepcopy(kiosk[1:5])
        for task in tasks:
            task.pop("notify", None)
            if "ansible.builtin.template" in task:
                task["ansible.builtin.template"]["src"] = str(root / "templates/surface-dashboard.service.j2")
        with tempfile.TemporaryDirectory(prefix="surface-ansible-test-") as temporary:
            directory = Path(temporary)
            (directory / "group_vars").symlink_to(root / "group_vars", target_is_directory=True)
            inventory = directory / "inventory.yaml"
            inventory.write_text(yaml.safe_dump({"surface_dashboard": {"hosts": {"fixture": {
                "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                "dashboard_idle_seconds": 73, "dashboard_motion_threshold": 2500,
                "dashboard_camera_name": "\\_SB_.PCI0.I2C2.CAMF",
            }}}}))
            fixture = directory / "render.yaml"
            fixture.write_text(yaml.safe_dump([{
                "name": "Render actual kiosk tasks into a temporary home",
                "hosts": "surface_dashboard", "gather_facts": False, "become": False,
                "vars": {**play["vars"], "dashboard_home": str(directory / "home"),
                         "dashboard_user": str(os.getuid()), "dashboard_gid": str(os.getgid())},
                "tasks": tasks,
            }]))
            environment = {**os.environ, "ANSIBLE_LOCAL_TEMP": str(directory / "ansible-local"),
                           "ANSIBLE_REMOTE_TEMP": str(directory / "ansible-remote"), "ANSIBLE_NOCOLOR": "1"}
            command = [str(executable), "-i", str(inventory), str(fixture)]
            first = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            config = json.loads((directory / "home/.config/surface-dashboard/config.json").read_text())
            self.assertEqual(config["idle_seconds"], 73, "Host settings must override group defaults")
            self.assertEqual(config["motion_threshold"], 2500)
            self.assertEqual(config["camera_name"], "\\_SB_.PCI0.I2C2.CAMF")
            locker = (directory / "home/.config/autostart/light-locker.desktop").read_text()
            self.assertIn("Hidden=true", locker)
            second = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertRegex(second.stdout, r"changed=0\s+unreachable=0\s+failed=0")
            units = list((directory / "home/.config/systemd/user").glob("*"))
            self.assertEqual(len(units), 5)
            if shutil.which("systemd-analyze"):
                result = subprocess.run(["systemd-analyze", "verify", "--man=no", *map(str, units)],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(dashboard_url="https://hass.example.test/front-door/home",
                           camera_name="\\_SB_.PCI0.I2C2.CAMF", idle_seconds=120,
                           motion_threshold=1500)

    def test_browser_configures_touch_kiosk_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(dashboard.Path, "home", return_value=Path(temporary)), \
                patch.object(dashboard, "urlopen"), \
                patch.object(dashboard.os, "execvp") as launch:
            dashboard.browser(self.config)
            profile = Path(temporary) / ".local/share/surface-dashboard/firefox"
            preferences = (profile / "user.js").read_text()
            self.assertIn('user_pref("dom.screenwakelock.enabled", false);', preferences)
            self.assertIn('user_pref("browser.chrome.toolbar_tips", false);', preferences)
            self.assertIn('user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);', preferences)
            self.assertEqual((profile / "chrome/userContent.css").read_text(),
                             '* { cursor: none !important; }\n')
            launch.assert_called_once_with("firefox", ["firefox", "--no-remote", "--profile",
                                                       str(profile), "--kiosk", self.config["dashboard_url"]])

    def test_idle_requires_both_input_and_motion_inactivity(self):
        display = dashboard.Display(0)
        self.assertEqual(display.desired(119, 1000, None, 120), "on")
        self.assertEqual(display.desired(120, 120, None, 120), "off")
        self.assertEqual(display.desired(121, 0, None, 120), "on")
        self.assertEqual(display.desired(130, 1000, 42, 120), "on")
        self.assertEqual(display.desired(249, 1000, 42, 120), "on")
        self.assertEqual(display.desired(250, 1000, 42, 120), "off")
        self.assertEqual(display.desired(251, 1000, 43, 120), "on")

    def test_repeated_movement_resets_timeout_and_missing_marker_does_not(self):
        display = dashboard.Display(0)
        for now in range(1, 400):
            self.assertEqual(display.desired(now, 1000, now, 120), "on")
        self.assertEqual(display.desired(519, 1000, None, 120), "off")

    def test_wall_clock_not_used_for_idle(self):
        display = dashboard.Display(100)
        self.assertEqual(display.desired(101, 1000, 10**18, 120), "on")
        self.assertEqual(display.desired(221, 1000, 10**18, 120), "off")

    def test_url_rejects_credentials_and_insecure_or_token_urls(self):
        for url in ("http://hass.test/front-door", "https://user:password@hass.test/",
                    "https://hass.test/?token=secret", "https://hass.test/#token",
                    "https:///front-door", "https://hass.test/\n", "https://hass.test:bad/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                dashboard.validate({**self.config, "dashboard_url": url})
        self.assertEqual(dashboard.validate(self.config), self.config)

    def test_invalid_calibration_rejected(self):
        for key in ("idle_seconds", "motion_threshold"):
            for value in (0, -1, True, "120", 100000):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    dashboard.validate({**self.config, key: value})
        with self.assertRaises(ValueError):
            dashboard.validate({**self.config, "camera_name": "camera\nother"})

    def test_recording_and_network_interfaces_disabled(self):
        config = dashboard.motion_config(self.config, Path("/run/user/1000/surface-dashboard"))
        for line in ("picture_output off", "picture_output_motion off", "movie_output off",
                     "movie_output_motion off", "snapshot_interval 0", "stream_port 0", "webcontrol_port 0"):
            self.assertIn(line + "\n", config)
        self.assertIn("on_motion_detected /usr/bin/touch ", config)
        self.assertIn("on_camera_lost /usr/bin/systemctl --user --no-block restart", config)
        self.assertIn(f"video_device {dashboard.VIDEO_DEVICE}", config)
        self.assertIn(f"device={dashboard.VIDEO_DEVICE}", dashboard.camera_command(self.config))
        self.assertIn('camera-name="\\\\_SB_.PCI0.I2C2.CAMF"', dashboard.camera_command(self.config))

    def test_display_restores_backlight_after_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(dashboard, "runtime_dir", return_value=Path(temporary)), \
                    patch.object(dashboard, "run") as run:
                run.side_effect = ["", "", "", "invalid idle", ""]
                with self.assertRaises(ValueError):
                    dashboard.display(self.config)
                self.assertEqual(run.call_args.args, ("xset", "dpms", "force", "on"))

    def test_bridge_is_stopped_if_frame_capture_times_out(self):
        with patch.object(dashboard.subprocess, "Popen") as popen, \
                patch.object(dashboard, "capture_frames", side_effect=subprocess.TimeoutExpired("v4l2-ctl", 20)), \
                patch.object(dashboard.os, "killpg") as kill:
            popen.return_value.poll.return_value = None
            with self.assertRaises(subprocess.TimeoutExpired):
                dashboard.camera_check(self.config)
            kill.assert_called_once()
            popen.return_value.wait.assert_called_once_with(timeout=5)

    def test_capture_waits_for_producer_then_reads_real_frames(self):
        with patch.object(dashboard, "run", side_effect=["Capabilities: Video Capture\nDevice Caps: Video Output", "Device Caps: Video Capture"]), \
                patch.object(dashboard.time, "sleep") as sleep, \
                patch.object(dashboard.subprocess, "run") as run:
            run.return_value.stdout = b"\x00" * (10 * 320 * 240 * 2)
            dashboard.capture_frames(10)
            sleep.assert_called_once_with(0.5)
            self.assertIn("--stream-count=10", run.call_args.args[0])
            self.assertIn("--stream-to=-", run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_capture_rejects_success_exit_without_frames(self):
        with patch.object(dashboard, "run", return_value="Device Caps: Video Capture"), \
                patch.object(dashboard.subprocess, "run") as run, \
                patch.object(dashboard.time, "sleep"):
            run.return_value.stdout = b""
            with self.assertRaisesRegex(RuntimeError, "did not deliver"):
                dashboard.capture_frames(10)

    def test_motion_restarts_stalled_bridge_at_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(self.config))
            with patch.object(dashboard, "CONFIG", path), patch("sys.argv", ["dashboard.py", "motion"]), \
                    patch.object(dashboard, "capture_frames", side_effect=subprocess.TimeoutExpired("v4l2-ctl", 20)), \
                    patch.object(dashboard, "run") as run:
                with self.assertRaises(subprocess.TimeoutExpired):
                    dashboard.main()
                run.assert_called_once_with("systemctl", "--user", "--no-block", "restart",
                                            "surface-dashboard-camera.service")

    def test_config_creation_is_private_and_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            argv = ["dashboard.py", "configure", self.config["dashboard_url"], self.config["camera_name"]]
            with patch.object(dashboard, "CONFIG", path), patch("sys.argv", argv):
                dashboard.main()
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(path.read_text()), self.config)
                calibrated = {**self.config, "motion_threshold": 3000}
                path.write_text(json.dumps(calibrated))
                dashboard.main()
                self.assertEqual(json.loads(path.read_text()), calibrated)
                with patch("sys.argv", [*argv[:2], "https://other.test/", argv[3]]):
                    with self.assertRaises(RuntimeError):
                        dashboard.main()
                self.assertEqual(json.loads(path.read_text()), calibrated)


if __name__ == "__main__":
    unittest.main()
