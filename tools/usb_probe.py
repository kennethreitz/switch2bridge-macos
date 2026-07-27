#!/usr/bin/env python3
"""
Bring the Switch 2 Pro Controller up over USB
=============================================

Plugged in, the controller enumerates as a HID device but sends nothing until
it receives an initialisation handshake. This walks the documented sequence
one stage at a time and reports what changes after each, so a partial success
is still informative.

Init order (docs.handheldlegend.com, via BlueRetro issue #1249):
    0x03 handshake -> 0x07 -> 0x16 -> 0x15 x3 -> 0x09 -> 0x0C(0x02)
    -> SPI reads -> 0x0A -> 0x0C(0x04) enables motion

Commands share the same frame as the BLE command channel:
    <report_type> 0x91 0x00 <command> 0x00 <len> 0x00 0x00 <payload>

    python3 tools/usb_probe.py            # dry run, prints the plan
    python3 tools/usb_probe.py --go       # actually send
"""

import argparse
import sys
import time

try:
    import hid
except ImportError:
    print("❌ hidapi not installed — run: pip install hidapi")
    sys.exit(1)

VENDOR_ID = 0x057E
PRODUCT_ID = 0x2069

# Stand-in for the console's Bluetooth address. The controller stores whatever
# host claims it; a locally-administered address keeps it obviously synthetic.
# Switch2Connect sends all-0xFF here rather than a real address.
HOST_MAC = bytes([0xFF] * 6)

# Verified against TommyWabg/Switch2Connect src/usb_hid_controller.py:
# commands go to interface 1, endpoint 0x02 over raw USB. The HID output
# report path below is only a fallback and does not work on macOS.
USB_COMMAND_INTERFACE = 1
USB_COMMAND_ENDPOINT_OUT = 0x02
# Output report body size for the HID fallback (0x2A, not the 63 the report
# descriptor implies).
PRO2_OUTPUT_BODY = 0x2A


def frame(report_type, command, payload=b"", length=None):
    body = bytes([report_type, 0x91, 0x00, command, 0x00,
                  length if length is not None else len(payload), 0x00, 0x00])
    return body + payload


# (name, frame, description)
def build_stages():
    return [
        ("handshake",
         frame(0x03, 0x0D, bytes([0x01, 0x00]) + HOST_MAC, length=0x08),
         "0x03 cmd 0x0D — claims the controller for this host and, per the "
         "docs, starts HID reporting at 4ms intervals"),
        ("status-07", frame(0x07, 0x01, b"", length=0x00),
         "0x07 status check"),
        ("status-16", frame(0x16, 0x01, b"", length=0x00),
         "0x16 status check"),
        ("report-0A", frame(0x0A, 0x01, b"", length=0x00),
         "0x0A — the full input report format the BLE side calls 'format 2'"),
        ("imu-config", frame(0x0C, 0x02, bytes([0x27, 0x00, 0x00, 0x00]), length=0x04),
         "0x0C arg 0x02 — IMU configuration (docs say no ACK is returned)"),
        ("imu-enable", frame(0x0C, 0x04, bytes([0x01, 0x00, 0x00, 0x00]), length=0x04),
         "0x0C arg 0x04 — documented as the point where motion data starts "
         "flowing. This is the command the BLE path never found"),
        ("player-led", frame(0x09, 0x07, bytes([0x01]), length=0x08),
         "0x09 cmd 0x07 — player LED 1, a visible success signal"),
    ]


def try_libusb():
    """The path that actually works upstream: raw USB to interface 1."""
    try:
        import usb.core
        import usb.util
    except ImportError:
        print("pyusb not installed — run: pip install pyusb  (and brew install libusb)")
        return False

    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        print(f"❌ libusb cannot see {VENDOR_ID:#06x}:{PRODUCT_ID:#06x}.")
        seen = list(usb.core.find(find_all=True))
        print(f"   {len(seen)} device(s) visible to libusb:")
        for d in seen:
            print(f"     {d.idVendor:#06x}:{d.idProduct:#06x}")
        return False

    print(f"✅ libusb sees {VENDOR_ID:#06x}:{PRODUCT_ID:#06x}\n")
    for cfg in dev:
        for intf in cfg:
            print(f"  interface {intf.bInterfaceNumber} alt {intf.bAlternateSetting} "
                  f"class={intf.bInterfaceClass:#04x}")
            for ep in intf:
                import usb.util as u
                direction = ("IN " if u.endpoint_direction(ep.bEndpointAddress) == u.ENDPOINT_IN
                             else "OUT")
                print(f"     ep {ep.bEndpointAddress:#04x} {direction} "
                      f"type={u.endpoint_type(ep.bmAttributes)} "
                      f"maxpkt={ep.wMaxPacketSize}")
    return True


def open_device():
    devices = hid.enumerate(VENDOR_ID, PRODUCT_ID)
    if not devices:
        print("❌ No Switch 2 Pro Controller on USB. Plug it in with a data "
              "cable (charge-only cables will not enumerate).")
        return None
    handle = hid.device()
    handle.open_path(devices[0]["path"])
    handle.set_nonblocking(True)
    return handle


def drain(handle, seconds, bucket=None):
    """Collect reports for a while. Returns the list."""
    got = []
    end = time.time() + seconds
    while time.time() < end:
        data = handle.read(128)
        if data:
            got.append(bytes(data))
            if bucket is not None:
                bucket.append(bytes(data))
        else:
            time.sleep(0.002)
    return got


def summarise(reports, label):
    if not reports:
        print(f"      {label}: nothing")
        return
    ids = sorted({r[0] for r in reports})
    lengths = sorted({len(r) for r in reports})
    print(f"      {label}: {len(reports)} reports, ids={[hex(i) for i in ids]}, "
          f"len={lengths}")
    print(f"         first: {reports[0].hex()[:100]}")


def main(args):
    stages = build_stages()
    if not args.go:
        print("PLAN — nothing is sent in dry-run mode\n")
        print(f"Target: USB HID {VENDOR_ID:#06x}:{PRODUCT_ID:#06x}")
        print(f"Host MAC claimed: {HOST_MAC.hex(':')}\n")
        for name, payload, why in stages:
            print(f"   {name:<12} {payload.hex()}")
            print(f"   {'':<12} {why}\n")
        print("After each stage the tool reads for 1.5s and reports what "
              "arrived.\nRe-run with --go to send.")
        return 0

    handle = open_device()
    if handle is None:
        return 1
    print(f"✅ Opened {handle.get_manufacturer_string()} "
          f"{handle.get_product_string()}\n")

    baseline = drain(handle, 2.0)
    summarise(baseline, "before any command")
    print()

    all_reports = []
    for name, payload, _why in stages:
        wire = payload
        if args.pad:
            wire = payload.ljust(args.pad, b"\x00")
        try:
            written = handle.write(wire)
            status = f"wrote {written}B"
        except Exception as e:
            status = f"write failed: {e}"
        print(f"   {name:<12} {payload.hex()[:40]:<42} {status}")
        got = drain(handle, 1.5, all_reports)
        summarise(got, "after")

    print("\n" + "=" * 66)
    if all_reports:
        ids = sorted({r[0] for r in all_reports})
        lengths = sorted({len(r) for r in all_reports})
        print(f"✅ {len(all_reports)} reports total. "
              f"ids={[hex(i) for i in ids]} lengths={lengths}")
        # Look for a report that has data past the button/stick block, which
        # is where motion would appear
        with_tail = [r for r in all_reports if len(r) > 16 and any(r[12:])]
        if with_tail:
            print(f"\n⚡ {len(with_tail)} report(s) carry data past byte 12 "
                  f"— candidate motion payload:")
            print(f"   {with_tail[0].hex()[:140]}")
        print("\nSample of distinct reports:")
        seen = set()
        for r in all_reports:
            key = (r[0], len(r))
            if key in seen:
                continue
            seen.add(key)
            print(f"   id={r[0]:#04x} len={len(r):3d}  {r.hex()[:110]}")
    else:
        print("❌ Still silent. The handshake framing is likely wrong for USB —")
        print("   try --pad 64 to pad output reports to the endpoint size.")
    handle.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="actually send")
    ap.add_argument("--pad", type=int, default=PRO2_OUTPUT_BODY,
                    help="pad each output report body to N bytes")
    ap.add_argument("--libusb", action="store_true",
                    help="enumerate via libusb and show interface 1 endpoints")
    parsed = ap.parse_args()
    if parsed.libusb:
        sys.exit(0 if try_libusb() else 1)
    sys.exit(main(parsed))
