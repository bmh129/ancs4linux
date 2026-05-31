import logging
import re
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
from ancs4linux.observer.device import MobileDevice

log = logging.getLogger(__name__)

ANCS_SERVICE_UUID = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
LE_RECONNECT_INTERVAL_S = 30
BREDR_WATCHDOG_S = 15


class Reconnector:
    """Periodically writes LastUsedBearer=le and calls Device1.Connect() so BlueZ
    uses LE rather than BR/EDR when reconnecting to the iPhone after a reboot."""

    def __init__(self, device_path: ObjPath, adapter_mac: str) -> None:
        self.device_path = device_path
        device_mac = device_path.split("/")[-1][4:].replace("_", ":")
        self.info_path = f"/var/lib/bluetooth/{adapter_mac}/{device_mac}/info"
        self.device_proxy = BluezDeviceAPI.connect(device_path)
        self._timeout_id: Optional[int] = None
        self._active = False

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
        self._set_le_bearer()
        try:
            self.device_proxy.Connect()
            log.debug(f"LE connect initiated for {self.device_path}")
        except Exception as e:
            log.debug(f"LE connect attempt failed for {self.device_path}: {e}")
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
        self._bredr_watchdogs: Dict[str, int] = {}
        self._discovery_active: bool = False
        self._non_ancs_connected: int = 0

    def start_observing(self) -> None:
        self.root.InterfacesAdded.connect(self.process_object)
        self.root.InterfacesRemoved.connect(self.remove_observers)
        managed = self.root.GetManagedObjects()
        for path, services in managed.items():
            if "org.bluez.Adapter1" in services:
                self._adapters[path] = BluezAdapterAPI.connect(path)
                self._adapter_macs[path] = (
                    services["org.bluez.Adapter1"]["Address"].unpack()
                )
            self.process_object(path, services)
        self._start_ios_reconnectors(managed)
        self._start_discovery()
        self._init_non_ancs_connected(managed)

    def _get_adapter_mac(self, device_path: ObjPath) -> Optional[str]:
        adapter_path = "/".join(device_path.split("/")[:-1])
        return self._adapter_macs.get(adapter_path)

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
                r = Reconnector(path, adapter_mac)
                self._reconnectors[path] = r
                r.start()
            connected = props.get("Connected", Variant("b", False)).unpack()
            if connected:
                self._start_bredr_watchdog(path)

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
        connected = changes.get("Connected")
        if connected is not None:
            if device in self._reconnectors:
                if connected.unpack():
                    self._start_bredr_watchdog(device)
                else:
                    self._cancel_bredr_watchdog(device)
            else:
                self._update_non_ancs_connection(device, connected.unpack())
        paired = changes.get("Paired")
        if paired is not None and paired.unpack():
            # Newly paired device — check if it's iOS and start reconnector.
            props = self.property_observers[device].GetAll(BluezDeviceAPI.interface)
            uuids = props.get("UUIDs", [])
            if ANCS_SERVICE_UUID in uuids:
                adapter_mac = self._get_adapter_mac(device)
                if adapter_mac and device not in self._reconnectors:
                    r = Reconnector(device, adapter_mac)
                    self._reconnectors[device] = r
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

    def _on_subscribed(self, device: ObjPath) -> None:
        self._cancel_bredr_watchdog(device)
        if r := self._reconnectors.get(device):
            r.stop()
        self.stop_discovery()

    def _start_bredr_watchdog(self, device: ObjPath) -> None:
        self._cancel_bredr_watchdog(device)
        tid = GLib.timeout_add_seconds(
            BREDR_WATCHDOG_S, partial(self._on_bredr_watchdog, device)
        )
        self._bredr_watchdogs[device] = tid
        log.debug(f"BR/EDR watchdog started for {device}")

    def _cancel_bredr_watchdog(self, device: ObjPath) -> None:
        if tid := self._bredr_watchdogs.pop(device, None):
            GLib.source_remove(tid)

    def _on_bredr_watchdog(self, device: ObjPath) -> bool:
        self._bredr_watchdogs.pop(device, None)
        mobile = self.devices.get(device)
        if mobile and mobile.communicator is not None:
            return False
        log.warning(
            f"Device connected but ANCS not resolved after {BREDR_WATCHDOG_S}s — "
            f"disconnecting to force LE reconnect: {device}"
        )
        proxy = self.property_observers.get(device)
        if proxy:
            try:
                proxy.Disconnect()
            except Exception as e:
                log.warning(f"Disconnect failed for {device}: {e}")
        return False

    def _on_unsubscribed(self, device: ObjPath) -> None:
        if r := self._reconnectors.get(device):
            r.start()
        self._start_discovery()

    def _init_non_ancs_connected(
        self, managed: Dict[ObjPath, Dict[Str, Dict[Str, Variant]]]
    ) -> None:
        for path, services in managed.items():
            if "org.bluez.Device1" not in services or path in self._reconnectors:
                continue
            props = services["org.bluez.Device1"]
            if props.get("Connected", Variant("b", False)).unpack():
                self._non_ancs_connected += 1
        if self._non_ancs_connected > 0:
            log.info(
                f"{self._non_ancs_connected} non-ANCS device(s) already connected at startup, "
                "pausing LE activity"
            )
            for r in self._reconnectors.values():
                r.stop()

    def _update_non_ancs_connection(self, device: ObjPath, connected: bool) -> None:
        prev = self._non_ancs_connected
        if connected:
            self._non_ancs_connected += 1
        else:
            self._non_ancs_connected = max(0, self._non_ancs_connected - 1)

        if prev == 0 and self._non_ancs_connected > 0:
            log.info(f"Non-ANCS device connected ({device}), pausing LE activity")
            self.stop_discovery()
            for r in self._reconnectors.values():
                r.stop()
        elif prev > 0 and self._non_ancs_connected == 0:
            log.info("All non-ANCS devices disconnected, resuming LE activity")
            for dev_path, r in self._reconnectors.items():
                mobile = self.devices.get(dev_path)
                if mobile is None or mobile.communicator is None:
                    r.start()
            if not any(m.communicator is not None for m in self.devices.values()):
                self._start_discovery()

    def _start_discovery(self) -> None:
        if self._discovery_active:
            return
        if self._non_ancs_connected > 0:
            log.info("Skipping LE discovery: non-ANCS device connected")
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
        self._cancel_bredr_watchdog(path)
        if path in self.property_observers:
            self.property_observers[path].PropertiesChanged.disconnect()
            del self.property_observers[path]
