from typing import Optional

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from ancs4linux.common.apis import AdvertisingAPI, PairingAgentAPI
from ancs4linux.common.dbus import (
    ObjPath,
    PairingRejected,
    Str,
    SystemBus,
    UInt16,
    UInt32,
    dbus_interface,
)
from ancs4linux.common.external_apis import BluezAgentManagerAPI


@dbus_interface(PairingAgentAPI.interface)
class PairingAgent:
    def __init__(self, server: AdvertisingAPI):
        self.server = server
        self._pending = False
        self._confirmation_result: Optional[bool] = None

    def Release(self) -> None:
        pass

    def RequestPinCode(self, device: ObjPath) -> Str:
        raise PairingRejected

    def DisplayPinCode(self, device: ObjPath, pincode: Str) -> None:
        raise PairingRejected

    def RequestPassKey(self, device: ObjPath) -> UInt32:
        raise PairingRejected

    def DisplayPasskey(self, device: ObjPath, passkey: UInt32, entered: UInt16) -> None:
        raise PairingRejected

    def RequestConfirmation(self, device: ObjPath, passkey: UInt32) -> None:
        if self._pending:
            raise PairingRejected

        self._pending = True
        self._confirmation_result = None

        try:
            passkey_str = "{:06d}".format(int(passkey))
            self.server.emit_pairing_confirmation_requested(passkey_str)

            GLib.timeout_add_seconds(30, self._on_timeout)
            context = GLib.MainContext.default()

            # Pump the GLib event loop manually so ConfirmPairing/Cancel D-Bus calls can arrive
            # and set _confirmation_result without deadlocking the main loop.
            while self._confirmation_result is None:
                context.iteration(may_block=True)

            if not self._confirmation_result:
                raise PairingRejected
        finally:
            self._pending = False

    def _on_timeout(self) -> bool:
        if self._confirmation_result is None:
            self._confirmation_result = False
        return False

    def set_confirmation(self, confirmed: bool) -> None:
        if self._pending and self._confirmation_result is None:
            self._confirmation_result = confirmed

    def RequestAuthorization(self, device: ObjPath) -> None:
        raise PairingRejected

    def AuthorizeService(self, device: ObjPath, uuid: Str) -> None:
        pass

    def Cancel(self) -> None:
        if self._pending and self._confirmation_result is None:
            self._confirmation_result = False


class PairingManager:
    def __init__(self):
        self.enabled = False
        self.enabled_automatically = False
        self.agent_manager = BluezAgentManagerAPI.connect()
        self.agent: Optional[PairingAgent] = None

    def register(self, server: AdvertisingAPI) -> None:
        self.agent = PairingAgent(server)
        SystemBus().publish_object(PairingAgentAPI.path, self.agent)

    def set_confirmation(self, confirmed: bool) -> None:
        if self.agent is not None:
            self.agent.set_confirmation(confirmed)

    def enable(self) -> None:
        if self.enabled:
            return

        self.agent_manager.RegisterAgent(PairingAgentAPI.path, "DisplayYesNo")
        self.agent_manager.RequestDefaultAgent(PairingAgentAPI.path)
        self.enabled = True
        self.enabled_automatically = False

    def disable(self) -> None:
        if not self.enabled:
            return

        self.agent_manager.UnregisterAgent(PairingAgentAPI.path)
        self.enabled = False
        self.enabled_automatically = False

    def enable_automatically(self) -> None:
        if self.enabled:
            return

        self.enable()
        self.enabled_automatically = True

    def disable_if_enabled_automatically(self) -> None:
        if not self.enabled:
            return
        if not self.enabled_automatically:
            return

        self.disable()
