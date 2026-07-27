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

import logging
import threading
import time

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

    # --- lifecycle ---

    def _claim_command_interface(self):
        import usb.core
        import usb.util

        device = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
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
        self.connected = True
        log.info("USB transport connected")
        return True

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
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(1.0)
        self._thread = None
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
