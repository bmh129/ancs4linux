import logging
import random
from collections import deque
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set

import gi  # type: ignore

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # type: ignore

from ancs4linux.common.apis import ShowNotificationData
from ancs4linux.common.dbus import Variant

log = logging.getLogger(__name__)
from ancs4linux.observer.ancs.builders import (
    GetAppAttributes,
    GetNotificationAttributes,
    PerformNotificationAction,
)
from ancs4linux.observer.ancs.constants import UINT_MAX, CommandID, EventID
from ancs4linux.observer.ancs.parsers import (
    AppAttributes,
    DataSourceEvent,
    Notification,
    NotificationAttributes,
)

if TYPE_CHECKING:
    from ancs4linux.observer.device import MobileDevice


class DeviceCommunicator:
    def __init__(self, device: "MobileDevice"):
        self.device = device
        # Per-device ID namespace: random base * 1000 leaves room for device-local UIDs (small ints) without colliding across devices.
        self.id = random.randint(1, 10 ** 5) * 1000
        self.notification_queue: List[ShowNotificationData] = []
        self.awaiting_app_names: Set[str] = set()
        self.known_app_names: Dict[str, str] = dict()
        # ANCS allows only one outstanding control-point request at a time.
        # Queue entries: (msg_bytes, expects_data_source_response).
        self._control_point_queue: deque = deque()
        self._request_in_flight: bool = False

    def attach(self) -> None:
        assert self.device.notification_source and self.device.data_source
        self.device.notification_source.PropertiesChanged.disconnect()
        self.device.notification_source.PropertiesChanged.connect(self.on_ns_change)
        self.device.data_source.PropertiesChanged.disconnect()
        self.device.data_source.PropertiesChanged.connect(self.on_ds_change)

    def _queue_to_control_point(
        self, msg: List[int], expects_response: bool, on_sent: Optional[Callable[[], None]] = None
    ) -> None:
        self._control_point_queue.append((msg, expects_response, on_sent))
        self._pump_control_point()

    def _pump_control_point(self) -> None:
        while not self._request_in_flight and self._control_point_queue:
            msg, expects_response, on_sent = self._control_point_queue.popleft()
            assert self.device.control_point
            try:
                self.device.control_point.WriteValue(msg, {})
            except Exception as e:
                log.error(f"Control point write failed: {e}")
                break
            if on_sent is not None:
                on_sent()
            if expects_response:
                self._request_in_flight = True
                break
            # PerformNotificationAction: no data source response; loop to send next

    def on_ns_change(
        self, interface: str, changes: Dict[str, Variant], invalidated: List[str]
    ) -> None:
        if interface != "org.bluez.GattCharacteristic1" or "Value" not in changes:
            return

        try:
            notification = Notification.parse(changes["Value"].unpack())
            log.debug(f"ANCS notification: type={notification.type} fresh={notification.is_fresh()} id={notification.id}")
            if notification.type in (EventID.NotificationAdded, EventID.NotificationModified):
                self.ask_for_notification_details(notification)
            else:
                self.device.server.emit_dismiss_notification(notification.id)
        except Exception as e:
            log.error(f"Failed to handle notification source packet: {e}")

    def ask_for_notification_details(self, notification: Notification) -> None:
        msg = GetNotificationAttributes(
            id=notification.id,
            get_positive_action=notification.has_positive_action(),
            get_negative_action=notification.has_negative_action(),
        )
        self._queue_to_control_point(msg.to_list(), expects_response=True)

    def on_ds_change(
        self, interface: str, changes: Dict[str, Variant], invalidated: List[str]
    ) -> None:
        if interface != "org.bluez.GattCharacteristic1" or "Value" not in changes:
            return

        try:
            ev = DataSourceEvent.parse(changes["Value"].unpack())
            # Clear in-flight before the handler runs so that any follow-up requests
            # queued inside the handler (e.g. GetAppAttributes from process_queue)
            # are sent immediately by _pump_control_point.
            self._request_in_flight = False
            if ev.type == CommandID.GetNotificationAttributes:
                self.on_notification_attributes(ev.as_notification_attributes())
            elif ev.type == CommandID.GetAppAttributes:
                self.on_app_attributes(ev.as_app_attributes())
        except Exception as e:
            log.error(f"Failed to handle data source packet: {e}")
            self._request_in_flight = False
        self._pump_control_point()

    def on_notification_attributes(self, attrs: NotificationAttributes) -> None:
        assert self.device.name
        self.queue_notification(
            ShowNotificationData(
                device_handle=self.device.path,
                device_name=self.device.name,
                app_id=attrs.app_id,
                app_name="",
                id=(self.id + attrs.id) % UINT_MAX,  # device-local UID → process-global UInt32
                title=attrs.title,
                body=attrs.message,
                positive_action=attrs.positive_action,
                negative_action=attrs.negative_action,
            )
        )
        self.process_queue()

    def on_app_attributes(self, attrs: AppAttributes) -> None:
        self.known_app_names[attrs.app_id] = attrs.app_name
        if attrs.app_id in self.awaiting_app_names:
            self.awaiting_app_names.remove(attrs.app_id)
        self.process_queue()

    def queue_notification(self, data: ShowNotificationData) -> None:
        self.notification_queue.append(data)

    def ask_for_app_name(self, app_id: str) -> None:
        self.awaiting_app_names.add(app_id)
        msg = GetAppAttributes(app_id=app_id)

        def on_sent() -> None:
            GLib.timeout_add_seconds(5, lambda: self._app_name_timeout(app_id))

        self._queue_to_control_point(msg.to_list(), expects_response=True, on_sent=on_sent)

    def _app_name_timeout(self, app_id: str) -> bool:
        if app_id in self.awaiting_app_names:
            log.warning(f"App name lookup timed out for {app_id!r}, using app_id as fallback")
            self.awaiting_app_names.discard(app_id)
            self.known_app_names[app_id] = app_id
            # Unblock the queue in case the iPhone never responded to our GetAppAttributes.
            # If the response arrives late, on_ds_change will harmlessly clear in_flight again.
            self._request_in_flight = False
            self._pump_control_point()
            self.process_queue()
        return False

    def process_queue(self) -> None:
        unprocessed = []
        for data in self.notification_queue:
            if data.app_name != "":
                self._emit(data)
            elif data.app_id in self.known_app_names:
                data.app_name = self.known_app_names[data.app_id]
                self._emit(data)
            elif data.app_id in self.awaiting_app_names:
                unprocessed.append(data)
            else:
                self.ask_for_app_name(data.app_id)
                unprocessed.append(data)
        self.notification_queue = unprocessed

    def _emit(self, data: ShowNotificationData) -> None:
        try:
            self.device.server.emit_show_notification(data)
        except Exception as e:
            log.error(f"Failed to emit notification {data.id!r}: {e}")

    def ask_for_action(self, notification_id: int, is_positive: bool) -> None:
        id = (notification_id - self.id) % UINT_MAX  # reverse the global→local mapping before writing to control point
        msg = PerformNotificationAction(notification_id=id, is_positive=is_positive)
        # PerformNotificationAction has no data source response (ANCS spec §3.3).
        self._queue_to_control_point(msg.to_list(), expects_response=False)
