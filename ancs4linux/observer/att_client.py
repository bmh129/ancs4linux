import errno as _errno
import logging
import socket
import struct
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from ancs4linux.common.dbus import Variant
from ancs4linux.observer.ancs.constants import (
    ANCS_SERVICE,
    CONTROL_POINT_CHAR,
    DATA_SOURCE_CHAR,
    NOTIFICATION_SOURCE_CHAR,
)

log = logging.getLogger(__name__)

_ATT_EXCHANGE_MTU_REQ = 0x02
_ATT_EXCHANGE_MTU_RSP = 0x03
_ATT_ERROR_RSP = 0x01
_ATT_READ_BY_TYPE_REQ = 0x08
_ATT_READ_BY_TYPE_RSP = 0x09
_ATT_READ_BY_GROUP_TYPE_REQ = 0x10
_ATT_READ_BY_GROUP_TYPE_RSP = 0x11
_ATT_WRITE_REQ = 0x12
_ATT_WRITE_RSP = 0x13
_ATT_HANDLE_VALUE_NTF = 0x1B
_ATT_HANDLE_VALUE_IND = 0x1D
_ATT_HANDLE_VALUE_CFM = 0x1E
_ATT_WRITE_CMD = 0x52

# ATT request opcodes that the iPhone may send to our (nonexistent) GATT server.
# We must reply to each with ATT_ERROR_RSP so the iPhone doesn't block.
_ATT_SERVER_REQUEST_OPCODES = frozenset({
    0x04,  # Find Information Request
    0x06,  # Find By Type Value Request
    0x08,  # Read By Type Request
    0x0A,  # Read Request
    0x0C,  # Read Blob Request
    0x0E,  # Read Multiple Request
    0x10,  # Read By Group Type Request
    0x12,  # Write Request
    0x16,  # Prepare Write Request
    0x18,  # Execute Write Request
})

_UUID_PRIMARY_SERVICE = 0x2800
_UUID_CHARACTERISTIC = 0x2803
_BTPROTO_L2CAP = 0
_ATT_PSM = 31  # BR/EDR fixed ATT PSM
_OUR_MTU = 512


def _uuid128_le(uuid_str: str) -> bytes:
    return bytes(reversed(bytes.fromhex(uuid_str.replace("-", ""))))


class FakeSignal:
    """Drop-in for dasbus Signal used by BluezGattCharacteristicAPI."""

    def __init__(self) -> None:
        self._cb: Optional[Callable] = None

    def connect(self, cb: Callable) -> None:
        self._cb = cb

    def disconnect(self) -> None:
        self._cb = None

    def emit(self, *args) -> None:
        if self._cb:
            try:
                self._cb(*args)
            except Exception as e:
                log.error(f"FakeSignal callback raised: {e}")


class FakeGattChar:
    """Duck-typed BluezGattCharacteristicAPI backed by a direct ATT connection."""

    interface = "org.bluez.GattCharacteristic1"

    def __init__(
        self, conn: "ATTConnection", value_handle: int, ccc_handle: Optional[int]
    ) -> None:
        self._conn = conn
        self._value_handle = value_handle
        self._ccc_handle = ccc_handle
        self.PropertiesChanged = FakeSignal()

    def StartNotify(self) -> None:
        if self._ccc_handle is not None:
            self._conn._write_req(self._ccc_handle, b"\x01\x00", callback=None)

    def StopNotify(self) -> None:
        if self._ccc_handle is not None:
            self._conn._write_req(self._ccc_handle, b"\x00\x00", callback=None)

    def WriteValue(self, value: List[int], options: Dict) -> None:
        self._conn._write_cmd(self._value_handle, bytes(value))

    def _deliver(self, data: bytes) -> None:
        self.PropertiesChanged.emit(
            "org.bluez.GattCharacteristic1",
            {"Value": Variant("ay", list(data))},
            [],
        )


class ATTConnection:
    """
    Direct BR/EDR ATT client. Opens L2CAP PSM 31 to device_mac, performs GATT
    service and characteristic discovery for ANCS, then calls
    on_ready(ns_char, cp_char, ds_char). Calls on_failed() on any error.
    """

    def __init__(
        self,
        device_mac: str,
        on_ready: Callable[[FakeGattChar, FakeGattChar, FakeGattChar], None],
        on_failed: Callable[[], None],
    ) -> None:
        self._mac = device_mac
        self._on_ready = on_ready
        self._on_failed = on_failed
        self._sock: Optional[socket.socket] = None
        self._watch: Optional[int] = None
        self._connected = False
        self._failed = False

        # Strictly one outstanding ATT request at a time.
        self._queue: deque = deque()
        self._req_inflight = False

        # handle → FakeGattChar for incoming notifications/indications
        self._notify_chars: Dict[int, FakeGattChar] = {}

        # Discovery state
        self._ancs_start = 0
        self._ancs_end = 0
        self._chars_found: List[Tuple[int, int, bytes]] = []

    # ---- Public ----

    def open(self) -> None:
        try:
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, _BTPROTO_L2CAP)
            sock.setblocking(False)
            err = sock.connect_ex((self._mac, _ATT_PSM))
        except OSError as e:
            log.error(f"ATT socket error for {self._mac}: {e}")
            self._fail()
            return

        if err not in (0, _errno.EINPROGRESS):
            log.error(f"ATT connect_ex({self._mac}) returned {err}")
            sock.close()
            self._fail()
            return

        self._sock = sock
        if err == 0:
            self._connected = True
            self._watch = GLib.io_add_watch(
                sock.fileno(), GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP, self._on_io
            )
            GLib.idle_add(self._start_mtu_exchange)
        else:
            self._watch = GLib.io_add_watch(
                sock.fileno(),
                GLib.IO_IN | GLib.IO_OUT | GLib.IO_ERR | GLib.IO_HUP,
                self._on_io,
            )

    def close(self) -> None:
        if self._watch is not None:
            GLib.source_remove(self._watch)
            self._watch = None
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    # ---- Internal: send ----

    def _write_req(self, handle: int, value: bytes, callback: Optional[Callable]) -> None:
        pdu = struct.pack("<BH", _ATT_WRITE_REQ, handle) + value
        self._enqueue(pdu, callback or (lambda _: None))

    def _write_cmd(self, handle: int, value: bytes) -> None:
        # Write Commands require no response — skip the queue.
        self._send(struct.pack("<BH", _ATT_WRITE_CMD, handle) + value)

    def _enqueue(self, pdu: bytes, on_rsp: Callable[[bytes], None]) -> None:
        self._queue.append((pdu, on_rsp))
        self._pump()

    def _pump(self) -> None:
        if self._req_inflight or not self._queue or self._sock is None:
            return
        pdu, _ = self._queue[0]
        self._req_inflight = True
        self._send(pdu)

    def _complete(self, rsp: bytes) -> None:
        if not self._queue:
            return
        _, cb = self._queue.popleft()
        self._req_inflight = False
        try:
            cb(rsp)
        except Exception as e:
            log.error(f"ATT response handler raised: {e}")
        self._pump()

    def _send(self, pdu: bytes) -> None:
        if self._sock is None:
            return
        log.debug(f"ATT TX {self._mac}: {pdu.hex()}")
        try:
            self._sock.send(pdu)
        except OSError as e:
            log.error(f"ATT send error for {self._mac}: {e}")

    # ---- Internal: IO ----

    def _on_io(self, fd: int, condition: int) -> bool:
        if not self._connected:
            return self._handle_connecting(condition)

        if condition & (GLib.IO_ERR | GLib.IO_HUP):
            log.info(f"ATT connection closed for {self._mac}")
            self._watch = None  # returning False removes the source
            self.close()
            return False

        if condition & GLib.IO_IN:
            try:
                data = self._sock.recv(4096)
            except BlockingIOError:
                return True
            except OSError as e:
                log.error(f"ATT recv error for {self._mac}: {e}")
                return True
            if not data:
                log.info(f"ATT peer closed for {self._mac}")
                self._watch = None
                self.close()
                return False
            self._dispatch(data)
        return True

    def _handle_connecting(self, condition: int) -> bool:
        if condition & (GLib.IO_ERR | GLib.IO_HUP):
            log.error(f"ATT connect error for {self._mac}")
            self._watch = None
            self.close()
            self._fail()
            return False
        if condition & GLib.IO_OUT:
            err = self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err != 0:
                log.error(f"ATT connect failed ({err}) for {self._mac}")
                self._watch = None
                self.close()
                self._fail()
                return False
            self._connected = True
            # Replace IO_OUT watch with IO_IN watch; return False removes the old one.
            self._watch = GLib.io_add_watch(
                self._sock.fileno(), GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP, self._on_io
            )
            self._start_mtu_exchange()
            return False
        return True

    # ---- Internal: ATT dispatch ----

    def _dispatch(self, pdu: bytes) -> None:
        if not pdu:
            return
        op = pdu[0]

        if op == _ATT_HANDLE_VALUE_IND:
            handle = struct.unpack_from("<H", pdu, 1)[0]
            self._send(bytes([_ATT_HANDLE_VALUE_CFM]))
            log.debug(f"ATT IND handle=0x{handle:04x} value={pdu[3:].hex()}")
            if handle in self._notify_chars:
                self._notify_chars[handle]._deliver(pdu[3:])

        elif op == _ATT_HANDLE_VALUE_NTF:
            handle = struct.unpack_from("<H", pdu, 1)[0]
            log.debug(f"ATT NTF handle=0x{handle:04x} value={pdu[3:].hex()}")
            if handle in self._notify_chars:
                self._notify_chars[handle]._deliver(pdu[3:])

        elif op in (
            _ATT_EXCHANGE_MTU_RSP,
            _ATT_READ_BY_GROUP_TYPE_RSP,
            _ATT_READ_BY_TYPE_RSP,
            _ATT_WRITE_RSP,
        ):
            self._complete(pdu)

        elif op == _ATT_ERROR_RSP:
            log.debug(f"ATT ERROR: {pdu.hex()}")
            self._complete(pdu)

        elif op == _ATT_EXCHANGE_MTU_REQ:
            # iPhone also wants to negotiate MTU; respond as server.
            peer_mtu = struct.unpack_from("<H", pdu, 1)[0]
            log.debug(f"ATT MTU REQ from peer: {peer_mtu}")
            self._send(struct.pack("<BH", _ATT_EXCHANGE_MTU_RSP, _OUR_MTU))

        elif op in _ATT_SERVER_REQUEST_OPCODES:
            # iPhone is probing our (nonexistent) GATT server. Reject every request
            # so iPhone unblocks and can respond to our own queued ATT requests.
            handle = struct.unpack_from("<H", pdu, 1)[0] if len(pdu) >= 3 else 0
            log.debug(f"ATT server probe op=0x{op:02x} handle=0x{handle:04x} pdu={pdu.hex()}; rejecting")
            self._send(struct.pack("<BBHB", _ATT_ERROR_RSP, op, handle, 0x0A))

        else:
            log.debug(f"ATT unhandled op=0x{op:02x} pdu={pdu.hex()}")

    # ---- GATT discovery ----

    def _start_mtu_exchange(self) -> bool:
        # Skip MTU exchange: iPhone often doesn't respond to our ATT_EXCHANGE_MTU_REQ
        # over BR/EDR before it finishes its own GATT client probes, stalling discovery.
        # BR/EDR L2CAP provides at least 48-byte MTU, which is enough with pagination.
        log.debug(f"ATT: starting service discovery for {self._mac}")
        self._discover_services_from(0x0001)
        return False  # one-shot idle

    def _discover_services_from(self, start: int) -> None:
        pdu = struct.pack("<BHHH", _ATT_READ_BY_GROUP_TYPE_REQ, start, 0xFFFF, _UUID_PRIMARY_SERVICE)
        self._enqueue(pdu, self._on_service_rsp)

    def _on_service_rsp(self, rsp: bytes) -> None:
        if rsp[0] == _ATT_ERROR_RSP:
            self._on_services_done()
            return
        if rsp[0] != _ATT_READ_BY_GROUP_TYPE_RSP:
            log.error(f"Unexpected service RSP 0x{rsp[0]:02x} from {self._mac}")
            self._fail()
            return

        fmt = rsp[1]
        data = rsp[2:]
        ancs_le = _uuid128_le(ANCS_SERVICE)
        last_end = 0
        for i in range(0, len(data) - fmt + 1, fmt):
            entry = data[i:i + fmt]
            start_h = struct.unpack_from("<H", entry, 0)[0]
            end_h = struct.unpack_from("<H", entry, 2)[0]
            uuid_b = entry[4:]
            last_end = end_h
            if len(uuid_b) == 16 and uuid_b == ancs_le:
                log.info(f"ATT: ANCS service at 0x{start_h:04x}-0x{end_h:04x}")
                self._ancs_start = start_h
                self._ancs_end = end_h

        if last_end < 0xFFFF:
            self._discover_services_from(last_end + 1)
        else:
            self._on_services_done()

    def _on_services_done(self) -> None:
        if self._ancs_start == 0:
            log.error(f"ATT: ANCS service not found on {self._mac}")
            self._fail()
            return
        self._discover_chars_from(self._ancs_start)

    def _discover_chars_from(self, start: int) -> None:
        pdu = struct.pack(
            "<BHHH", _ATT_READ_BY_TYPE_REQ, start, self._ancs_end, _UUID_CHARACTERISTIC
        )
        self._enqueue(pdu, self._on_char_rsp)

    def _on_char_rsp(self, rsp: bytes) -> None:
        if rsp[0] == _ATT_ERROR_RSP:
            self._on_chars_done()
            return
        if rsp[0] != _ATT_READ_BY_TYPE_RSP:
            log.error(f"Unexpected char RSP 0x{rsp[0]:02x} from {self._mac}")
            self._fail()
            return

        fmt = rsp[1]
        data = rsp[2:]
        last_attr = self._ancs_start
        for i in range(0, len(data) - fmt + 1, fmt):
            entry = data[i:i + fmt]
            attr_h = struct.unpack_from("<H", entry, 0)[0]
            # entry layout: attr_handle(2) properties(1) value_handle(2) uuid(n)
            val_h = struct.unpack_from("<H", entry, 3)[0]
            uuid_b = entry[5:]
            last_attr = attr_h
            self._chars_found.append((attr_h, val_h, uuid_b))

        if last_attr < self._ancs_end:
            self._discover_chars_from(last_attr + 1)
        else:
            self._on_chars_done()

    def _on_chars_done(self) -> None:
        ns_le = _uuid128_le(NOTIFICATION_SOURCE_CHAR)
        cp_le = _uuid128_le(CONTROL_POINT_CHAR)
        ds_le = _uuid128_le(DATA_SOURCE_CHAR)

        ns_val = cp_val = ds_val = None
        for _, val_h, uuid_b in self._chars_found:
            if uuid_b == ns_le:
                ns_val = val_h
            elif uuid_b == cp_le:
                cp_val = val_h
            elif uuid_b == ds_le:
                ds_val = val_h

        if ns_val is None or cp_val is None or ds_val is None:
            log.error(
                f"ATT: incomplete ANCS chars on {self._mac}: "
                f"NS={ns_val} CP={cp_val} DS={ds_val}"
            )
            self._fail()
            return

        # CCD descriptor is always the next handle after the characteristic value.
        ns_ccd = ns_val + 1
        ds_ccd = ds_val + 1
        log.info(
            f"ATT: NS=0x{ns_val:04x}/CCC=0x{ns_ccd:04x} "
            f"CP=0x{cp_val:04x} "
            f"DS=0x{ds_val:04x}/CCC=0x{ds_ccd:04x}"
        )

        ns = FakeGattChar(self, ns_val, ns_ccd)
        cp = FakeGattChar(self, cp_val, None)
        ds = FakeGattChar(self, ds_val, ds_ccd)

        self._notify_chars[ns_val] = ns
        self._notify_chars[ds_val] = ds

        self._on_ready(ns, cp, ds)

    def _fail(self) -> None:
        if not self._failed:
            self._failed = True
            self._on_failed()
