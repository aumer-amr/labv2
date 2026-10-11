# Surface dashboard provisioning

This directory provisions the Surface Pro 4 (i5-6300U, 8 GB RAM) after Linux Mint
22.x Xfce is installed. It replaces the shell installer. The existing
[Front Door dashboard](https://hass.aumer.dev/front-door/home) stays separate from
the PC dashboard; card design is deferred.

`surface-dashboard.yaml` has two stages: `prepare` installs the Surface kernel and
camera dependencies; `kiosk` manages runtime settings, user services, and automatic
login after hardware verification. Uses only built-in Ansible modules. No roles,
Galaxy collections, PXE, disk partitioning, or automatic reboots.

## Bootstrap Windows to Linux

1. Back up the Surface's files and create Windows recovery media.
2. Download and verify [Linux Mint 22.3 Xfce](https://linuxmint.com/download.php),
   then install using a USB drive and keyboard. Create a non-root user named
   `tablet`, connect Wi-Fi, and log in to the Xfce/X11 desktop.
3. Enable SSH on the tablet:

    ```bash
    sudo apt-get update
    sudo apt-get install openssh-server
    sudo systemctl enable --now ssh
    hostname -I
    ```

4. From WSL, install Ansible in a virtual environment using Python 3.12–3.14:

    ```bash
    cd /mnt/e/code/private/labv2
    python3 -m venv ansible/.venv
    ansible/.venv/bin/python -m pip install -r ansible/requirements.txt
    cp ansible/inventory.example.yaml ansible/inventory.yaml
    ```

5. Edit `ansible/inventory.yaml`: replace `ansible_host` with the tablet's address.
   Set `ansible_user` to its SSH login. `dashboard_user` defaults to `tablet`; override
   it if the desktop account has another name. Inventory is Git-ignored. Use an SSH
   key and verify the SSH host key; do not disable host-key checking or store login,
   sudo, Wi-Fi, or HA passwords in inventory. For example:

    ```bash
    ssh-copy-id tablet@YOUR_TABLET_ADDRESS
    ssh tablet@YOUR_TABLET_ADDRESS
    ```

The playbook checks the target's DMI model, architecture, OS, and existing desktop
account before provisioning. It refuses WSL and other machines.

## Prepare kernel and camera

Run from the repository root:

```bash
ansible/.venv/bin/ansible-playbook -i ansible/inventory.yaml ansible/surface-dashboard.yaml --tags prepare --ask-become-pass
```

This configures the official signed `linux-surface` APT repository, installs the
kernel and headers, touch support, packaged libcamera/GStreamer stack, Motion,
Firefox, and display utilities. It disables the distro Motion service, configures
a private loopback camera at `/dev/video42`, grants the desktop user `video` group
membership, and installs the runtime. Existing supplementary groups are retained.
It does not enable kiosk startup or automatic login during preparation.

Before rebooting, if Secure Boot is enabled, run `sudo apt install linux-surface-secureboot-mok`
in the tablet terminal and follow the package's instructions. On reboot, select
**Enroll MOK** in the blue enrollment screen and complete enrollment as described
in the project's [key-enrollment instructions](https://github.com/linux-surface/linux-surface/wiki/Installation-and-Setup#debian--ubuntu).
Then boot the Surface kernel and verify `uname -r` contains `surface`.
Keep the stock Mint kernel as a recovery option. If boot reports `bad shim signature`,
select **Advanced options for Linux Mint** in GRUB and boot a `generic` kernel
(not recovery mode), then install the enrollment package and reboot again.
If the loopback module reports `Key was rejected by service`, enroll Mint's
separate DKMS certificate with `sudo mokutil --import /var/lib/shim-signed/mok/MOK.der`,
choose a temporary password, reboot, and complete **Enroll MOK** again using that
password. This certificate is separate from the Surface kernel certificate.
The playbook does not
disable Secure Boot or automate firmware key enrollment.

Run `cam --list` on the tablet. Copy the full **front** camera ID, not its numbered
position. It commonly ends in `CAMF`; use the actual ID. If cameras are absent,
stop and follow the [camera guide](https://github.com/linux-surface/linux-surface/wiki/Camera-Support).
Pro 4 support has needed driver fixes; a bootable kernel does not prove camera
compatibility. If `/dev/video42` is missing, inspect
`journalctl -b -u systemd-modules-load` and `modinfo v4l2loopback`.

### Surface 6.19 camera regression

On `6.19.8-surface-3`, all cameras can disappear because the `dw9719` autofocus
driver lacks its I2C device ID table. The sensors appear in `media-ctl -p`, but
have zero links and the autofocus device has no bound driver. See the
[upstream diagnosis](https://github.com/linux-surface/linux-surface/issues/2225)
and [backport](https://github.com/linux-surface/linux-surface/pull/2123).

For this verified regression, run:

```bash
ansible/.venv/bin/ansible-playbook -i ansible/inventory.yaml ansible/surface-dashboard.yaml --tags camera-fix --ask-become-pass
```

Then reboot manually and run `cam --list` again. This stage installs only a
DKMS backport of `dw9719`, signed with the enrolled Mint DKMS key. The source is
Linux v6.19's `drivers/media/i2c/dw9719.c` (pristine SHA256
`d8c02aef98e575ca8d7d59debc8dee3b982b4ae49008f143b4be02bc1afd4b44`),
with the missing ID table restored. It retains the upstream SPDX license.
It is restricted to 6.19 kernels and rebuilds automatically for 6.19 updates.
Once a newer kernel includes the fix and camera capture is verified there, remove
the backport with `sudo dkms remove surface-camera-dw9719/6.19.1 --all`.
Reboot to use the upstream driver. The kiosk remains disabled until frame capture
and physical backlight checks pass.

## Verify hardware and enable the kiosk

Before testing, use Xfce Power Manager to set plugged-in system sleep and display
blanking/sleep/off timers to **Never** and disable locking on sleep. Disable the
Xfce screensaver and automatic screen lock. Keep battery emergency shutdown enabled.
Enable automatic security updates in Mint Update Manager. Verify camera operation
after kernel updates before retiring the previous working kernel.

On the tablet, in a terminal inside its X11 desktop:

```bash
python3 /usr/local/lib/surface-dashboard/dashboard.py configure 'https://hass.aumer.dev/front-door/home' 'YOUR_FRONT_CAMERA_ID'
xset +dpms
python3 /usr/local/lib/surface-dashboard/dashboard.py check
```

The check requires ten real frames through the camera bridge and turns the display
off for five seconds. **Watch that the physical backlight goes out.** Camera frames
are discarded. If either check fails, fix it before enabling the kiosk.

Back in WSL, set the exact `dashboard_camera_name` in `ansible/inventory.yaml` and
set `dashboard_hardware_verified: true` only after these checks pass. Keep the
tablet's Xfce desktop logged in; Ansible manages its systemd user services through
that session's bus. Then run:

```bash
ansible/.venv/bin/ansible-playbook -i ansible/inventory.yaml ansible/surface-dashboard.yaml --tags kiosk --ask-become-pass
```

The kiosk stage installs supervised camera, Motion, display, and browser services,
Xfce login autostart, and LightDM automatic login. It disables Light Locker's
autostart for the kiosk account and stops its running process, so DPMS wake returns
to the dashboard. Configuration belongs to the
desktop account with mode `0600`. Ansible validates it before writing and backs
up replaced configuration. Changed settings restart an already-running kiosk;
unchanged reruns do not restart it. The first launch happens from the desktop:

```bash
python3 /usr/local/lib/surface-dashboard/dashboard.py session
```

Alternatively reboot to test automatic login and launch. Ansible does not start
the browser from an SSH session with guessed display credentials.

In HA, create a dedicated **non-admin** `Front Door Tablet` account through
Settings → People → Users. Log in with that account in the kiosk. Choose **Front Door**
under Profile → Appearance; leave the instance-wide default alone. Keep login in
the browser's persistent profile, never in scripts or inventory. A non-admin user
prevents administration but does not limit control to cards visible on its dashboard.
[HA dashboard defaults](https://www.home-assistant.io/dashboards/dashboards/)

## Settings, maintenance, and acceptance

Inventory overrides the defaults in `group_vars/surface_dashboard.yaml`:

| Variable                      | Default                                  | Purpose                                           |
| ----------------------------- | ---------------------------------------- | ------------------------------------------------- |
| `dashboard_user`              | `tablet`                                 | Existing desktop account                          |
| `dashboard_url`               | `https://hass.aumer.dev/front-door/home` | HTTPS URL without credentials, query, or fragment |
| `dashboard_camera_name`       | empty                                    | Exact front-camera ID                             |
| `dashboard_idle_seconds`      | `120`                                    | Inactivity before physical display shutoff        |
| `dashboard_motion_threshold`  | `1500`                                   | Changed pixels required out of 76800              |
| `dashboard_hardware_verified` | `false`                                  | Records successful physical verification          |

Keep calibration changes in inventory and rerun `--tags kiosk`; Ansible owns
`~/.config/surface-dashboard/config.json` and overwrites manual changes there.
Increase the motion threshold to reduce false wakes or lower it for smaller movements.
Credentials never belong in these settings. If using SSH password authentication,
add `--ask-pass`; sudo credentials use `--ask-become-pass`.

The front webcam detects **movement**, not a stationary person's continued presence.
Frames are reduced to 320×240 at 5 fps before Motion detection. Recording, snapshots,
streaming, and web control are disabled. Movement or touch wakes the display locally,
including without HA or Wi-Fi. After 120 seconds without either, X11 DPMS switches
off the display. The computer and camera stay awake, so only display power is saved.
Normal hallway lighting is required; measure wall power to establish actual savings.

Before mounting, run the original hardware acceptance checks:

- Keep capture running for an hour; check all services and recent journal errors.
- After 120 seconds without activity, confirm the backlight is off. Approach and
  confirm wake within about two seconds, including with normal evening light.
- Keep moving or touching for more than 120 seconds; the screen must stay on.
  Check that lighting changes and distant movement do not cause frequent false wakes.
- Reboot and verify automatic login, authenticated dashboard, camera capture, and wake.
- Disconnect/reconnect Wi-Fi and confirm the dashboard recovers. Restart the camera
  service and terminate Firefox to test recovery. Explicitly stopping a service
  suppresses automatic restart, so it is not a crash test.
- Compare wall power with the screen on and off.

Inspect on the tablet:

```bash
systemctl --user status surface-dashboard-{camera,motion,display,browser}.service
journalctl --user -b -u 'surface-dashboard-*.service' --since '10 minutes ago'
```

For later Ansible changes, inspect the kiosk stage with `--check --diff` first.
Do this after preparation; a fresh-host prepare dry run cannot resolve packages
from a repository it has not actually added yet.

## Recovery and validation

To repeat the hardware check, stop `surface-dashboard.target` from the desktop
terminal, run `dashboard.py check`, then restart the target even if the check fails.
For a dark-screen recovery, use `Ctrl+Alt+F2` and run
`systemctl --user stop surface-dashboard.target`, then `DISPLAY=:0 xset dpms force on`
as the desktop user. `Ctrl+Alt+F7` normally returns to the desktop. Boot a previous
working kernel from GRUB if an update breaks camera support.

To disable kiosk startup, stop the target and rename its
`~/.config/autostart/surface-dashboard.desktop` file to a `.disabled` suffix. With sudo,
rename `/etc/lightdm/lightdm.conf.d/90-surface-dashboard.conf` to a `.disabled` suffix.
Remove `~/.config/autostart/light-locker.desktop` to restore Light Locker startup.
Restore the Xfce power/locking preferences and reboot. Ansible will restore those
files on the next kiosk run, so do not rerun that stage while keeping it disabled.
Configuration backups and Firefox's authenticated profile remain on the tablet.

Offline checks from the repository root:

```bash
ansible/.venv/bin/ansible-playbook -i ansible/inventory.example.yaml ansible/surface-dashboard.yaml --syntax-check
ansible/.venv/bin/python -m unittest discover -s ansible/tests -p 'test_*.py' -v
git diff --check
oxfmt --check ansible/README.md ansible/*.yaml ansible/group_vars/*.yaml
```

Tests cover the runtime's idle timing, motion resets, private config, privacy
defaults, and capture-failure cleanup, plus service-template rendering. Local
validation cannot prove package installation, hardware behavior, power savings,
or on-device Ansible idempotence. No live tablet changes have been made from WSL.
