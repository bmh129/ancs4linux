import logging
from functools import partial
from typing import Dict, List, Optional

import gi  # type: ignore

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # type: ignore

from ancs4linux.common.apis import ObserverAPI
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
from ancs4linux.observer.att_client import ATTConnection, FakeGattChar
from ancs4linux.observer.device import MobileDevice

log = logging.getLogger(__name__)

ANCS_SERVICE_UUID = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
RECONNECT_INTERVAL_S = 30


def _mac_from_path(path: str) -> str:
    """Extract MAC address from BlueZ device path, e.g. /org/bluez/hci0/dev_1C_3C_78_DE_D3_65."""
    return path.split("/")[-1][4:].replace("_", ":")


class Reconnector:
    """Ensures GATT is established after a Bluetooth restart.

    BlueZ 5.x auto-reconnects the iPhone via BR/EDR on reboot but does NOT
    re-establish the ATT bearer.  We work around this by opening a direct
    L2CAP connection to the iPhone (PSM 31) and doing our own GATT discovery
    via ATTConnection, then injecting FakeGattChar objects into MobileDevice.
    """

    def __init__(self, device_path: ObjPath, on_att_ready: callable) -> None:
        self.device_path = device_path
        self.device_proxy = BluezDeviceAPI.connect(device_path)
        self._mac = _mac_from_path(device_path)
        self._timeout_id: Optional[int] = None
        self._active = False
        self._att: Optional[ATTConnection] = None
        self._att_ready = False
        self._on_att_ready = on_att_ready

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        log.info(f"Starting GATT reconnect loop for {self.device_path}")
        self._attempt()
        self._timeout_id = GLib.timeout_add_seconds(RECONNECT_INTERVAL_S, self._attempt)

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None
        if self._att is not None:
            self._att.close()
            self._att = None
        self._att_ready = False
        log.info(f"Stopped GATT reconnect loop for {self.device_path}")

    def on_disconnected(self) -> None:
        """Call when BlueZ signals the device disconnected."""
        if self._att is not None:
            self._att.close()
            self._att = None
        self._att_ready = False

    def _has_bluez_gatt(self) -> bool:
        try:
            managed = BluezRootAPI.connect().GetManagedObjects()
            prefix = self.device_path + "/"
            return any(
                path.startswith(prefix) and BluezGattCharacteristicAPI.interface in svcs
                for path, svcs in managed.items()
            )
        except Exception:
            return False

    def _attempt(self) -> bool:
        if not self._active:
            return False
        if self._att_ready or self._att is not None:
            log.debug(
                f"ATT {'ready' if self._att_ready else 'connecting'} for {self.device_path}"
            )
            return self._active
        if self._has_bluez_gatt():
            log.debug(f"BlueZ GATT present for {self.device_path}")
            return self._active
        try:
            props = self.device_proxy.GetAll(BluezDeviceAPI.interface)
            connected = props.get("Connected", Variant("b", False)).unpack()
        except Exception as e:
            log.debug(f"Could not check {self.device_path}: {e}")
            return self._active
        if connected:
            log.info(f"Opening direct ATT for {self.device_path} ({self._mac})")
            self._att = ATTConnection(
                self._mac,
                on_ready=self._on_att_connected,
                on_failed=self._on_att_failed,
            )
            self._att.open()
        return self._active

    def _on_att_connected(
        self, ns: FakeGattChar, cp: FakeGattChar, ds: FakeGattChar
    ) -> None:
        self._att_ready = True
        self._on_att_ready(ns, cp, ds)

    def _on_att_failed(self) -> None:
        log.warning(f"ATT failed for {self.device_path}, will retry in {RECONNECT_INTERVAL_S}s")
        self._att = None
        self._att_ready = False


class Scanner:
    def __init__(self, server: ObserverAPI):
        self.server = server
        self.root = BluezRootAPI.connect()
        self.devices: Dict[str, MobileDevice] = {}
        self.property_observers: Dict[str, BluezDeviceAPI] = {}
        self._adapters: Dict[str, BluezAdapterAPI] = {}
        self._reconnectors: Dict[str, Reconnector] = {}
        self._discovery_active: bool = False

    def start_observing(self) -> None:
        self.root.InterfacesAdded.connect(self.process_object)
        self.root.InterfacesRemoved.connect(self.remove_observers)
        managed = self.root.GetManagedObjects()
        for path, services in managed.items():
            if "org.bluez.Adapter1" in services:
                self._adapters[path] = BluezAdapterAPI.connect(path)
            self.process_object(path, services)
        self._start_discovery()
        self._start_ios_reconnectors(managed)

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
            if path not in self._reconnectors:
                r = Reconnector(path, on_att_ready=partial(self._on_att_ready, path))
                self._reconnectors[path] = r
                r.start()

    def _on_att_ready(
        self,
        device_path: ObjPath,
        ns: FakeGattChar,
        cp: FakeGattChar,
        ds: FakeGattChar,
    ) -> None:
        device = self.devices.setdefault(
            device_path,
            MobileDevice(
                device_path,
                self.server,
                on_subscribed=partial(self._on_subscribed, device_path),
                on_unsubscribed=partial(self._on_unsubscribed, device_path),
            ),
        )
        device.set_chars_direct(ns, cp, ds)

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
        paired = changes.get("Paired")
        if paired is not None and paired.unpack():
            # Newly paired device — check if it's iOS and start reconnector.
            props = self.property_observers[device].GetAll(BluezDeviceAPI.interface)
            uuids = props.get("UUIDs", [])
            if ANCS_SERVICE_UUID in uuids:
                if device not in self._reconnectors:
                    r = Reconnector(device, on_att_ready=partial(self._on_att_ready, device))
                    self._reconnectors[device] = r
                    r.start()
            # Alias may have arrived before Paired — seed it from the full props
            # so the name is set even when it's absent from this change event.
            if "Alias" not in changes:
                alias = props.get("Alias")
                if alias is not None:
                    changes = dict(changes)
                    changes["Alias"] = alias

        if "Connected" in changes:
            connected_val = changes["Connected"].unpack()
            if connected_val:
                # Device reconnected — trigger an immediate ATT attempt.
                if r := self._reconnectors.get(device):
                    if r._active:
                        r._attempt()
            else:
                # Device disconnected — tear down ATT and unsubscribe.
                if r := self._reconnectors.get(device):
                    r.on_disconnected()
                if device in self.devices:
                    self.devices[device].unsubscribe()

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

    def _on_subscribed(self, device: ObjPath) -> None:
        if r := self._reconnectors.get(device):
            r.stop()
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
