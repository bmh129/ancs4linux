import logging
import os
import re
import socket
from functools import partial
from typing import Callable, Dict, List, Optional

import gi  # type: ignore

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # type: ignore

from ancs4linux.common.apis import AdvertisingAPI, ObserverAPI
from ancs4linux.common.dbus import ObjPath, Str, Variant
from ancs4linux.common.external_apis import (
    BluezAdapterAPI,
    BluezDeviceAPI,
    BluezGattCharacteristicAPI,
    BluezRootAPI,
)
from ancs4linux.observer.ancs.constants import (
    ANCS_CHARS,
    CONTROL_POINT_CHAR,
    DATA_SOURCE_CHAR,
    NOTIFICATION_SOURCE_CHAR,
)
from ancs4linux.observer.device import MobileDevice

log = logging.getLogger(__name__)

ANCS_SERVICE_UUID = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
LE_RECONNECT_INTERVAL_S = 30


class Reconnector:
    """Periodically writes LastUsedBearer=le and calls Device1.Connect() so BlueZ
    uses LE rather than BR/EDR when reconnecting to the iPhone after a reboot."""

    def __init__(
        self,
        device_path: ObjPath,
        adapter_mac: str,
        on_in_progress: Optional[Callable[[], None]] = None,
        on_attempt: Optional[Callable[[], None]] = None,
        on_failure: Optional[Callable[[], None]] = None,
    ) -> None:
        self.device_path = device_path
        device_mac = device_path.split("/")[-1][4:].replace("_", ":")
        self.info_path = f"/var/lib/bluetooth/{adapter_mac}/{device_mac}/info"
        self.device_proxy = BluezDeviceAPI.connect(device_path)
        self._timeout_id: Optional[int] = None
        self._active = False
        self._in_progress = False
        self._on_in_progress = on_in_progress
        self._on_attempt = on_attempt
        self._on_failure = on_failure

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        log.info(f"Starting LE reconnect loop for {self.device_path}")
        self._attempt()
        self._timeout_id = GLib.timeout_add_seconds(
            LE_RECONNECT_INTERVAL_S, self._attempt
        )

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None
        log.info(f"Stopped LE reconnect loop for {self.device_path}")

    def _attempt(self) -> bool:
        if not self._active:
            return False
        # Guard against re-entrancy: Connect() blocks for up to 41 s while the
        # GLib main loop continues running, so the 30-second repeat timer can
        # fire a second _attempt() before the first one finishes.
        if self._in_progress:
            return self._active
        self._in_progress = True
        try:
            self._set_le_bearer()
            if self._on_attempt:
                self._on_attempt()
            try:
                self.device_proxy.Connect()
                log.debug(f"LE connect initiated for {self.device_path}")
            except Exception as e:
                log.debug(f"LE connect attempt failed for {self.device_path}: {e}")
                if self._on_in_progress:
                    # On any failure, check whether the device is already connected
                    # (e.g. BR/EDR from the iPhone side).  If so, stop the outgoing LE
                    # loop and hand off to GATT discovery instead.
                    try:
                        props = self.device_proxy.GetAll("org.bluez.Device1")
                        if props.get("Connected", Variant("b", False)).unpack():
                            self.stop()
                            self._on_in_progress()
                            return False
                    except Exception:
                        pass
                # Device is still disconnected. Resume advertising so iOS can
                # initiate the BLE connection from its side (workaround for the
                # BlueZ ll-privacy bug that sends wrong address type in
                # LE Create Connection, causing outgoing attempts to time out).
                if self._on_failure:
                    self._on_failure()
        finally:
            self._in_progress = False
        return self._active

    def _set_le_bearer(self) -> None:
        try:
            with open(self.info_path) as f:
                content = f.read()
            patched = re.sub(r"LastUsedBearer=\w+", "LastUsedBearer=le", content)
            if patched == content and "LastUsedBearer=" not in content:
                patched += "LastUsedBearer=le\n"
            if patched != content:
                with open(self.info_path, "w") as f:
                    f.write(patched)
        except Exception as e:
            log.warning(f"Could not set LE bearer in {self.info_path}: {e}")


class Scanner:
    def __init__(self, server: ObserverAPI):
        self.server = server
        self.root = BluezRootAPI.connect()
        self.devices: Dict[str, MobileDevice] = {}
        self.property_observers: Dict[str, BluezDeviceAPI] = {}
        self._adapters: Dict[str, BluezAdapterAPI] = {}
        self._adapter_macs: Dict[str, str] = {}
        self._reconnectors: Dict[str, Reconnector] = {}
        self._discovery_active: bool = False
        self._advertising_api: Optional[AdvertisingAPI] = None
        self._paused_advertising: Dict[str, str] = {}  # hci_address -> name

    def start_observing(self) -> None:
        self.root.InterfacesAdded.connect(self.process_object)
        self.root.InterfacesRemoved.connect(self.remove_observers)
        managed = self.root.GetManagedObjects()
        # First pass: collect adapters so we can reset them before any device
        # processing. process_object() may create Reconnectors that call Connect()
        # immediately, so the reset must happen before that.
        for path, services in managed.items():
            if "org.bluez.Adapter1" in services:
                self._adapters[path] = BluezAdapterAPI.connect(path)
                self._adapter_macs[path] = (
                    services["org.bluez.Adapter1"]["Address"].unpack()
                )
        # Reset adapters to non-discoverable so iOS cannot auto-connect via BR/EDR
        # while we establish an outgoing LE connection. This corrects leftover state
        # from a previous service session that may have left Discoverable=True.
        for path, adapter in self._adapters.items():
            try:
                adapter.Discoverable = False
                adapter.Pairable = False
                log.debug(f"Reset adapter {path} to non-discoverable")
            except Exception as e:
                log.debug(f"Could not reset adapter state on {path}: {e}")
        # Second pass: process all objects (may create and start Reconnectors).
        for path, services in managed.items():
            self.process_object(path, services)
        self._start_discovery()
        self._start_ios_reconnectors(managed)

    def _get_adapter_mac(self, device_path: ObjPath) -> Optional[str]:
        adapter_path = "/".join(device_path.split("/")[:-1])
        return self._adapter_macs.get(adapter_path)

    def _get_advertising_api(self) -> Optional[AdvertisingAPI]:
        if self._advertising_api is None:
            try:
                self._advertising_api = AdvertisingAPI.connect("ancs4linux.Advertising")
            except Exception as e:
                log.debug(f"Advertising service not available: {e}")
        return self._advertising_api

    def _pause_advertising(self, device: ObjPath) -> None:
        """Stop advertising so an outgoing LE connect can proceed without conflict."""
        api = self._get_advertising_api()
        if api is None:
            return
        hci_address = self._get_adapter_mac(device)
        if hci_address is None or hci_address in self._paused_advertising:
            return
        name = os.environ.get("ANCS4LINUX_DEVICE_NAME", socket.gethostname().split(".")[0])
        try:
            api.DisableAdvertising(hci_address)
            self._paused_advertising[hci_address] = name
            log.info(f"Paused advertising on {hci_address} to unblock GATT discovery")
        except Exception as e:
            if "No advertisement found" in str(e):
                # A previous session disabled advertising without re-enabling it.
                # Track the address so _resume_advertising can restore it after
                # GATT discovery, breaking the BR/EDR-only reconnect deadlock.
                self._paused_advertising[hci_address] = name
                log.debug(f"No active advertisement on {hci_address}, will restore after GATT discovery")
            else:
                log.debug(f"Could not pause advertising on {hci_address}: {e}")

    def _resume_advertising(self, device: ObjPath) -> None:
        """Re-enable advertising after GATT discovery completes or fails."""
        hci_address = self._get_adapter_mac(device)
        if hci_address is None or hci_address not in self._paused_advertising:
            return
        name = self._paused_advertising.pop(hci_address)
        api = self._get_advertising_api()
        if api is None:
            return
        try:
            api.EnableAdvertising(hci_address, name)
            log.info(f"Resumed advertising on {hci_address}")
        except Exception as e:
            log.warning(f"Could not resume advertising on {hci_address}: {e}")

    def _start_ios_reconnectors(
        self, managed: Dict[ObjPath, Dict[Str, Dict[Str, Variant]]]
    ) -> None:
        for path, services in managed.items():
            if "org.bluez.Device1" not in services:
                continue
            props = services["org.bluez.Device1"]
            paired = props.get("Paired", Variant("b", False)).unpack()
            if not paired:
                continue
            uuids = props.get("UUIDs", Variant("as", [])).unpack()
            if ANCS_SERVICE_UUID not in uuids:
                continue
            adapter_mac = self._get_adapter_mac(path)
            if adapter_mac and path not in self._reconnectors:
                r = Reconnector(
                    path,
                    adapter_mac,
                    on_in_progress=partial(self._trigger_gatt_discovery, path),
                    on_failure=partial(self._resume_advertising, path),
                )
                self._reconnectors[path] = r
                connected = props.get("Connected", Variant("b", False)).unpack()
                if connected:
                    # Already connected (likely BR/EDR). Don't fight the incoming
                    # BLE connection from advertising; trigger GATT discovery instead.
                    GLib.timeout_add_seconds(3, partial(self._trigger_gatt_discovery, path))
                else:
                    self._pause_advertising(path)
                    r.start()

    def process_object(
        self, path: ObjPath, services: Dict[Str, Dict[Str, Variant]]
    ) -> None:
        if BluezDeviceAPI.interface in services:
            if path not in self.property_observers:
                self.property_observers[path] = BluezDeviceAPI.connect(path)
                self.property_observers[path].PropertiesChanged.connect(
                    partial(self.process_property, path)
                )
                self.process_property(
                    path,
                    BluezDeviceAPI.interface,
                    services[BluezDeviceAPI.interface],
                    [],
                )

        if BluezGattCharacteristicAPI.interface in services:
            uuid = services[BluezGattCharacteristicAPI.interface]["UUID"].unpack()
            if uuid in ANCS_CHARS:
                # Bluez path hierarchy: …/hciN/dev_XX/serviceYYYY/charZZZZ — drop last 2 segments to get device path.
                device = "/".join(path.split("/")[:-2])
                self.devices.setdefault(
                    device,
                    MobileDevice(
                        device,
                        self.server,
                        on_subscribed=partial(self._on_subscribed, device),
                        on_unsubscribed=partial(self._on_unsubscribed, device),
                    ),
                )
                if uuid == NOTIFICATION_SOURCE_CHAR:
                    self.devices[device].set_notification_source(path)
                elif uuid == CONTROL_POINT_CHAR:
                    self.devices[device].set_control_point(path)
                elif uuid == DATA_SOURCE_CHAR:
                    self.devices[device].set_data_source(path)

    def process_property(
        self,
        device: ObjPath,
        interface: str,
        changes: Dict[str, Variant],
        invalidated: List[str],
    ) -> None:
        if interface != BluezDeviceAPI.interface:
            return
        connected_change = changes.get("Connected")
        if connected_change is not None and device in self._reconnectors:
            if connected_change.unpack():
                # Device connected (incoming BLE via advertising solicitation, or BR/EDR).
                # Stop the outgoing reconnect loop so it doesn't fight the incoming
                # connection, then trigger GATT discovery on the existing connection.
                self._reconnectors[device].stop()
                GLib.timeout_add_seconds(3, partial(self._trigger_gatt_discovery, device))
            else:
                # Device disconnected — restart reconnector if not yet subscribed.
                is_subscribed = (
                    device in self.devices
                    and self.devices[device].communicator is not None
                )
                if not is_subscribed:
                    self._pause_advertising(device)
                    self._reconnectors[device].start()
        paired = changes.get("Paired")
        if paired is not None and paired.unpack():
            # Newly paired device — check if it's iOS and start reconnector.
            props = self.property_observers[device].GetAll(BluezDeviceAPI.interface)
            uuids = props.get("UUIDs", [])
            if ANCS_SERVICE_UUID in uuids:
                adapter_mac = self._get_adapter_mac(device)
                if adapter_mac and device not in self._reconnectors:
                    r = Reconnector(
                        device,
                        adapter_mac,
                        on_in_progress=partial(self._trigger_gatt_discovery, device),
                        on_failure=partial(self._resume_advertising, device),
                    )
                    self._reconnectors[device] = r
                    self._pause_advertising(device)
                    r.start()
            # Alias may have arrived before Paired — seed it from the full props
            # so the name is set even when it's absent from this change event.
            if "Alias" not in changes:
                alias = props.get("Alias")
                if alias is not None:
                    changes = dict(changes)
                    changes["Alias"] = alias
        # Only create MobileDevice for known paired-or-already-tracked devices.
        if device in self.devices or (paired is not None and paired.unpack()):
            self.devices.setdefault(
                device,
                MobileDevice(
                    device,
                    self.server,
                    on_subscribed=partial(self._on_subscribed, device),
                    on_unsubscribed=partial(self._on_unsubscribed, device),
                ),
            )
            if "Paired" in changes:
                self.devices[device].set_paired(changes["Paired"].unpack())
            if "ServicesResolved" in changes:
                self.devices[device].set_services_resolved(
                    changes["ServicesResolved"].unpack()
                )
            if "Alias" in changes:
                self.devices[device].set_name(changes["Alias"].unpack())

    def _ancs_characteristics_present(self, device: ObjPath) -> bool:
        """Return True if all three ANCS GATT characteristics are already in the D-Bus tree."""
        try:
            managed = self.root.GetManagedObjects()
            found = set()
            for path, services in managed.items():
                if BluezGattCharacteristicAPI.interface not in services:
                    continue
                if not path.startswith(device):
                    continue
                uuid = services[BluezGattCharacteristicAPI.interface].get("UUID")
                if uuid and uuid.unpack() in ANCS_CHARS:
                    found.add(uuid.unpack())
            return all(c in found for c in ANCS_CHARS)
        except Exception:
            return False

    def _trigger_gatt_discovery(self, device: ObjPath) -> bool:
        """Trigger GATT service discovery on a connected device.

        BlueZ only sets ServicesResolved=True automatically for outgoing connections.
        Calling Connect() on a device connected via BLE prompts BlueZ to discover GATT
        services on the existing BLE link. Advertising is paused first to prevent an
        incoming BLE solicitation response from racing with the Connect() call.

        If Connect() fails while the device is still connected (e.g. because iOS is
        simultaneously establishing an incoming BLE link in response to our
        SolicitUUIDs advertisement, causing le-connection-abort-by-local), the call
        is retried after LE_RECONNECT_INTERVAL_S seconds. By that point iOS should
        have completed the BLE link, so the retry hits a dual-connected device and
        triggers GATT discovery on the existing BLE link rather than opening a new one.

        We check for ANCS characteristics in the D-Bus tree rather than ServicesResolved,
        because BR/EDR SDP sets ServicesResolved=True before BLE GATT discovery occurs.
        """
        if device not in self.property_observers:
            return False
        proxy = self.property_observers[device]
        # Stop the reconnect loop before attempting Connect() to avoid both racing.
        if device in self._reconnectors:
            self._reconnectors[device].stop()
            self._reconnectors[device]._set_le_bearer()
        try:
            props = proxy.GetAll(BluezDeviceAPI.interface)
            if not props.get("Connected", Variant("b", False)).unpack():
                if device in self._reconnectors:
                    self._reconnectors[device].start()
                return False
            if self._ancs_characteristics_present(device):
                log.debug(f"ANCS characteristics already present for {device}, skipping discovery")
                self._resume_advertising(device)
                return False
            self._pause_advertising(device)
            log.info(f"Triggering GATT discovery for {device}")
            proxy.Connect()
        except Exception as e:
            log.debug(f"GATT discovery trigger failed for {device}: {e}")
            # Keep advertising paused during the retry wait so that iOS's
            # in-flight BLE connection attempt can complete without racing our
            # next Connect() call. If we re-enabled advertising here, iOS would
            # immediately start a new BLE attempt and conflict again.
            try:
                props = proxy.GetAll(BluezDeviceAPI.interface)
                if props.get("Connected", Variant("b", False)).unpack():
                    log.info(f"Scheduling GATT discovery retry for {device}")
                    GLib.timeout_add_seconds(LE_RECONNECT_INTERVAL_S, partial(self._trigger_gatt_discovery, device))
                    return False
            except Exception:
                pass
            # Device disconnected — restore advertising and restart reconnector.
            self._resume_advertising(device)
            if device in self._reconnectors:
                self._reconnectors[device].start()
        return False

    def _on_subscribed(self, device: ObjPath) -> None:
        if r := self._reconnectors.get(device):
            r.stop()
        self._resume_advertising(device)
        self.stop_discovery()

    def _on_unsubscribed(self, device: ObjPath) -> None:
        if r := self._reconnectors.get(device):
            r.start()
        self._start_discovery()

    def _start_discovery(self) -> None:
        if self._discovery_active:
            return
        for path, adapter in self._adapters.items():
            try:
                adapter.SetDiscoveryFilter({"Transport": Variant("s", "le")})
                adapter.StartDiscovery()
                self._discovery_active = True
                log.info(f"Started LE discovery on {path}")
                break
            except Exception as e:
                log.warning(f"Could not start discovery on {path}: {e}")

    def stop_discovery(self) -> None:
        if not self._discovery_active:
            return
        for path, adapter in self._adapters.items():
            try:
                adapter.StopDiscovery()
                self._discovery_active = False
                log.info(f"Stopped LE discovery on {path}")
                break
            except Exception as e:
                log.warning(f"Could not stop discovery on {path}: {e}")

    def remove_observers(self, path: ObjPath, services: List[Str]) -> None:
        if path in self.property_observers:
            self.property_observers[path].PropertiesChanged.disconnect()
            del self.property_observers[path]
