import struct

from ancs4linux.observer.ancs.builders import (
    GetAppAttributes,
    GetNotificationAttributes,
    PerformNotificationAction,
)
from ancs4linux.observer.ancs.constants import (
    USHORT_MAX,
    ActionID,
    AppAttributeID,
    CommandID,
    NotificationAttributeID,
)


# ---------------------------------------------------------------------------
# GetNotificationAttributes
# ---------------------------------------------------------------------------


def test_get_notification_attributes_no_actions():
    msg = GetNotificationAttributes(id=1, get_positive_action=False, get_negative_action=False)
    result = msg.to_list()
    expected = list(
        struct.pack(
            "<BIBBHBH",
            CommandID.GetNotificationAttributes,
            1,
            NotificationAttributeID.AppIdentifier,
            NotificationAttributeID.Title,
            USHORT_MAX,
            NotificationAttributeID.Message,
            USHORT_MAX,
        )
    )
    assert result == expected


def test_get_notification_attributes_positive_action_only():
    msg = GetNotificationAttributes(id=2, get_positive_action=True, get_negative_action=False)
    result = msg.to_list()
    expected = list(
        struct.pack(
            "<BIBBHBHB",
            CommandID.GetNotificationAttributes,
            2,
            NotificationAttributeID.AppIdentifier,
            NotificationAttributeID.Title,
            USHORT_MAX,
            NotificationAttributeID.Message,
            USHORT_MAX,
            NotificationAttributeID.PositiveActionLabel,
        )
    )
    assert result == expected


def test_get_notification_attributes_negative_action_only():
    msg = GetNotificationAttributes(id=3, get_positive_action=False, get_negative_action=True)
    result = msg.to_list()
    expected = list(
        struct.pack(
            "<BIBBHBHB",
            CommandID.GetNotificationAttributes,
            3,
            NotificationAttributeID.AppIdentifier,
            NotificationAttributeID.Title,
            USHORT_MAX,
            NotificationAttributeID.Message,
            USHORT_MAX,
            NotificationAttributeID.NegativeActionLabel,
        )
    )
    assert result == expected


def test_get_notification_attributes_both_actions():
    msg = GetNotificationAttributes(id=4, get_positive_action=True, get_negative_action=True)
    result = msg.to_list()
    expected = list(
        struct.pack(
            "<BIBBHBHBB",
            CommandID.GetNotificationAttributes,
            4,
            NotificationAttributeID.AppIdentifier,
            NotificationAttributeID.Title,
            USHORT_MAX,
            NotificationAttributeID.Message,
            USHORT_MAX,
            NotificationAttributeID.PositiveActionLabel,
            NotificationAttributeID.NegativeActionLabel,
        )
    )
    assert result == expected


def test_get_notification_attributes_returns_list_of_ints():
    msg = GetNotificationAttributes(id=1, get_positive_action=False, get_negative_action=False)
    result = msg.to_list()
    assert isinstance(result, list)
    assert all(isinstance(b, int) for b in result)


def test_get_notification_attributes_uid_zero():
    msg = GetNotificationAttributes(id=0, get_positive_action=False, get_negative_action=False)
    result = msg.to_list()
    uid_bytes = result[1:5]  # bytes 1-4 are the UID (little-endian uint32)
    assert struct.unpack("<I", bytes(uid_bytes))[0] == 0


def test_get_notification_attributes_uid_max():
    uid = (2**32) - 1
    msg = GetNotificationAttributes(id=uid, get_positive_action=False, get_negative_action=False)
    result = msg.to_list()
    uid_bytes = result[1:5]
    assert struct.unpack("<I", bytes(uid_bytes))[0] == uid


# ---------------------------------------------------------------------------
# GetAppAttributes
# ---------------------------------------------------------------------------


def test_get_app_attributes_basic():
    app_id = "com.example.app"
    msg = GetAppAttributes(app_id=app_id)
    result = msg.to_list()
    expected = list(
        struct.pack(
            f"<B{len(app_id) + 1}sB",
            CommandID.GetAppAttributes,
            app_id.encode("utf8"),
            AppAttributeID.DisplayName,
        )
    )
    assert result == expected


def test_get_app_attributes_contains_null_terminated_app_id():
    msg = GetAppAttributes(app_id="com.test")
    result = msg.to_list()
    raw = bytes(result)
    assert raw[0] == CommandID.GetAppAttributes
    # app_id bytes should be null-terminated
    null_pos = raw.index(0, 1)
    assert raw[1:null_pos] == b"com.test"
    assert raw[null_pos] == 0
    assert raw[null_pos + 1] == AppAttributeID.DisplayName


def test_get_app_attributes_returns_list_of_ints():
    msg = GetAppAttributes(app_id="a")
    result = msg.to_list()
    assert isinstance(result, list)
    assert all(isinstance(b, int) for b in result)


def test_get_app_attributes_empty_app_id():
    msg = GetAppAttributes(app_id="")
    result = msg.to_list()
    raw = bytes(result)
    assert raw[0] == CommandID.GetAppAttributes
    assert raw[1] == 0  # null terminator immediately after command
    assert raw[2] == AppAttributeID.DisplayName


# ---------------------------------------------------------------------------
# PerformNotificationAction
# ---------------------------------------------------------------------------


def test_perform_positive_action():
    msg = PerformNotificationAction(notification_id=10, is_positive=True)
    result = msg.to_list()
    expected = list(
        struct.pack("<BIB", CommandID.PerformNotificationAction, 10, ActionID.Positive)
    )
    assert result == expected


def test_perform_negative_action():
    msg = PerformNotificationAction(notification_id=10, is_positive=False)
    result = msg.to_list()
    expected = list(
        struct.pack("<BIB", CommandID.PerformNotificationAction, 10, ActionID.Negative)
    )
    assert result == expected


def test_perform_action_uid_encoded_correctly():
    uid = 123456
    msg = PerformNotificationAction(notification_id=uid, is_positive=True)
    result = msg.to_list()
    uid_bytes = result[1:5]
    assert struct.unpack("<I", bytes(uid_bytes))[0] == uid


def test_perform_action_returns_list_of_ints():
    msg = PerformNotificationAction(notification_id=1, is_positive=True)
    result = msg.to_list()
    assert isinstance(result, list)
    assert all(isinstance(b, int) for b in result)
