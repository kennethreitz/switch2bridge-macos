"""
Wired (USB) transport for the Switch 2 Pro Controller
=====================================================

Plugged in, the controller enumerates but stays silent until it is told to
start reporting. The command that does it has to go to a *vendor-class*
interface, not the HID one — which is why sending output reports through
hidapi looks like it succeeds and does nothing.

Interface map (verified on hardware):

    interface 0  class 0x03 HID     ep 0x81 IN, 0x01 OUT   <- input reports
    interface 1  class 0xff vendor  ep 0x02 OUT, 0x82 IN   <- commands
    interface 2-4 class 0x01 audio                          (unused)

macOS claims interface 0 for HID, so input is read with hidapi while
commands go out over libusb on interface 1. Once initialised the controller
streams report 0x09 at ~250 Hz — against ~33 Hz over Bluetooth, and with a
4 ms interval instead of 30 ms.

The report body is byte-identical to the Bluetooth one; USB merely prepends
the HID report id, so the existing parser handles it after stripping byte 0.

Command sequence from TommyWabg/Switch2Connect (GPL-3.0) — used as a
protocol reference only.
"""

import controller_commands as cc
import controller_pairing as cp
import logging
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

VENDOR_ID = 0x057E
PRODUCT_ID = 0x2069

COMMAND_INTERFACE = 1
ENDPOINT_OUT = 0x02
ENDPOINT_IN = 0x82

INPUT_REPORT_ID = 0x09

# Transport byte (header offset 2) is 0x00 for USB, 0x01 for Bluetooth.
TRANSPORT_USB = 0x00


def _frame(report_type, command, payload=b"", length=None):
    head = bytes([report_type, 0x91, TRANSPORT_USB, command, 0x00,
                  length if length is not None else len(payload), 0x00, 0x00])
    return head + payload


# All-0xFF stands in for the console's address; the controller accepts it.
_HOST_MAC = bytes([0xFF] * 6)

INIT_SEQUENCE = (
    ("init", _frame(0x03, 0x0D, bytes([0x01, 0x00]) + _HOST_MAC, length=0x08)),
    ("feature-mask", _frame(0x0C, 0x02, bytes([0x27, 0, 0, 0]), length=0x04)),
    ("enable-features", _frame(0x0C, 0x04, bytes([0x27, 0, 0, 0]), length=0x04)),
    ("select-report", _frame(0x03, 0x0A, bytes([INPUT_REPORT_ID, 0, 0, 0]),
                             length=0x04)),
)


def player_light_command(pattern):
    return _frame(0x09, 0x07, bytes([pattern & 0x0F]) + bytes(7), length=0x08)


def _bundled_libusb():
    """Path to libusb inside a py2app bundle, if we shipped one.

    pyusb finds libusb through a hardcoded Homebrew path, which is absent on
    machines without brew. In a bundle we ship the dylib and point pyusb
    straight at it so wired mode works out of the box.
    """
    if not getattr(sys, "frozen", False):
        return None
    frameworks = Path(sys.executable).resolve().parent.parent / "Frameworks"
    for name in ("libusb-1.0.0.dylib", "libusb-1.0.dylib"):
        candidate = frameworks / name
        if candidate.exists():
            return str(candidate)
    return None


_backend = None
_backend_resolved = False


def usb_backend():
    """pyusb backend, preferring the bundled dylib. None means 'let pyusb try'."""
    global _backend, _backend_resolved
    if _backend_resolved:
        return _backend
    _backend_resolved = True
    bundled = _bundled_libusb()
    if bundled:
        try:
            import usb.backend.libusb1
            _backend = usb.backend.libusb1.get_backend(find_library=lambda _: bundled)
            log.info("using bundled libusb at %s", bundled)
        except Exception as e:
            log.warning("bundled libusb unusable (%s); falling back", e)
    return _backend


def dependencies_available():
    """True when both halves of the transport can be imported."""
    try:
        import hid  # noqa: F401
        import usb.core  # noqa: F401
        return True
    except Exception:
        return False


def is_connected():
    """True when the controller is present on USB, without claiming it."""
    try:
        import hid
        return bool(hid.enumerate(VENDOR_ID, PRODUCT_ID))
    except Exception:
        return False


class USBTransport:
    """Reads input reports from a wired controller.

    `on_report` is called with the report body, with the HID report id
    stripped, so it matches the Bluetooth payload exactly.
    """

    # Frames to keep sending after an effect ends. One dropped stop frame would
    # otherwise leave the motors running until the next effect.
    RUMBLE_TRAILING_FRAMES = 3

    def __init__(self, on_report, on_error=None):
        self.on_report = on_report
        self.on_error = on_error
        self.last_error = None
        self.connected = False
        self._device = None       # libusb handle, interface 1
        self._hid = None          # hidapi handle, interface 0
        self._claimed = False
        self._stop = threading.Event()
        self._thread = None
        # Rumble runs on its own thread: the motors decay unless refreshed every
        # few milliseconds, which is not something the caller should have to do.
        self._rumble = (0, 0)
        self._rumble_seq = 0
        self._rumble_lock = threading.Lock()
        self._rumble_wake = threading.Event()
        self._rumble_thread = None
        self._write_lock = threading.Lock()

    # --- lifecycle ---

    def _claim_command_interface(self):
        import usb.core
        import usb.util

        device = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID,
                               backend=usb_backend())
        if device is None:
            self.last_error = (
                "Controller not visible over USB. Use a data cable — "
                "charge-only cables do not enumerate."
            )
            return None
        try:
            if device.is_kernel_driver_active(COMMAND_INTERFACE):
                device.detach_kernel_driver(COMMAND_INTERFACE)
        except Exception:
            pass  # not applicable to a vendor-class interface on macOS
        try:
            usb.util.claim_interface(device, COMMAND_INTERFACE)
        except Exception as e:
            self.last_error = (
                f"Could not claim the controller's command interface: {e}"
            )
            return None
        self._claimed = True
        return device

    def read_stick_calibration(self):
        """Factory calibration over the same bulk channel the init uses.

        The command frames are transport-agnostic, so this is the identical
        SPI read the Bluetooth path performs. Call before the reader thread
        starts; it uses the bulk IN endpoint, not the HID interface.
        """
        if self._device is None:
            return None, None

        def spi(address, length):
            try:
                self._device.write(ENDPOINT_OUT,
                                   cc.spi_read_command(address, length), 1000)
            except Exception as e:
                log.warning("USB SPI read failed: %s", e)
                return None
            for _attempt in range(4):
                try:
                    reply = bytes(self._device.read(ENDPOINT_IN, 96, 500))
                except Exception:
                    return None
                parsed = cc.parse_spi_reply(reply)
                if parsed and parsed[0] == address:
                    return parsed[1]
            return None

        left = cc.decode_stick_block(spi(cc.FACTORY_STICK_LEFT, cc.STICK_BLOCK_LEN))
        right = cc.decode_stick_block(spi(cc.FACTORY_STICK_RIGHT, cc.STICK_BLOCK_LEN))
        return left, right

    def _run_init(self):
        replies = 0
        for name, command in INIT_SEQUENCE:
            try:
                self._device.write(ENDPOINT_OUT, command, 1000)
            except Exception as e:
                log.warning("USB init %s failed: %s", name, e)
                continue
            try:
                self._device.read(ENDPOINT_IN, 64, 400)
                replies += 1
            except Exception:
                pass  # not every command is acknowledged
            time.sleep(0.02)
        log.info("USB init sent (%d/%d acknowledged)", replies, len(INIT_SEQUENCE))

    def connect(self):
        """Claim, initialise and start streaming. Returns True on success."""
        self.last_error = None
        if not dependencies_available():
            self.last_error = (
                "Wired mode needs pyusb and hidapi:\n"
                "    pip install pyusb hidapi\n"
                "    brew install libusb"
            )
            return False

        import hid

        self._device = self._claim_command_interface()
        if self._device is None:
            return False

        self._run_init()

        try:
            entries = hid.enumerate(VENDOR_ID, PRODUCT_ID)
            if not entries:
                raise RuntimeError("HID interface disappeared after init")
            handle = hid.device()
            handle.open_path(entries[0]["path"])
            handle.set_nonblocking(True)
            self._hid = handle
        except Exception as e:
            self.last_error = f"Could not open the controller's HID interface: {e}"
            self.disconnect()
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop,
                                        name="usb-reader", daemon=True)
        self._thread.start()
        self._rumble_thread = threading.Thread(target=self._rumble_loop,
                                               name="usb-rumble", daemon=True)
        self._rumble_thread.start()
        self.connected = True
        log.info("USB transport connected")
        return True

    # --- pairing ---

    def run_pairing(self, host_address, commit=False):
        """Pair the controller with `host_address` over the channel we hold.

        Reusing this transport's own claim is the point: the standalone tool
        needs the app closed precisely because two processes cannot claim the
        same interface, and from in here there is nothing to close.

        With `commit` false nothing is written — see controller_pairing.pair.
        """
        if self._device is None:
            raise RuntimeError("the controller is not connected over USB")

        def send(frame):
            self._device.write(ENDPOINT_OUT, frame, 1000)
            # Replies to other traffic can sit ahead of ours in the pipe, so
            # read until a pairing reply appears rather than taking the first.
            for _attempt in range(6):
                try:
                    reply = bytes(self._device.read(ENDPOINT_IN, 96, 1000))
                except Exception:
                    return b""
                if reply and reply[0] == cp.CMD_PAIRING:
                    return reply
            return b""

        return cp.pair(send, host_address, transport=cp.TRANSPORT_USB,
                       commit=commit)

    # --- rumble ---

    def set_rumble(self, low_amplitude, high_amplitude):
        """Drive both actuators, 0-65535 each. Zero on both stops them.

        Returns immediately; the effect is held by a background thread until
        it is changed or cleared, because the motors decay if the report is
        not repeated.
        """
        pair = (max(0, min(0xFFFF, int(low_amplitude))),
                max(0, min(0xFFFF, int(high_amplitude))))
        with self._rumble_lock:
            if pair == self._rumble:
                return
            self._rumble = pair
        self._rumble_wake.set()

    def _write_report(self, report):
        with self._write_lock:
            handle = self._hid
            if handle is None:
                return False
            try:
                handle.write(report)
                return True
            except Exception as e:
                log.warning("USB rumble write failed: %s", e)
                return False

    def _rumble_loop(self):
        """Repeat the current effect, and stop cleanly when it ends."""
        trailing = 0
        while not self._stop.is_set():
            with self._rumble_lock:
                low, high = self._rumble
            if low or high:
                trailing = self.RUMBLE_TRAILING_FRAMES
            elif trailing > 0:
                trailing -= 1     # a few explicit zero frames, then go quiet
            else:
                # Idle: block until something changes rather than spinning at
                # 80 Hz writing zeros nobody asked for.
                self._rumble_wake.wait(0.5)
                self._rumble_wake.clear()
                continue
            self._rumble_seq += 1
            if not self._write_report(
                cc.rumble_report(low, high, self._rumble_seq)
            ):
                return
            time.sleep(cc.RUMBLE_RESEND_INTERVAL)

    def set_player_light(self, pattern):
        if self._device is None:
            return
        try:
            self._device.write(ENDPOINT_OUT, player_light_command(pattern), 1000)
        except Exception as e:
            log.warning("USB player light failed: %s", e)

    def _read_loop(self):
        idle = 0
        while not self._stop.is_set():
            try:
                data = self._hid.read(96)
            except Exception as e:
                log.warning("USB read failed: %s", e)
                self.last_error = "The wired controller was disconnected."
                break
            if not data:
                idle += 1
                # 250 Hz means a report every 4 ms; a long gap means it is gone
                if idle > 2000:
                    self.last_error = "The wired controller stopped responding."
                    break
                time.sleep(0.001)
                continue
            idle = 0
            report = bytes(data)
            if report[0] != INPUT_REPORT_ID:
                continue
            try:
                self.on_report(report[1:])   # strip the HID report id
            except Exception:
                log.exception("USB report handler failed")
        self.connected = False
        if self.last_error and self.on_error:
            try:
                self.on_error(self.last_error)
            except Exception:
                pass

    def disconnect(self):
        # Silence the motors first. Dropping the handle mid-effect would leave
        # the controller buzzing with nothing left to tell it to stop.
        with self._rumble_lock:
            self._rumble = (0, 0)
        if self._hid is not None:
            self._rumble_seq += 1
            self._write_report(cc.rumble_report(0, 0, self._rumble_seq))

        self._stop.set()
        self._rumble_wake.set()
        for thread in (self._thread, self._rumble_thread):
            if thread and thread.is_alive():
                thread.join(1.0)
        self._thread = None
        self._rumble_thread = None
        with self._write_lock:
            if self._hid is not None:
                try:
                    self._hid.close()
                except Exception:
                    pass
                self._hid = None
        if self._device is not None and self._claimed:
            try:
                import usb.util
                usb.util.release_interface(self._device, COMMAND_INTERFACE)
            except Exception:
                pass
        self._device = None
        self._claimed = False
        self.connected = False
        log.info("USB transport disconnected")
