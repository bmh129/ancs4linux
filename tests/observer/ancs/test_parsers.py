import struct

import pytest

from ancs4linux.observer.ancs.constants import (
    CommandID,
    EventFlag,
    EventID,
    NotificationAttributeID,
)
from ancs4linux.observer.ancs.parsers import (
    AppAttributes,
    DataSourceEvent,
    Notification,
    NotificationAttributes,
)


def make_string_field(attr_id: int, text: str) -> bytes:
    """Encode a string in ANCS wire format: AttributeID(B) + length(H) + utf8."""
    encoded = text.encode("utf8")
    return struct.pack("<BH", attr_id, len(encoded)) + encoded


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


def test_notification_parse_basic():
    data = struct.pack("<BBBBI", EventID.NotificationAdded, 0, 0, 0, 99)
    n = Notification.parse(data)
    assert n.id == 99
    assert n.type == EventID.NotificationAdded
    assert n.flags == 0


def test_notification_parse_modified():
    data = struct.pack("<BBBBI", EventID.NotificationModified, 0, 0, 0, 7)
    n = Notification.parse(data)
    assert n.type == EventID.NotificationModified


def test_notification_parse_removed():
    data = struct.pack("<BBBBI", EventID.NotificationRemoved, 0, 0, 0, 3)
    n = Notification.parse(data)
    assert n.type == EventID.NotificationRemoved


def test_notification_is_fresh():
    data = struct.pack("<BBBBI", EventID.NotificationAdded, 0, 0, 0, 1)
    n = Notification.parse(data)
    assert n.is_fresh() is True
    assert n.is_preexisting() is False


def test_notification_is_preexisting():
    data = struct.pack("<BBBBI", EventID.NotificationAdded, EventFlag.PreExisting, 0, 0, 1)
    n = Notification.parse(data)
    assert n.is_preexisting() is True
    assert n.is_fresh() is False


def test_notification_has_positive_action():
    data = struct.pack("<BBBBI", EventID.NotificationAdded, EventFlag.PositiveAction, 0, 0, 1)
    n = Notification.parse(data)
    assert n.has_positive_action() is True
    assert n.has_negative_action() is False


def test_notification_has_negative_action():
    data = struct.pack("<BBBBI", EventID.NotificationAdded, EventFlag.NegativeAction, 0, 0, 1)
    n = Notification.parse(data)
    assert n.has_negative_action() is True
    assert n.has_positive_action() is False


def test_notification_has_both_actions():
    flags = EventFlag.PositiveAction | EventFlag.NegativeAction
    data = struct.pack("<BBBBI", EventID.NotificationAdded, flags, 0, 0, 1)
    n = Notification.parse(data)
    assert n.has_positive_action() is True
    assert n.has_negative_action() is True


def test_notification_uid_max():
    uid = (2**32) - 1
    data = struct.pack("<BBBBI", EventID.NotificationAdded, 0, 0, 0, uid)
    n = Notification.parse(data)
    assert n.id == uid


# ---------------------------------------------------------------------------
# NotificationAttributes
# ---------------------------------------------------------------------------


def _make_notification_attrs_payload(
    uid: int,
    app_id: str,
    title: str,
    message: str,
    positive_action: str = None,
    negative_action: str = None,
) -> bytes:
    data = struct.pack("<I", uid)
    data += make_string_field(NotificationAttributeID.AppIdentifier, app_id)
    data += make_string_field(NotificationAttributeID.Title, title)
    data += make_string_field(NotificationAttributeID.Message, message)
    if positive_action is not None:
        data += make_string_field(NotificationAttributeID.PositiveActionLabel, positive_action)
    if negative_action is not None:
        data += make_string_field(NotificationAttributeID.NegativeActionLabel, negative_action)
    return data


def test_notification_attributes_basic():
    payload = _make_notification_attrs_payload(42, "com.example.app", "Hello", "World")
    attrs = NotificationAttributes.parse(payload)
    assert attrs.id == 42
    assert attrs.app_id == "com.example.app"
    assert attrs.title == "Hello"
    assert attrs.message == "World"
    assert attrs.positive_action is None
    assert attrs.negative_action is None


def test_notification_attributes_with_both_actions():
    payload = _make_notification_attrs_payload(
        1, "com.example.app", "Incoming Call", "", "Answer", "Decline"
    )
    attrs = NotificationAttributes.parse(payload)
    assert attrs.positive_action == "Answer"
    assert attrs.negative_action == "Decline"


def test_notification_attributes_with_positive_action_only():
    payload = _make_notification_attrs_payload(
        5, "com.example.app", "Reminder", "Do the thing", "Dismiss"
    )
    attrs = NotificationAttributes.parse(payload)
    assert attrs.positive_action == "Dismiss"
    assert attrs.negative_action is None


def test_notification_attributes_empty_strings():
    payload = _make_notification_attrs_payload(0, "", "", "")
    attrs = NotificationAttributes.parse(payload)
    assert attrs.app_id == ""
    assert attrs.title == ""
    assert attrs.message == ""


def test_notification_attributes_unicode():
    payload = _make_notification_attrs_payload(10, "jp.app", "日本語", "メッセージ")
    attrs = NotificationAttributes.parse(payload)
    assert attrs.title == "日本語"
    assert attrs.message == "メッセージ"


# ---------------------------------------------------------------------------
# AppAttributes
# ---------------------------------------------------------------------------


def _make_app_attrs_payload(app_id: str, app_name: str = None) -> bytes:
    data = app_id.encode("utf8") + b"\x00"
    if app_name is not None:
        encoded = app_name.encode("utf8")
        # AttributeID(B=0) + length(H) + name bytes
        data += struct.pack("<BH", 0, len(encoded)) + encoded
    return data


def test_app_attributes_basic():
    payload = _make_app_attrs_payload("com.example.app", "Example App")
    attrs = AppAttributes.parse(payload)
    assert attrs.app_id == "com.example.app"
    assert attrs.app_name == "Example App"


def test_app_attributes_not_installed():
    # When the device returns no name body, the app is not installed
    payload = _make_app_attrs_payload("com.gone.app")
    attrs = AppAttributes.parse(payload)
    assert attrs.app_id == "com.gone.app"
    assert attrs.app_name == "<not installed>"


def test_app_attributes_unicode_name():
    payload = _make_app_attrs_payload("jp.example", "日本語アプリ")
    attrs = AppAttributes.parse(payload)
    assert attrs.app_name == "日本語アプリ"


def test_app_attributes_empty_app_id():
    payload = _make_app_attrs_payload("", "Some App")
    attrs = AppAttributes.parse(payload)
    assert attrs.app_id == ""
    assert attrs.app_name == "Some App"


# ---------------------------------------------------------------------------
# DataSourceEvent
# ---------------------------------------------------------------------------


def test_data_source_event_get_notification_attributes():
    inner = _make_notification_attrs_payload(7, "com.example", "Title", "Body")
    data = bytes([CommandID.GetNotificationAttributes]) + inner
    ev = DataSourceEvent.parse(data)
    assert ev.type == CommandID.GetNotificationAttributes
    attrs = ev.as_notification_attributes()
    assert attrs.id == 7
    assert attrs.title == "Title"


def test_data_source_event_get_app_attributes():
    inner = _make_app_attrs_payload("com.example", "My App")
    data = bytes([CommandID.GetAppAttributes]) + inner
    ev = DataSourceEvent.parse(data)
    assert ev.type == CommandID.GetAppAttributes
    attrs = ev.as_app_attributes()
    assert attrs.app_id == "com.example"
    assert attrs.app_name == "My App"


def test_data_source_event_wrong_type_raises():
    inner = _make_notification_attrs_payload(1, "a", "b", "c")
    data = bytes([CommandID.GetAppAttributes]) + inner
    ev = DataSourceEvent.parse(data)
    with pytest.raises(AssertionError):
        ev.as_notification_attributes()
