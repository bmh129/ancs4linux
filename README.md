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

After rebooting, the user services start automatically on login. No manual
steps are needed.

### Updating to a newer version

First, obtain the new code — either by pulling from GitHub or copying it from
another source. `update.sh` does **not** fetch code for you; that step must be
done beforehand:

```bash
cd ~/projects/ancs4linux
git pull origin develop
```

Then run the update script (requires sudo). It reinstalls the Python package
into the conda environment (picking up any new dependencies), restarts the
system services, and restarts the user services for your current session:

```bash
sudo autorun/update.sh
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

Then start advertising. `install.sh` adds `ancs4linux-ctl` to `~/bin`, so it
is available in any terminal without activating the conda environment:

```bash
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

`autorun/install.sh` handles this automatically by installing a WirePlumber
rule that sets the audio profile to `off` for any paired Bluetooth phone:

```
~/.config/wireplumber/wireplumber.conf.d/51-disable-phone-audio.conf
```

The rule matches on `device.form-factor = "phone"`, which PipeWire exposes
for any Bluetooth phone. Other Bluetooth audio devices (headphones, speakers)
are unaffected. The ANCS notification connection runs over BLE and is also
unaffected.

If you installed before this rule was added, install it manually:

```bash
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
cp autorun/51-disable-phone-audio.conf \
    ~/.config/wireplumber/wireplumber.conf.d/
systemctl --user restart wireplumber
```

## Hardware Compatibility

A Bluetooth adapter that supports BLE pairing is required. iOS only grants ANCS
notification access to BLE-bonded accessories — a classic BR/EDR-only bond is
not sufficient, even if the adapter supports BLE in other respects.

**Known working:** The following adapters pair via BLE correctly and iOS
will display the notification-permission prompt after pairing:

- **Intel** M.2/PCIe cards (e.g., Intel AX210, Intel BE200): recommended.
  These work reliably with BlueZ's ll-privacy implementation and the iPhone
  connects automatically after reboot without manual intervention.

**Known unreliable:**

- **Qualcomm Atheros QCA6174** combo adapter (802.11ac Wi-Fi + Bluetooth 4.2):
  a BlueZ bug with ll-privacy causes outgoing LE connections to always time out
  (41s each). The intended workaround — having the iPhone connect inward via a
  solicitation advertisement — did not work reliably in practice. Replacing the
  card with an Intel AX210 resolved the issue.

**Known not working:** Realtek Bluetooth adapters (e.g. the chip used in the
**TP-Link UB500 Plus**) cannot achieve a BLE bond with an iPhone on Linux.
When discoverable, iOS pairs via classic BR/EDR instead of BLE, and the Realtek
firmware negotiates P-192 keys (not P-256/Secure Connections), so cross-transport
key derivation (CTKD) cannot derive a BLE bond from the BR/EDR pairing either.
ANCS authorization is never granted. Note that ANCS may appear to function
temporarily immediately after the initial pairing, but this is not persistent —
it will be lost after a reboot.

**USB Bluetooth adapters:** No USB Bluetooth adapter is currently known to work
with this project on Linux. If you have confirmed a USB adapter that achieves a
proper BLE bond with an iPhone and receives ANCS notifications, please open an
issue with the chipset and adapter details.

## Opening a URL or App from a Notification

Every notification includes an **Open** button. Clicking it opens a browser or
launches an app based on a per-app mapping you configure. If no mapping exists
for an app, it falls back to a Google "I'm Feeling Lucky" search on the app name.

Mappings are stored in `~/.config/ancs4linux/open_url.json` and managed with
`ancs4linux-ctl`. You can key a mapping on either the iOS bundle ID (more precise)
or the human-readable app name — the bundle ID takes priority when both match.

### Finding an app's bundle ID

To map by bundle ID you first need to know what it is. Trigger a notification
from the app on your iPhone, then check the service log:

```bash
journalctl --user -u ancs4linux-desktop-integration --no-pager | grep app_id | tail -20
```

Each line looks like:

```
... Shown 42 from Outlook (app_id=com.microsoft.Outlook).
```

The value in parentheses is the bundle ID to use as the key.

### Setting a URL

```bash
# By bundle ID (more precise — survives an app rename)
ancs4linux-ctl set-url --key "com.microsoft.Outlook" --target "https://outlook.com"

# By app name (easier when you don't know the bundle ID)
ancs4linux-ctl set-url --key "Mail" --target "https://outlook.com"
```

Common mail targets: `https://outlook.com`, `https://mail.google.com`,
`https://mail.proton.me`

### Launching a Linux app instead of a URL

Prefix the value with `app://` followed by the command to run:

```bash
ancs4linux-ctl set-url --key "com.spotify.client" --target "app://spotify"

# Flatpak apps work too
ancs4linux-ctl set-url --key "com.spotify.client" --target "app://flatpak run com.spotify.Client"
```

### Listing and removing mappings

```bash
# Show all configured mappings
ancs4linux-ctl list-urls

# Remove a mapping (reverts to Google fallback)
ancs4linux-ctl remove-url --key "com.microsoft.Outlook"

# Debug: show what would be opened for a given app
ancs4linux-ctl resolve-url --app-id "com.microsoft.Outlook" --app-name "Outlook"
```

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
