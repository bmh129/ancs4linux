# Release Notes

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
