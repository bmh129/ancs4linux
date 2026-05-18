import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

from ancs4linux.advertising.pairing import PairingManager
from ancs4linux.common.dbus import (
    Bool,
    Byte,
    DBusError,
    ObjPath,
    Str,
    SystemBus,
    UInt16,
    Variant,
    dbus_interface,
)
from ancs4linux.common.external_apis import BluezRootAPI


def array_of_bytes(array: List[int]) -> Variant:
    return Variant("ay", [Byte(i) for i in array])


@dbus_interface("org.bluez.LEAdvertisement1")
class AdvertisementData:
    """Simple advertisement. IDs were taken randomly."""

    @property
    def Type(self) -> Str:
        return "peripheral"

    @Type.setter
    def Type(self, value: Str) -> None:
        pass

    @property
    def ServiceUUIDs(self) -> List[Str]:
        return []

    @ServiceUUIDs.setter
    def ServiceUUIDs(self, value: List[Str]) -> None:
        pass

    @property
    def SolicitUUIDs(self) -> List[Str]:
        # Soliciting ANCS tells iOS to proactively connect via BLE for notification delivery.
        return ["7905f431-b5ce-4e99-a40f-4b1e122d00d0"]

    @SolicitUUIDs.setter
    def SolicitUUIDs(self, value: List[Str]) -> None:
        pass

    @property
    def IncludeTxPower(self) -> Bool:
        return True

    @IncludeTxPower.setter
    def IncludeTxPower(self, value: Bool) -> None:
        pass

    @property
    def ManufacturerData(self) -> Dict[UInt16, Variant]:
        # 0xFFFF is the unregistered/test manufacturer ID; payload bytes are arbitrary — iOS ignores them and only needs a visible advertisement to initiate ANCS.
        return {UInt16(0xFFFF): array_of_bytes([0x50, 0xB0, 0x13, 0xF0])}

    @ManufacturerData.setter
    def ManufacturerData(self, value: Dict[UInt16, Variant]) -> None:
        pass

    @property
    def ServiceData(self) -> Dict[Str, Variant]:
        return {"9999": array_of_bytes([0x9E, 0x85, 0x39, 0x96])}

    @ServiceData.setter
    def ServiceData(self, value: Dict[Str, Variant]) -> None:
        pass

    def Release(self):
        pass


@dataclass
class HciState:
    name: str
    powered: bool
    discoverable: bool
    pairable: bool

    @classmethod
    def advertising(cls, name: str) -> "HciState":
        return cls(name=name, powered=True, discoverable=True, pairable=True)

    @classmethod
    def save(cls, hci: Any) -> "HciState":
        return cls(
            name=hci.Alias,
            powered=hci.Powered,
            discoverable=hci.Discoverable,
            pairable=hci.Pairable,
        )

    def restore_on(self, hci: Any) -> None:
        hci.Powered = self.powered
        hci.Alias = self.name
        if self.powered:
            hci.Pairable = self.pairable
            hci.Discoverable = self.discoverable


class AdvertisingManager:
    ADDRESS = ObjPath("/advertisement")

    def __init__(self, pairing_manager: PairingManager):
        self.active_advertisements: Dict[str, HciState] = {}
        self.pairing_manager = pairing_manager

    def register(self) -> None:
        SystemBus().publish_object(self.ADDRESS, AdvertisementData())

    def get_all_hci(self) -> Dict[ObjPath, Dict[str, Dict[str, Variant]]]:
        proxy = BluezRootAPI.connect()
        return {
            path: services
            for path, services in proxy.GetManagedObjects().items()
            if "org.bluez.Adapter1" in services
            if "org.bluez.LEAdvertisingManager1" in services
        }

    def get_all_hci_addresses(self) -> List[str]:
        return [
            hci["org.bluez.Adapter1"]["Address"].unpack()
            for path, hci in self.get_all_hci().items()
        ]

    def get_hci_path(self, hci_address: str) -> Optional[ObjPath]:
        for path, hci in self.get_all_hci().items():
            if hci["org.bluez.Adapter1"]["Address"].unpack() == hci_address:
                return path
        return None

    def enable_advertising(self, hci_address: str, name: str) -> None:
        if hci_address in self.active_advertisements:
            # We were already advertising, but maybe something went wrong.
            self.disable_advertising(hci_address)

        path = self.get_hci_path(hci_address)
        if path is None:
            raise Exception(f"Unknown hci address {hci_address}")

        if not self.pairing_manager.enabled and len(self.active_advertisements) == 0:
            self.pairing_manager.enable_automatically()

        hci: Any = SystemBus().get_proxy("org.bluez", path)
        self.active_advertisements[hci_address] = HciState.save(hci)
        try:
            HciState.advertising(name).restore_on(hci)
        except Exception as e:
            log.warning(f"Could not set adapter advertising state on {hci_address}: {e}")
        hci.RegisterAdvertisement("/advertisement", {})

    def disable_advertising(self, hci_address: str) -> None:
        if hci_address not in self.active_advertisements:
            raise Exception(f"No advertisement found for {hci_address}")

        original_state = self.active_advertisements[hci_address]
        del self.active_advertisements[hci_address]
        path = self.get_hci_path(hci_address)
        if path is not None:
            hci: Any = SystemBus().get_proxy("org.bluez", path)
            try:
                hci.UnregisterAdvertisement("/advertisement")
            except DBusError:
                pass  # BlueZ may have already expired the advertisement
            try:
                original_state.restore_on(hci)
            except Exception as e:
                log.warning(f"Could not restore adapter state on {hci_address}: {e}")

        if len(self.active_advertisements) == 0:
            self.pairing_manager.disable_if_enabled_automatically()
