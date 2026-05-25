import html
import logging
from typing import Dict, Optional

import typer

from ancs4linux.common.apis import AdvertisingAPI, ObserverAPI, ShowNotificationData
from ancs4linux.common.dbus import EventLoop, Int32, UInt32
from ancs4linux.common.external_apis import NotificationAPI
from ancs4linux.common.url_config import open_target

log = logging.getLogger(__name__)
app = typer.Typer()
notification_api: NotificationAPI
advertising_api: AdvertisingAPI
observer_api: ObserverAPI
notification_timeout: int
notifications: Dict[int, "Notification"] = {}
# Survives notification_closed so open-url still works after the popup expires.
host_id_to_app: Dict[int, tuple] = {}
pairing_notification_id: int = 0


class Notification:
    def __init__(self, id: int):
        self.device_id = id
        self.device_handle: Optional[str] = None
        self.host_id = 0
        self.app_id = ""
        self.app_name = ""

    def show(self, data: ShowNotificationData) -> None:
        self.device_handle = data.device_handle
        self.app_id = data.app_id
        self.app_name = data.app_name
        actions = []
        if data.positive_action is not None:
            actions.extend(["positive-action", data.positive_action])
        if data.negative_action is not None:
            actions.extend(["negative-action", data.negative_action])
        actions.extend(["open-url", "Open"])
        self.host_id = notification_api.Notify(
            f"{data.app_name} ({data.device_name})",
            UInt32(self.host_id),
            "",
            html.escape(data.title, quote=False),
            html.escape(data.body, quote=False),
            actions,
            [],
            Int32(notification_timeout),
        )
        host_id_to_app[self.host_id] = (self.app_id, self.app_name)
        log.debug(f"Shown {self.host_id} from {data.app_name} (app_id={data.app_id}).")

    def dismiss(self) -> None:
        if self.host_id != 0:
            notification_api.CloseNotification(UInt32(self.host_id))
            log.debug(f"Hidden {self.host_id}.")
            self.host_id = 0

    def on_action(self, action: str) -> None:
        if self.device_handle and action == "positive-action":
            observer_api.InvokeDeviceAction(
                self.device_handle, UInt32(self.device_id), True
            )
        if self.device_handle and action == "negative-action":
            observer_api.InvokeDeviceAction(
                self.device_handle, UInt32(self.device_id), False
            )
        if action == "open-url":
            open_target(self.app_id, self.app_name)


def pairing_confirmation_requested(pin: str) -> None:
    global pairing_notification_id
    pairing_notification_id = int(
        notification_api.Notify(
            "ancs4linux",
            UInt32(0),
            "",
            "Bluetooth Pairing Request",
            f"Confirm pairing only if PIN matches: {pin}",
            ["confirm-pairing", "Confirm", "deny-pairing", "Deny"],
            [],
            Int32(30000),
        )
    )


def new_notification(json: str) -> None:
    data = ShowNotificationData.parse(json)
    notifications.setdefault(data.id, Notification(data.id))
    notifications[data.id].show(data)


def dismiss_notification(id: int) -> None:
    if id in notifications:
        notifications[id].dismiss()


def action_clicked(host_id: int, action: str) -> None:
    global pairing_notification_id
    if host_id == pairing_notification_id:
        advertising_api.ConfirmPairing(action == "confirm-pairing")
        pairing_notification_id = 0
        return
    for notification in notifications.values():
        if notification.host_id == host_id:
            notification.on_action(action)
            return
    # Notification already removed from dict (timed out or service restarted) —
    # fall back to the app lookup so open-url still works.
    if action == "open-url" and host_id in host_id_to_app:
        app_id, app_name = host_id_to_app[host_id]
        open_target(app_id, app_name)


def notification_closed(id: int, reason: int) -> None:
    if id in notifications:
        del notifications[id]


@app.command()
def main(
    observer_dbus: str = typer.Option(
        "ancs4linux.Observer", help="Observer service path"
    ),
    advertising_dbus: str = typer.Option(
        "ancs4linux.Advertising", help="Advertising service path"
    ),
    notification_ms: int = typer.Option(
        5000, help="How long to show notifications for"
    ),
) -> None:
    logging.basicConfig(level=logging.DEBUG)
    loop = EventLoop()

    global notification_timeout
    notification_timeout = notification_ms

    global observer_api, advertising_api, notification_api
    notification_api = NotificationAPI.connect()
    advertising_api = AdvertisingAPI.connect(advertising_dbus)
    observer_api = ObserverAPI.connect(observer_dbus)

    notification_api.ActionInvoked.connect(action_clicked)
    notification_api.NotificationClosed.connect(notification_closed)
    advertising_api.PairingConfirmationRequested.connect(pairing_confirmation_requested)
    observer_api.ShowNotification.connect(new_notification)
    observer_api.DismissNotification.connect(dismiss_notification)

    log.info("Listening to notifications...")
    loop.run()
