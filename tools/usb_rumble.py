#!/usr/bin/env python3
"""
Drive the wired controller's HD rumble
======================================

Rumble is the mirror image of the command channel. The init commands only work
on the vendor interface and are silently ignored as HID output reports; rumble
only works as a HID output report and is *acknowledged* but ignored when sent
as command 0x0A/0x08 on the vendor interface. That ACK is what makes the wrong
path convincing, so this exists to test the right one against real hardware.

    python3 tools/usb_rumble.py              # a short demo sweep
    python3 tools/usb_rumble.py --low 40000  # hold one amplitude
    python3 tools/usb_rumble.py --seconds 3

Quit "Switch2 Bridge.app" first — it holds the command interface.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import usb_transport  # noqa: E402


def hold(transport, low, high, seconds, label):
    print(f"  {label:<28} low={low:5d} high={high:5d}  {seconds:.1f}s")
    transport.set_rumble(low, high)
    time.sleep(seconds)


def main(args):
    if not usb_transport.dependencies_available():
        print("Wired mode needs pyusb and hidapi:\n"
              "    pip install pyusb hidapi\n"
              "    brew install libusb")
        return 1
    if not usb_transport.is_connected():
        print("❌ No Switch 2 Pro Controller on USB. Use a data cable — "
              "charge-only cables do not enumerate.")
        return 1

    transport = usb_transport.USBTransport(lambda _report: None)
    if not transport.connect():
        print(f"❌ {transport.last_error}")
        return 1
    print("Connected. The controller should buzz.\n")

    try:
        if args.low is not None or args.high is not None:
            hold(transport, args.low or 0, args.high or 0, args.seconds,
                 "holding")
        else:
            hold(transport, 0, 45000, 1.2, "high frequency only")
            hold(transport, 0, 0, 0.4, "off")
            hold(transport, 45000, 0, 1.2, "low frequency only")
            hold(transport, 0, 0, 0.4, "off")
            hold(transport, 0xFFFF, 0xFFFF, 1.2, "both, full scale")
            hold(transport, 0, 0, 0.4, "off")
            for step in range(1, 6):
                amp = int(0xFFFF * step / 5)
                hold(transport, amp, amp, 0.35, f"ramp {step}/5")
    finally:
        # disconnect() silences the motors, but be explicit: leaving the pad
        # buzzing because a demo raised would be a poor trade.
        transport.set_rumble(0, 0)
        time.sleep(0.1)
        transport.disconnect()
    print("\nDone. Motors stopped.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--low", type=int, default=None,
                        help=f"low-frequency amplitude, 0-{0xFFFF}")
    parser.add_argument("--high", type=int, default=None,
                        help=f"high-frequency amplitude, 0-{0xFFFF}")
    parser.add_argument("--seconds", type=float, default=2.0,
                        help="how long to hold --low/--high (default 2)")
    sys.exit(main(parser.parse_args()))
