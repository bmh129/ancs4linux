# Tests

## Running the tests

Install pytest if you haven't already:

```bash
pip install pytest
```

Then from the repo root:

```bash
pytest
```

## What is tested

These tests cover the pure-Python protocol logic that requires no hardware or system daemons:

- **`tests/observer/ancs/test_parsers.py`** — binary ANCS packet parsing (`Notification`, `NotificationAttributes`, `AppAttributes`, `DataSourceEvent`)
- **`tests/observer/ancs/test_builders.py`** — binary ANCS message construction (`GetNotificationAttributes`, `GetAppAttributes`, `PerformNotificationAction`)
- **`tests/common/test_apis.py`** — `ShowNotificationData` JSON serialization round-trip

The D-Bus-dependent code (scanner, device, advertising) is not covered here since it requires a running BlueZ stack and a paired iOS device.
