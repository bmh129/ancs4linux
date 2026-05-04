# ANCS4Linux

> iOS & iPadOS notification service client for GNU/Linux

This project lets you receive iOS and iPadOS notifications on your Linux computer. No jailbreak needed.

It uses Apple Notification Center Service (ANCS) - the same protocol that smartwatches use. Bluetooth 4.0 (Low Energy) is required.

## Running

### Fedora Silverblue

`gobject-introspection` is already present in the Silverblue base image.

If conda is not yet installed, install [Miniforge3](https://github.com/conda-forge/miniforge) first:

```bash
curl -fsSL "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh" -o /tmp/miniforge3.sh
bash /tmp/miniforge3.sh -b -p "$HOME/miniforge3"
```

Then install the Python bindings and project dependencies in a conda environment.
Note: `pip` is not included in the base env by default, so install it explicitly:

```bash
conda create -n ancs4linux python=3.11
conda activate ancs4linux
conda install -c conda-forge pygobject
conda install pip
pip install -e .
```

Then run the install script (requires sudo). It sets up D-Bus policy files,
creates the `ancs4linux` group, writes systemd service files to writable
locations, configures SELinux file contexts, and enables everything:

```bash
sudo autorun/install.sh
```

The install script adds your user to the `ancs4linux` group, but group membership
only takes effect once `systemd --user` restarts with the new credentials.
**Reboot before starting the user services**, otherwise they will fail with a
D-Bus authorization error.

A logout/login is not sufficient if systemd lingering is enabled
(`loginctl show-user $USER | grep Linger`). When lingering is on, `systemd --user`
survives logout and keeps its old group credentials — only a full reboot
restarts it with the updated group membership.

After rebooting, start the user services for your current session (future
logins start them automatically):

```bash
systemctl --user daemon-reload
systemctl --user start ancs4linux-desktop-integration.service
systemctl --user start ancs4linux-enable-advertising.service
```

### Updating to a newer version

First, obtain the new code — either by pulling from GitHub or copying it from
another source. `update.sh` does **not** fetch code for you; that step must be
done beforehand:

```bash
cd ~/projects/ancs4linux
git pull origin develop
```

Then run the update script (requires sudo). It reinstalls the Python package
into the conda environment (picking up any new dependencies) and restarts the
system services:

```bash
sudo autorun/update.sh
```

Finally, restart the user services for your current session:

```bash
systemctl --user restart ancs4linux-desktop-integration.service
systemctl --user restart ancs4linux-enable-advertising.service
```

By default the Bluetooth device name is your system hostname. To use a custom
name, create `~/.config/ancs4linux/advertising.env` before starting the service:

```
ANCS4LINUX_DEVICE_NAME=my-laptop
```

### Pairing your iPhone (first time)

If the devices were previously paired outside of ancs4linux, unpair them on
both ends first:

```bash
bluetoothctl remove <MAC>   # find the MAC with: bluetoothctl devices
```

On iPhone: Settings → Bluetooth → tap ⓘ next to the computer → Forget This Device.

Then start advertising. The `ancs4linux-ctl` binary lives inside the conda
environment, so either activate it first or use the full path:

```bash
conda activate ancs4linux
address=$(ancs4linux-ctl get-all-hci | python3 -c "import sys,json; print(json.load(sys.stdin)[0])")
ancs4linux-ctl enable-advertising --hci-address="$address" --name="$(hostname -s)"
# This may take 30 seconds. Do not attempt to connect until it finishes.
```

On your iPhone, open Settings → Bluetooth. Tap `my-laptop` when it appears. A
desktop notification with **Confirm** and **Deny** buttons will appear — confirm
only if the passkey shown on the iPhone matches. Once paired, the iPhone will
warn you that notifications will be forwarded to the computer.

### Starting advertising after a reboot (already paired)

Re-pairing is not needed after the first time. After installing via
`autorun/install.sh`, advertising is enabled automatically at login and
the iPhone reconnects via Bluetooth LE automatically — typically within
30–60 seconds of the computer booting, with no manual steps needed.

### Preventing iPhone audio from routing to the computer

When the iPhone connects via Bluetooth, it establishes both a BLE connection
(used by ANCS for notifications) and a classic Bluetooth connection that
includes A2DP and HFP audio profiles. Without additional configuration,
WirePlumber (PipeWire's session manager) will activate these audio profiles,
causing iPhone audio to route to the computer's speakers and media controls
to appear in the GNOME notification area.

To prevent this, create a WirePlumber rule that sets the iPhone's Bluetooth
audio profile to `off`. Replace `WALTER-PE` with your iPhone's Bluetooth
name (visible in `bluetoothctl devices` or GNOME Bluetooth settings):

```bash
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
```

Create `~/.config/wireplumber/wireplumber.conf.d/51-disable-iphone-audio.conf`
with the following contents:

```
monitor.bluez.rules = [
  {
    matches = [
      {
        device.alias = "WALTER-PE"
      }
    ]
    actions = {
      update-props = {
        bluez5.profile = "off"
      }
    }
  }
]
```

Then restart WirePlumber:

```bash
systemctl --user restart wireplumber
```

This rule is matched by device name so it only affects the iPhone — other
Bluetooth audio devices (headphones, speakers) are unaffected. The ANCS
notification connection runs over BLE and is also unaffected.

## TODO

- [x] Write a systemd user service drop-in (or wrapper script) that runs
      `enable-advertising` automatically after `ancs4linux-advertising.service`
      starts, so no manual command is needed after login.
- [x] Test and document the `autorun/install.sh` path for Fedora Silverblue so
      the three services start automatically via systemd on boot/login.
- [ ] Investigate whether `rpm-ostree` layering or a Toolbox/Distrobox container
      is the better long-term packaging approach for Silverblue.
- [ ] Add an option to `autorun/update.sh` to pull a specified branch from
      GitHub before reinstalling and restarting services.

## Integration

If you want to do some scripting, you can observe ancs4linux DBus APIs. [How to](https://askubuntu.com/questions/150790/how-do-i-run-a-script-on-a-dbus-signal).

## Alternatives

- Pusher - jailbreak required
- ForwardNotifier - jailbreak required
- Dell Mobile Connect - Windows 10 required

## Backlinks

- [original Reddit announcement](https://www.reddit.com/r/linux/comments/gks3bt/ios_notifications_on_linux_desktop_over_bluetooth/)
