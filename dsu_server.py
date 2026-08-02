"""
DSU (cemuhook) server for Switch2 Bridge
========================================

Exposes the controller as a DSU pad on UDP so emulators that speak the
cemuhook protocol (Dolphin, Cemu, Ryujinx) can read it as a real analog
gamepad — no driver, no permissions, no keyboard in the middle.

The pad presents itself as a Switch Pro Controller: full gyro model, a
Bluetooth connection type, and the Switch face-button layout mapped to its
positional DSU equivalents (A is the right face button, so it lands on
Circle, and so on).

Protocol reference: https://github.com/v1993/cemuhook-protocol
"""

import logging
import socket
import struct
import threading
import time
import zlib

from controller_state import monotonic_us

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1001
SERVER_ID = 0x53324252  # "S2BR"

MSG_VERSION = 0x100000
MSG_PORTS = 0x100001
MSG_DATA = 0x100002
# Rumble is an unofficial extension to the protocol rather than part of
# rajkosto's original. Client support is thin — Cemu has an open feature
# request for it — so this answers correctly and does nothing if nobody asks.
MSG_MOTOR_INFO = 0x110001
MSG_RUMBLE = 0x110002

# The Pro 2 has a left and a right actuator, so DSU motor ids 0 and 1.
MOTOR_COUNT = 2
# Clients re-send rumble a few times a second. If one stops without sending a
# zero the pad would buzz forever, so drop the effect after this long.
RUMBLE_TIMEOUT = 5.0

# Locally-administered, made-up MAC so clients can identify the pad
PAD_MAC = b"\x02S2BRG"
PAD_SLOT = 0
MODEL_FULL_GYRO = 2
CONN_BLUETOOTH = 2
BATTERY_NA = 0x00
BATTERY_FULL = 0x05

# DSU has exactly 16 button slots and no room for the Switch 2's extra
# GL/GR/C buttons. They stay unmapped unless the user aliases them onto a
# DSU button in mappings.json.
ALIASABLE_BUTTONS = ('GL', 'GR', 'C')

# Clients must re-send a data request at least this often to keep receiving
CLIENT_TIMEOUT = 5.0

# DSU buttons, mapped positionally from the Switch layout
# (A→Circle, B→Cross, X→Triangle, Y→Square, -→Share, +→Options,
#  HOME→PS, CAPT→Touch; GL/GR/C have no DSU equivalent)
_BUTTONS1 = (  # payload byte 36: (dsu bit, switch button name)
    (0x01, '-'), (0x02, 'LS'), (0x04, 'RS'), (0x08, '+'),
    (0x10, 'DUP'), (0x20, 'DRIGHT'), (0x40, 'DDOWN'), (0x80, 'DLEFT'),
)
_BUTTONS2 = (  # payload byte 37
    (0x01, 'ZL'), (0x02, 'ZR'), (0x04, 'L'), (0x08, 'R'),
    (0x10, 'X'), (0x20, 'A'), (0x40, 'B'), (0x80, 'Y'),
)
# Analog button bytes 44..55, in protocol order
_ANALOG_ORDER = (
    'DLEFT', 'DDOWN', 'DRIGHT', 'DUP',
    'Y', 'B', 'A', 'X',
    'R', 'L', 'ZR', 'ZL',
)


def _axis_to_byte(value):
    """[-1.0, 1.0] → [0, 255] with 128 center (255 = right/up)."""
    return max(0, min(255, int(round((value + 1.0) * 127.5))))


class DSUServer:
    """Threaded UDP server. `push()` may be called from any thread."""

    def __init__(self, host="127.0.0.1", port=26760, aliases=None,
                 on_rumble=None):
        self.host = host
        self.port = port
        # Switch 2-only buttons folded onto DSU slots, e.g. {"GL": "L"}
        self.aliases = dict(aliases or {})
        # Called with two 0-65535 amplitudes when a client asks for rumble.
        # None means the pad cannot rumble, which is reported honestly as a
        # motor count of zero rather than accepting effects and dropping them.
        self.on_rumble = on_rumble
        self.battery = BATTERY_NA
        self.last_error = None  # consumed by the UI tick
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._clients = {}  # addr -> last request time
        self._counter = 0
        self._connected = False
        self._motors = [0, 0]      # latest intensity per DSU motor id
        self._rumble_seen = 0.0    # when a client last said anything about it

    # --- lifecycle ---

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return True
        self._stop.clear()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.settimeout(1.0)
        except OSError as e:
            log.warning("DSU server could not bind %s:%s: %s", self.host, self.port, e)
            self.last_error = (
                f"DSU server could not listen on {self.host}:{self.port}: {e}"
            )
            return False
        self._sock = sock
        self.port = sock.getsockname()[1]  # resolve port 0 → real port
        self._thread = threading.Thread(
            target=self._serve, name="dsu-server", daemon=True
        )
        self._thread.start()
        log.info("DSU server listening on %s:%s", self.host, self.port)
        return True

    def stop(self):
        # Shutting the server down must not leave a running effect behind.
        with self._lock:
            buzzing = any(self._motors)
            self._motors = [0, 0]
        if buzzing:
            self._emit_rumble(0, 0)
        self._stop.set()
        if self._thread:
            self._thread.join(2.0)
        self._thread = None
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        with self._lock:
            self._clients.clear()

    def set_connected(self, connected):
        self._connected = bool(connected)

    def client_count(self):
        now = time.monotonic()
        with self._lock:
            return sum(1 for t in self._clients.values() if now - t < CLIENT_TIMEOUT)

    # --- packet builders ---

    def _packet(self, msg_type, payload):
        data = struct.pack("<I", msg_type) + payload
        header = struct.pack(
            "<4sHHII", b"DSUS", PROTOCOL_VERSION, len(data), 0, SERVER_ID
        )
        pkt = bytearray(header + data)
        struct.pack_into("<I", pkt, 8, zlib.crc32(bytes(pkt)) & 0xFFFFFFFF)
        return bytes(pkt)

    def _port_info(self, slot):
        if slot == PAD_SLOT:
            state = 2 if self._connected else 0
            return struct.pack(
                "<BBBB6sB", slot, state, MODEL_FULL_GYRO, CONN_BLUETOOTH,
                PAD_MAC, self.battery,
            )
        return struct.pack("<BBBB6sB", slot, 0, 0, 0, b"\x00" * 6, 0)

    # --- rumble ---

    def _set_motor(self, motor, intensity):
        with self._lock:
            # Refresh the deadline even when the value is unchanged: clients
            # re-send the same intensity precisely to say "still going".
            self._rumble_seen = time.monotonic()
            if self._motors[motor] == intensity:
                return
            self._motors[motor] = intensity
            low, high = self._motors
        self._emit_rumble(low, high)

    def _emit_rumble(self, low, high):
        callback = self.on_rumble
        if callback is None:
            return
        try:
            # DSU motor 0 is the large/low-frequency actuator and 1 the small
            # high-frequency one; 0-255 there, 16-bit amplitudes on the pad.
            callback(low * 257, high * 257)
        except Exception:
            log.exception("DSU rumble callback failed")

    def _expire_rumble(self):
        """Stop the motors when the client driving them goes quiet.

        A client that dies mid-effect never sends the zero, so without this the
        controller would buzz until it was unplugged.
        """
        with self._lock:
            if not any(self._motors):
                return
            if time.monotonic() - self._rumble_seen <= RUMBLE_TIMEOUT:
                return
            self._motors = [0, 0]
        log.info("DSU: no rumble packets for %.0fs, stopping the motors",
                 RUMBLE_TIMEOUT)
        self._emit_rumble(0, 0)

    def _effective_buttons(self, state):
        """State buttons plus any aliased Switch 2-only buttons folded in."""
        buttons = dict(state.buttons)
        for source, target in self.aliases.items():
            if state.pressed(source):
                buttons[target] = 1
        return buttons

    def _data_packet(self, state):
        buttons = self._effective_buttons(state)
        b1 = 0
        for bit, name in _BUTTONS1:
            if buttons.get(name):
                b1 |= bit
        b2 = 0
        for bit, name in _BUTTONS2:
            if buttons.get(name):
                b2 |= bit

        self._counter = (self._counter + 1) & 0xFFFFFFFF
        payload = self._port_info(PAD_SLOT)
        payload += struct.pack("<BI", 1, self._counter)  # connected + counter
        payload += struct.pack(
            "<BBBB", b1, b2,
            0xFF if buttons.get('HOME') else 0,
            0xFF if buttons.get('CAPT') else 0,
        )
        payload += struct.pack(
            "<BBBB",
            _axis_to_byte(state.lx), _axis_to_byte(state.ly),
            _axis_to_byte(state.rx), _axis_to_byte(state.ry),
        )
        payload += bytes(
            0xFF if buttons.get(name) else 0 for name in _ANALOG_ORDER
        )
        payload += b"\x00" * 12  # two (inactive) touch structs
        timestamp = state.timestamp_us or monotonic_us()
        payload += struct.pack("<Q", timestamp)
        ax, ay, az = state.accel
        gp, gy, gr = state.gyro
        payload += struct.pack("<6f", ax, ay, az, gp, gy, gr)
        return self._packet(MSG_DATA, payload)

    # --- server loop ---

    def _serve(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(1024)
            except socket.timeout:
                # A client that died mid-effect stops sending anything at all,
                # so the silent path is exactly where expiry has to happen.
                self._expire_rumble()
                continue
            except OSError:
                break  # socket closed
            try:
                self._handle(data, addr)
            except Exception:
                log.exception("DSU: failed to handle request from %s", addr)
            finally:
                self._expire_rumble()
        log.info("DSU server stopped")

    def _handle(self, data, addr):
        if len(data) < 20 or data[:4] != b"DSUC":
            return
        (msg_type,) = struct.unpack_from("<I", data, 16)

        if msg_type == MSG_VERSION:
            reply = self._packet(
                MSG_VERSION, struct.pack("<H", PROTOCOL_VERSION) + b"\x00\x00"
            )
            self._sock.sendto(reply, addr)

        elif msg_type == MSG_PORTS:
            if len(data) < 24:
                return
            (count,) = struct.unpack_from("<i", data, 20)
            slots = data[24:24 + max(0, min(count, 4))]
            for slot in slots:
                reply = self._packet(MSG_PORTS, self._port_info(slot) + b"\x00")
                self._sock.sendto(reply, addr)

        elif msg_type == MSG_DATA:
            # Registration request: keep streaming to this client until timeout
            with self._lock:
                self._clients[addr] = time.monotonic()

        elif msg_type == MSG_MOTOR_INFO:
            motors = MOTOR_COUNT if self.on_rumble else 0
            reply = self._packet(
                MSG_MOTOR_INFO, self._port_info(PAD_SLOT) + bytes([motors])
            )
            self._sock.sendto(reply, addr)

        elif msg_type == MSG_RUMBLE:
            # 8-byte controller header, then motor id and intensity.
            if len(data) < 30 or not self.on_rumble:
                return
            motor, intensity = data[28], data[29]
            if motor < MOTOR_COUNT:
                self._set_motor(motor, intensity)

    # --- input feed (called from the BLE thread) ---

    def push(self, state):
        """Broadcast one ControllerState to every subscribed DSU client."""
        sock = self._sock
        if sock is None or self._stop.is_set():
            return
        now = time.monotonic()
        with self._lock:
            stale = [a for a, t in self._clients.items() if now - t > CLIENT_TIMEOUT]
            for a in stale:
                del self._clients[a]
            targets = list(self._clients)
        if not targets:
            return
        pkt = self._data_packet(state)
        for addr in targets:
            try:
                sock.sendto(pkt, addr)
            except OSError as e:
                log.warning("DSU send to %s failed: %s", addr, e)
