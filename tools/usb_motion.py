#!/usr/bin/env python3
"""
Read motion from a wired Switch 2 Pro Controller
================================================

Motion is not in the report this project streams. Report `0x09` carries a tail
we could never decode; report `0x05` carries plaintext accelerometer and
gyroscope at fixed offsets, and the controller will stream either one.

This brings the controller up over USB, selects report `0x05` and prints live
motion, so the decode can be checked against physical movement rather than
taken on faith.

    python3 tools/usb_motion.py             # live accel / gyro
    python3 tools/usb_motion.py --raw       # one raw report, field by field
    python3 tools/usb_motion.py --seconds 5

Quit "Switch2 Bridge.app" first — it holds the command interface.

Offsets and scaling follow SDL's `SDL_hidapi_switch2.c`; the report layout is
documented in ndeadly's `hid_reports.md`. See docs/PROTOCOL.md.
"""

import argparse
import struct
import sys
import time

try:
    import usb.core
    import usb.util
except ImportError:
    print("pyusb not installed — run: pip install pyusb  (and brew install libusb)")
    sys.exit(1)

try:
    import hid
except ImportError:
    print("hidapi not installed — run: pip install hidapi")
    sys.exit(1)

VENDOR_ID = 0x057E
PRODUCT_ID = 0x2069

COMMAND_INTERFACE = 1
ENDPOINT_OUT = 0x02
ENDPOINT_IN = 0x82

MOTION_REPORT_ID = 0x05

G = 9.80665
# Accelerometer is +/-8g over a signed 16-bit range; gyro coefficient per SDL.
ACCEL_SCALE = G * 8.0 / 32767.0
GYRO_COEFF = 34.8

# Offsets into the report *including* its leading report-id byte.
OFF_COUNTER = 1
OFF_BUTTONS = 5
OFF_LEFT_STICK = 11
OFF_RIGHT_STICK = 14
OFF_BATTERY_MV = 32
OFF_SENSOR_TS = 0x2B
OFF_ACCEL = 0x31       # X, then Z (negated), then Y
OFF_GYRO = 0x37        # X, then Y (negated), then Z

INIT_SEQUENCE = (
    ("unknown-07",      bytes([0x07, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00])),
    ("feature-mask",    bytes([0x0C, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00,
                               0x27, 0x00, 0x00, 0x00])),
    ("unknown-11",      bytes([0x11, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00])),
    ("vibration-cfg",   bytes([0x0A, 0x91, 0x00, 0x08, 0x00, 0x14, 0x00, 0x00,
                               0x01, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
                               0xFF, 0x35, 0x00, 0x46, 0x00, 0x00, 0x00, 0x00,
                               0x00, 0x00, 0x00, 0x00])),
    ("enable-features", bytes([0x0C, 0x91, 0x00, 0x04, 0x00, 0x04, 0x00, 0x00,
                               0x27, 0x00, 0x00, 0x00])),
    ("unknown-01-0c",   bytes([0x01, 0x91, 0x00, 0x0C, 0x00, 0x00, 0x00, 0x00])),
    ("select-report-5", bytes([0x03, 0x91, 0x00, 0x0A, 0x00, 0x04, 0x00, 0x00,
                               MOTION_REPORT_ID, 0x00, 0x00, 0x00])),
    ("start-output",    bytes([0x03, 0x91, 0x00, 0x0D, 0x00, 0x08, 0x00, 0x00,
                               0x01, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])),
)

# Recovery for a controller that streams 0x05 with the IMU dark. A plain enable
# is usually enough; toggling off first has unstuck it when it was not.
DISABLE_FEATURES = bytes([0x0C, 0x91, 0x00, 0x05, 0x00, 0x04, 0x00, 0x00,
                          0x27, 0x00, 0x00, 0x00])
ENABLE_FEATURES = bytes([0x0C, 0x91, 0x00, 0x04, 0x00, 0x04, 0x00, 0x00,
                         0x27, 0x00, 0x00, 0x00])


def s16(report, offset):
    return struct.unpack_from("<h", report, offset)[0]


def send(device, command):
    device.write(ENDPOINT_OUT, command, 1000)
    try:
        return bytes(device.read(ENDPOINT_IN, 64, 400))
    except Exception:
        return b""


def decode_motion(report):
    """(accel, gyro) in m/s^2 and rad/s. Axis order and signs follow SDL."""
    accel = (
        s16(report, OFF_ACCEL) * ACCEL_SCALE,
        s16(report, OFF_ACCEL + 4) * ACCEL_SCALE,
        s16(report, OFF_ACCEL + 2) * -ACCEL_SCALE,
    )
    gyro = (
        s16(report, OFF_GYRO) * GYRO_COEFF / 32767.0,
        s16(report, OFF_GYRO + 2) * -GYRO_COEFF / 32767.0,
        s16(report, OFF_GYRO + 4) * GYRO_COEFF / 32767.0,
    )
    return accel, gyro


def unpack_pair(chunk):
    """Three bytes -> two 12-bit values, the packing the sticks use."""
    return (chunk[0] | ((chunk[1] & 0x0F) << 8),
            ((chunk[1] & 0xF0) >> 4) | (chunk[2] << 4))


def connect():
    device = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if device is None:
        print("❌ No Switch 2 Pro Controller on USB. Use a data cable — "
              "charge-only cables do not enumerate.")
        return None, None
    try:
        usb.util.claim_interface(device, COMMAND_INTERFACE)
    except Exception as e:
        print(f"❌ Could not claim the command interface: {e}")
        print("   Quit 'Switch2 Bridge.app' — it holds interface 1 while wired.")
        return None, None

    print("Init:")
    for name, command in INIT_SEQUENCE:
        reply = send(device, command)
        accepted = {0x01: "ok", 0x04: "rejected"}.get(
            reply[1] if len(reply) > 1 else -1, "no reply")
        print(f"   {name:<16} {accepted}")
        time.sleep(0.02)

    entries = hid.enumerate(VENDOR_ID, PRODUCT_ID)
    if not entries:
        print("❌ The HID interface disappeared after init.")
        usb.util.release_interface(device, COMMAND_INTERFACE)
        return None, None
    handle = hid.device()
    handle.open_path(entries[0]["path"])
    handle.set_nonblocking(True)
    return device, handle


def read_report(handle, timeout=1.0):
    end = time.time() + timeout
    while time.time() < end:
        data = handle.read(96)
        if data:
            report = bytes(data)
            if report[0] == MOTION_REPORT_ID and len(report) > OFF_GYRO + 6:
                return report
        else:
            time.sleep(0.0005)
    return None


def ensure_imu_live(device, handle):
    """The sensor timestamp advancing is the only trustworthy signal.

    A disabled IMU latches its last sample, so the accelerometer keeps reading
    a plausible gravity vector while nothing is actually updating.
    """
    def timestamp_moves():
        stamps = set()
        end = time.time() + 0.6
        while time.time() < end:
            report = read_report(handle, 0.2)
            if report:
                stamps.add(int.from_bytes(
                    report[OFF_SENSOR_TS:OFF_SENSOR_TS + 4], "little"))
        return len(stamps) > 2

    if timestamp_moves():
        return True
    print("   IMU idle — re-applying the feature enable")
    send(device, DISABLE_FEATURES)
    send(device, ENABLE_FEATURES)
    return timestamp_moves()


def show_raw(report):
    print(f"\nraw ({len(report)} bytes): {report.hex()}")
    accel, gyro = decode_motion(report)
    print(f"  [{OFF_COUNTER}:5]   counter      "
          f"{int.from_bytes(report[OFF_COUNTER:5], 'little')}")
    print(f"  [{OFF_BUTTONS}:9]   buttons      "
          f"0x{int.from_bytes(report[OFF_BUTTONS:9], 'little'):08x}")
    print(f"  [11:14] left stick   {unpack_pair(report[11:14])}")
    print(f"  [14:17] right stick  {unpack_pair(report[14:17])}")
    print(f"  [32:34] battery      "
          f"{int.from_bytes(report[OFF_BATTERY_MV:OFF_BATTERY_MV + 2], 'little')} mV")
    print(f"  [0x2b]  sensor ts    "
          f"{int.from_bytes(report[OFF_SENSOR_TS:OFF_SENSOR_TS + 4], 'little')}")
    print(f"  [0x31]  accel        "
          f"{accel[0]:+7.3f} {accel[1]:+7.3f} {accel[2]:+7.3f}  m/s^2  "
          f"(|a| = {sum(a * a for a in accel) ** 0.5:.3f})")
    print(f"  [0x37]  gyro         "
          f"{gyro[0]:+7.3f} {gyro[1]:+7.3f} {gyro[2]:+7.3f}  rad/s")


def main(args):
    device, handle = connect()
    if device is None:
        return 1
    try:
        if not ensure_imu_live(device, handle):
            print("⚠️  The sensor timestamp is not advancing — motion is idle.")
            print("   Unplug and replug the controller and try again.")

        if args.raw:
            report = read_report(handle)
            if report is None:
                print("❌ No report 0x05 arrived.")
                return 1
            show_raw(report)
            return 0

        print(f"\nReading for {args.seconds:.0f}s — move the controller.\n")
        peak = 0.0
        count = 0
        started = time.time()
        while time.time() - started < args.seconds:
            report = read_report(handle, 0.5)
            if report is None:
                continue
            count += 1
            accel, gyro = decode_motion(report)
            peak = max(peak, max(abs(v) for v in gyro))
            if count % 25 == 0:
                mag = sum(a * a for a in accel) ** 0.5
                print(f"  accel {accel[0]:+6.2f} {accel[1]:+6.2f} {accel[2]:+6.2f}"
                      f"  |a|={mag:5.2f}   "
                      f"gyro {gyro[0]:+6.2f} {gyro[1]:+6.2f} {gyro[2]:+6.2f}")
        rate = count / args.seconds if args.seconds else 0
        print(f"\n{count} reports ({rate:.0f} Hz), peak angular rate "
              f"{peak:.2f} rad/s ({peak * 57.2958:.0f} deg/s)")
        print("At rest |a| should sit near 9.81 and gyro near zero.")
        return 0
    finally:
        handle.close()
        usb.util.release_interface(device, COMMAND_INTERFACE)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", action="store_true",
                        help="print one report field by field and exit")
    parser.add_argument("--seconds", type=float, default=10.0,
                        help="how long to stream (default 10)")
    sys.exit(main(parser.parse_args()))
