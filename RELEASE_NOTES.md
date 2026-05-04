# Release Notes

## Update workflow

### `autorun/update.sh`

A new script, `autorun/update.sh`, handles applying code updates to a running
installation. It reinstalls the Python package into the conda environment
(picking up any new dependencies) and restarts the system services.

**Important:** the script does not fetch new code from GitHub. You must obtain
the updated source first — either by pulling from the repository or copying
files from another source — before running the script:

```bash
cd ~/projects/ancs4linux
git pull origin develop
sudo autorun/update.sh
systemctl --user restart ancs4linux-desktop-integration.service
systemctl --user restart ancs4linux-enable-advertising.service
```

## Bug fixes from initial Fedora Silverblue installation

### Re-advertising fails after BlueZ DiscoverableTimeout expires

By default, BlueZ turns off `Discoverable` and `Pairable` after 180 seconds.
When `enable-advertising` was called again after that window (e.g. manually, or
via the systemd service on a second attempt), the advertising daemon would try to
unregister the advertisement before re-registering it. BlueZ had already expired
the advertisement, so `UnregisterAdvertisement` returned `Does Not Exist` and the
whole call failed.

The `disable_advertising` path now catches that error and treats it as a no-op —
BlueZ having already cleaned up is not a problem.

### ANCS subscription blocked when device name arrives before pairing completes

When a device pairs, BlueZ sends `Alias` and `Paired` as separate
`PropertiesChanged` signals, and `Alias` typically arrives first. The observer
only tracks a device once `Paired` becomes `True`, so the earlier `Alias` signal
was silently dropped and `self.name` was never set.

Because `try_subscribe` requires all of `paired`, `services_resolved`, `name`,
and the three ANCS GATT characteristics before subscribing, a missing name
blocked the ANCS connection indefinitely — even though the device showed as
paired and services were resolved.

Fixed by reading the current `Alias` from the full device properties at the
moment `Paired` fires, so the name is always seeded regardless of signal ordering.

## Notification reliability improvements

Four fixes to prevent notifications from being silently dropped.

### Pre-existing notifications now shown

Notifications that fired before the ANCS connection was established (e.g.,
a reminder that triggered while the computer was rebooting or BLE was
reconnecting) were previously filtered out and never shown. iOS marks these
with a `PreExisting` flag, and the original code treated them the same as
`NotificationRemoved` and discarded them.

Any notification that is still active in ANCS has not yet been dismissed on
the iPhone, so it is still relevant. The `PreExisting` filter has been
removed — all `NotificationAdded` and `NotificationModified` events are now
forwarded regardless of the flag.

### App name lookup timeout

When a notification arrives from an app whose name has not been seen before,
the observer sends a `GetAppAttributes` request to the iPhone and holds the
notification in a queue until the response arrives. If the iPhone never
responded (brief Bluetooth hiccup, device busy), the notification would sit
in the queue indefinitely and never be shown.

The observer now sets a 5-second timeout for each app name request. If no
response arrives in time, the notification is released using the app's bundle
ID as the display name rather than being lost.

### Packet handler error isolation

An unexpected or malformed BLE packet in either the notification source or data
source handler would throw an unhandled exception, which left subsequent packets
unprocessed until the next BLE event fired. Both handlers now catch and log
parse errors so a bad packet is skipped without affecting anything that follows.

### Emit failure logging

If the D-Bus call to show a notification failed after the notification had
already been dequeued, it was lost with no indication in the logs. The emit
call is now wrapped so failures are logged rather than silently discarded.

## Automatic LE reconnect after reboot

After a reboot, BlueZ defaults to using BR/EDR (classic Bluetooth) when
reconnecting to a paired iPhone. A BR/EDR connection sets `ServicesResolved`
but exposes no GATT characteristics, so ANCS never subscribes and no
notifications are forwarded.

This release fixes that with an automatic reconnect loop:

- On startup, the observer identifies paired iOS devices (those whose cached
  BlueZ UUIDs include the ANCS service UUID) and starts a **Reconnector** for
  each one.
- The Reconnector writes `LastUsedBearer=le` to the BlueZ device info file
  before each connection attempt, forcing BlueZ to use Bluetooth LE rather than
  BR/EDR.
- `Device1.Connect()` is retried every 30 seconds until the iPhone accepts the
  LE connection — which happens as soon as the phone becomes BLE-active (screen
  on, notification received, etc.).
- The observer also runs `StartDiscovery` with an LE transport filter so BlueZ
  can resolve the iPhone's rotating private address (RPA) and reach it
  successfully.
- Once ANCS subscribes, the reconnect loop and discovery both stop
  automatically.

The BLE advertisement now also includes a `SolicitUUIDs` field with the ANCS
UUID, which signals iOS that this device is an ANCS notification consumer.

## Fedora Silverblue install notes

### Reboot required after install when systemd lingering is enabled

After `autorun/install.sh` runs, the user is added to the `ancs4linux`
group. The README previously said to log out and back in for the group
to take effect. This is not sufficient when systemd lingering is enabled.

When lingering is on (`loginctl show-user $USER | grep Linger` returns
`Linger=yes`), `systemd --user` is kept alive across logout and never
restarts. It retains its old group credentials, so processes it spawns —
including the `ancs4linux-desktop-integration` service — are not in the
`ancs4linux` group and fail with a D-Bus authorization error.

A full reboot is required. After rebooting, `systemd --user` starts
fresh with the updated group membership and the services come up cleanly.

## Fedora Silverblue support

### Automated install via `autorun/install.sh`

The install script now works on Fedora Silverblue. It writes systemd service
files to `/etc/systemd/system/` and `/etc/systemd/user/` (the writable
locations on an immutable OS), detects the conda environment automatically, and
configures SELinux file contexts so system services can execute binaries in the
user home directory.

### Automatic Bluetooth advertising at login

A new user service (`ancs4linux-enable-advertising.service`) runs
`enable-advertising` automatically after `ancs4linux-desktop-integration`
starts, so the iPhone reconnects silently on every login without any manual
steps. The Bluetooth device name defaults to the system hostname and can be
overridden via `~/.config/ancs4linux/advertising.env`.

### Apostrophe display fix

Notification titles and bodies containing apostrophes were displayed as the
literal string `&#x27;` by some notification daemons. The HTML escaping now
uses `quote=False`, which still protects against Pango markup injection via
`&`, `<`, and `>` but leaves `'` and `"` unmodified.

## Security fixes (fork of pzmarzly/ancs4linux)

This fork applies three security fixes to the original ancs4linux project.

### Bluetooth pairing now requires explicit user confirmation

Previously, any device within Bluetooth range could complete pairing while
advertising was active without the user taking any action. The passkey
notification arrived only after pairing had already been approved.

Pairing now blocks until the user clicks **Confirm** or **Deny** in a desktop
notification. Requests that are ignored time out and are rejected after 30
seconds. A `Cancel` from the Bluetooth stack also rejects immediately.

### Notification content is sanitized before display

Notification titles and bodies received over Bluetooth are now HTML-escaped
before being passed to the desktop notification daemon. This prevents a paired
device from injecting Pango markup or HTML into notifications, which some
notification daemons (dunst, mako) would otherwise render.

### D-Bus system policies restrict per-method and per-signal access

The D-Bus policies for the observer and advertising system services previously
granted the `ancs4linux` group unrestricted send and receive rights. The
policies now explicitly enumerate which methods group members may call and which
signals they may receive, reducing the ability of a compromised process running
as the user to interact with or eavesdrop on the services beyond what normal
operation requires.
