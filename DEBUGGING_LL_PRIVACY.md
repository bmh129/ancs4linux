# BlueZ ll-privacy Bug: ANCS Connection Failures on Linux

## Summary

After a Bluetooth service restart, `ancs4linux` fails to establish an LE connection to
the iPhone. Reconnector attempts time out every 41 seconds with `le-connection-abort-by-local`.

## Root Cause: BlueZ 5.86 ll-privacy Address Type Bug

With ll-privacy enabled (automatic on Qualcomm adapters in kernel 7.x), BlueZ uses the
**wrong address type** in the `LE Create Connection` HCI command:

### Phase 1 — Identity address with wrong type

BlueZ sends:
```
LE Create Connection
  Peer address type: Public (0x00)   ← should be Public Identity (0x02)
  Peer address: 1C:3C:78:DE:D3:65   (iPhone identity address)
```

With `Peer address type = 0x00`, the controller does NOT consult the Resolving List.
It looks for a device advertising its public identity address directly — but the iPhone
only advertises with a Resolvable Private Address (RPA). Result: 41-second timeout.

### Phase 2 — RPA with wrong type (after internal scan)

After the timeout, BlueZ scans for the iPhone's current RPA, finds it
(`59:E3:3F:E8:83:22`), stops scanning, then sends:

```
LE Create Connection
  Peer address type: Public (0x00)   ← should be Random (0x01)
  Peer address: 59:E3:3F:E8:83:22   (iPhone's current RPA)
  Own address type: Public (0x00)
```

`Peer address type = 0x00` for an RPA is wrong — RPAs have bits 7:6 = 01, indicating
they are Random type addresses. The controller looks for a Public address
`59:E3:3F:E8:83:22` in advertisements, but the iPhone advertises as type Random.
Result: another 41-second timeout.

### Evidence: btmon trace

```
# Phase 1 (identity address attempt, timed out before btmon started)
@ MGMT Event: Connect Failed     LE Address: 1C:3C:78:DE:D3:65  Status: Timeout

# BlueZ scans to find current RPA
< HCI Command: LE Set Scan Enable   Scanning: Enabled
> HCI Event: LE Advertising Report  Address type: Random  Address: 59:E3:3F:E8:83:22 (Resolvable)
@ MGMT Event: Device Found          LE Address: 1C:3C:78:DE:D3:65  ← controller resolved RPA!

# Phase 2 (connects to RPA with wrong type)
< HCI Command: LE Create Connection
    Peer address type: Public (0x00)    ← BUG: should be Random (0x01)
    Peer address: 59:E3:3F:E8:83:22
> HCI Event: Command Status   Status: Success  ← HCI accepted command
# (41s later: timeout because controller can't find a Public address 59:E3:3F:E8:83:22)
```

## Key Observations

- **ll-privacy UUID** (`15c0a148-c273-11ea-b3de-0242ac130004`) does NOT appear in
  `btmgmt expinfo` — it is enabled by the Qualcomm adapter/kernel driver, not via
  BlueZ's experimental feature mechanism. `KernelExperimental = false` in `main.conf`
  cannot disable it.

- **IRK is stored**: `/var/lib/bluetooth/3C:91:80:BA:A8:10/1C:3C:78:DE:D3:65/info`
  contains `[IdentityResolvingKey] Key=65AFDC7CA1F12B0D1C570BE513DEEA37`. The
  controller's Resolving List IS populated and DOES correctly resolve iPhone RPAs to
  the identity address `1C:3C:78:DE:D3:65` (confirmed in btmon Device Found events).
  The bug is only in the `LE Create Connection` address type selection.

- **iPhone IS advertising**: RSSI -43 to -50 dBm visible; RPA `59:E3:3F:E8:83:22`
  is resolved to identity address in MGMT events. The iPhone's side is fine.

- **Stale pending LE Create Connection**: Before a bluetooth service restart, a
  `Command Disallowed (0x0c)` error is returned immediately on every `LE Create
  Connection` attempt because a previous LE Create Connection is permanently pending
  in the HCI controller. Fix: `sudo systemctl restart bluetooth`.

## System Info

| Item | Value |
|------|-------|
| BlueZ version | 5.86-4.fc44 |
| Kernel | 7.0.8-200.fc44.x86_64 |
| Adapter | Qualcomm (`manufacturer 29`, `wide-band-speech ll-privacy` in current settings) |
| iPhone | 1C:3C:78:DE:D3:65 (public identity address) |

## Code Fix Applied (scanner.py)

### Problem: advertising never restored after Reconnector failure

When the Reconnector starts, it calls `_pause_advertising()` to track the advertising
state. When `Device1.Connect()` fails and the device is still disconnected, the original
code did nothing — advertising stayed paused indefinitely. This prevented iOS from
initiating a BLE connection to Linux from its side.

### Fix 1: `on_failure` callback

Added `on_failure: Optional[Callable[[], None]]` to `Reconnector`. After
`Connect()` fails with the device still disconnected, `on_failure()` is called, which
triggers `_resume_advertising()`. This starts BLE advertising with `SolicitUUIDs =
[ANCS_UUID]`, allowing iOS to connect to Linux from its side.

### Fix 2: `_in_progress` guard

Added `_in_progress: bool = False` to `Reconnector`. The 30-second GLib timer can
fire a second `_attempt()` while the first is still blocking inside `Connect()` (the
GLib main loop continues processing during blocking D-Bus calls). The guard prevents
concurrent/stacked attempts.

### Fix 3: No advertising pause during Reconnector loop

Removed `on_attempt=partial(self._pause_advertising, path)` from the Reconnector
constructor calls. Without this, advertising is only paused at startup (by the
explicit `_pause_advertising()` call before `r.start()`), and restored after the
first failure. Subsequent Reconnector attempts do not re-pause advertising, so iOS
has a continuous advertising signal to connect to.

## Remaining Issues

### 1. DiscoverableTimeout resets Discoverable to False after 3 minutes

BlueZ's default `DiscoverableTimeout = 180` reverts the adapter to
`Discoverable=False` 3 minutes after `enable_advertising()` sets it to `True`.
With `Discoverable=False`, the "LE General Discoverable Mode" bit may be absent
from the advertisement Flags, potentially preventing iOS from seeing/responding
to our advertisement.

**Fix**: Add to `/etc/bluetooth/main.conf`:
```ini
[General]
DiscoverableTimeout = 0
```
Then `sudo systemctl restart bluetooth`.

### 2. BlueZ ll-privacy bug is not fixed

The underlying `LE Create Connection` address type bug remains in BlueZ 5.86.
The `on_failure` → advertising workaround means iOS connects TO Linux (incoming),
rather than Linux connecting TO iOS (outgoing). This is the correct ANCS flow
regardless — iOS is the GATT server and should initiate the BLE connection.

Long-term fix: file a bug against BlueZ or patch `src/device.c` where
`LE Create Connection` address type is determined when ll-privacy is active.
The fix should use:
- `Peer address type: Public Identity (0x02)` when using the stored identity address
- OR `Peer address type: Random (0x01)` when using the current RPA from scan results

### 3. Previously-working flow (before BT restart)

Before a bluetooth service restart, ANCS worked via BR/EDR connection:
- iPhone connects to Linux via BR/EDR
- Observer detects `Connected=True` + `ServicesResolved=True` via BR/EDR SDP
- GATT discovery triggered on the existing connection

After a BT restart disrupts this, the ll-privacy bug prevents the outgoing LE
reconnect, making the `on_failure` advertising workaround essential.

## Workaround Summary for Users

If ANCS stops working after a Bluetooth restart:

1. Ensure ancs4linux services are running:
   ```
   sudo systemctl status ancs4linux-observer ancs4linux-advertising
   ```

2. Wait for the observer to start advertising (check logs for
   `Resumed advertising on ...`). This happens after the first Reconnector
   failure (~45 seconds after service start).

3. On the iPhone: toggle Bluetooth off, wait 5 seconds, then toggle Bluetooth back on.
   iOS does not reliably auto-connect to the solicitation advertisement on its own —
   the BT toggle forces an immediate re-scan and connection.

4. Once connected, check for `Asking for notifications: success.` in observer logs.
