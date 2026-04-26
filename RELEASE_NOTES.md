# Release Notes

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
