import json

from ancs4linux.common.apis import ShowNotificationData


def _make_data(**overrides) -> ShowNotificationData:
    defaults = dict(
        device_name="iPhone",
        device_handle="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        app_id="com.example.app",
        app_name="Example App",
        id=42,
        title="Hello",
        body="World",
        positive_action=None,
        negative_action=None,
    )
    defaults.update(overrides)
    return ShowNotificationData(**defaults)


def test_json_round_trip():
    original = _make_data()
    restored = ShowNotificationData.parse(original.json())
    assert restored == original


def test_json_round_trip_with_actions():
    original = _make_data(positive_action="Answer", negative_action="Decline")
    restored = ShowNotificationData.parse(original.json())
    assert restored.positive_action == "Answer"
    assert restored.negative_action == "Decline"


def test_json_is_valid_json():
    data = _make_data()
    parsed = json.loads(data.json())
    assert parsed["title"] == "Hello"
    assert parsed["app_id"] == "com.example.app"


def test_json_none_actions_serialize_as_null():
    data = _make_data()
    parsed = json.loads(data.json())
    assert parsed["positive_action"] is None
    assert parsed["negative_action"] is None


def test_parse_preserves_all_fields():
    original = _make_data(
        device_name="iPad",
        app_id="jp.example",
        app_name="日本語アプリ",
        id=999,
        title="件名",
        body="本文",
        positive_action="OK",
        negative_action=None,
    )
    restored = ShowNotificationData.parse(original.json())
    assert restored.device_name == "iPad"
    assert restored.app_name == "日本語アプリ"
    assert restored.id == 999
    assert restored.title == "件名"
    assert restored.body == "本文"
    assert restored.positive_action == "OK"
    assert restored.negative_action is None
