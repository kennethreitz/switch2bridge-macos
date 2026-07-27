#!/usr/bin/env python3
"""
Try to switch the IMU on over BLE
=================================

Bytes 12+ of the input report are zero on a freshly connected controller, so
motion is disabled rather than missing. The USB initialisation docs describe
command 0x0C: arg 0x02 configures the IMU, arg 0x04 is where "motion control
data is enabled and being sent".

Those commands use the same frame the BLE command channel already accepts, so
they are worth trying over BLE. Handheld Legend's motion doc places Accel Z at
bytes 40-41 as INT16 LE, which puts the IMU block around bytes 36-47.

    python3 tools/enable_motion.py --address <uuid>

Quit the Switch2 Bridge menubar app first.
"""

import argparse
import asyncio
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import controller_commands as cc  # noqa: E402
from bleak import BleakClient, BleakScanner  # noqa: E402

NINTENDO_COMPANY_IDS = (0x0553, 0x057E)
SWITCH2_PRO_PID_LE = b"\x69\x20"
INPUT_CHAR = "7492866c-ec3e-4619-8258-32755ffcc0f9"
# Motion very likely arrives on a different report than the one the bridge
# reads. Format 2 is documented as matching USB report 0x0A, and format 1 was
# already observed carrying non-zero data past byte 12.
INPUT_CHANNELS = {
    "7492866c-ec3e-4619-8258-32755ffcc0f9": "input f9 (bridge uses this)",
    "7492866c-ec3e-4619-8258-32755ffcc0f8": "input f8 (format 2 = USB report 0x0A)",
    "ab7de9be-89fe-49ad-828f-118f09df7fd2": "input fd2 (format 1)",
    "ab7de9be-89fe-49ad-828f-118f09df7fde": "input fde (unknown)",
}

# Verified command bytes from TommyWabg/Switch2Connect (GPL-3.0),
# src/usb_hid_controller.py, which has wired Pro Controller 2 working.
# Header offset 2 is a TRANSPORT byte: 0x00 = USB, 0x01 = Bluetooth.
# Earlier attempts here guessed the 0x0C payload as 0x01; it is 0x27.
def cmd(report_type, command, payload=b"", transport=0x01, length=None):
    head = bytes([report_type, 0x91, transport, command, 0x00,
                  length if length is not None else len(payload), 0x00, 0x00])
    return head + payload


FEATURE_PAYLOAD = bytes([0x27, 0x00, 0x00, 0x00])

ATTEMPTS = [
    ("feature-mask BT",
     cmd(0x0C, 0x02, FEATURE_PAYLOAD, transport=0x01, length=0x04),
     "0x0C/0x02 set feature mask 0x27, Bluetooth transport byte"),
    ("enable-feat BT",
     cmd(0x0C, 0x04, FEATURE_PAYLOAD, transport=0x01, length=0x04),
     "0x0C/0x04 enable features — payload 0x27, not 0x01 as guessed before"),
    ("select-rpt09 BT",
     cmd(0x03, 0x0A, bytes([0x09, 0x00, 0x00, 0x00]), transport=0x01, length=0x04),
     "0x03/0x0A select input report 0x09 (the Pro 2 report carrying motion)"),
    ("feature-mask USB",
     cmd(0x0C, 0x02, FEATURE_PAYLOAD, transport=0x00, length=0x04),
     "same, USB transport byte — the LED command worked with 0x00, so try both"),
    ("enable-feat USB",
     cmd(0x0C, 0x04, FEATURE_PAYLOAD, transport=0x00, length=0x04),
     "0x0C/0x04 with the USB transport byte"),
    ("select-rpt05 USB",
     cmd(0x03, 0x0A, bytes([0x05, 0x00, 0x00, 0x00]), transport=0x00, length=0x04),
     "select the common report 0x05, exactly as Switch2Connect sends it"),
]


async def find(timeout=20.0):
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for address, (device, adv) in devices.items():
        for cid in NINTENDO_COMPANY_IDS:
            payload = adv.manufacturer_data.get(cid)
            if payload and SWITCH2_PRO_PID_LE in payload:
                return address
        if device.name and "Pro Controller" in device.name:
            return address
    return None


def describe_tail(sample):
    """Decode the suspected IMU block so a hit is immediately readable."""
    if len(sample) < 48:
        return ""
    accel = struct.unpack_from("<3h", sample, 36)
    gyro = struct.unpack_from("<3h", sample, 42)
    return f"accel(36..41)={accel}  gyro(42..47)={gyro}"


async def main(args):
    address = args.address or await find()
    if not address:
        print("❌ Controller not found.")
        return 1

    client = BleakClient(address, timeout=20.0)
    await client.connect()
    print(f"✅ Connected to {address}\n")

    stats = {u: {"count": 0, "nonzero": 0, "sample": None} for u in INPUT_CHANNELS}
    responses = []

    def make_input_handler(uuid):
        def handler(_s, payload):
            payload = bytes(payload)
            st = stats[uuid]
            st["count"] += 1
            if len(payload) > 12 and any(payload[12:]):
                st["nonzero"] += 1
                if st["sample"] is None:
                    st["sample"] = payload
        return handler

    def on_reply(_s, payload):
        responses.append(bytes(payload))

    for uuid in INPUT_CHANNELS:
        try:
            await client.start_notify(uuid, make_input_handler(uuid))
        except Exception as e:
            print(f"   ✗ subscribe {uuid}: {e}")
    await client.start_notify(cc.RESPONSE_CHAR, on_reply)
    await asyncio.sleep(1.5)

    print("baseline:")
    for uuid, label in INPUT_CHANNELS.items():
        st = stats[uuid]
        print(f"   {label:<38} {st['count']:4d} reports, "
              f"{st['nonzero']} with tail data")
    print()

    hit = None
    for label, payload, why in ATTEMPTS:
        before = {u: stats[u]["nonzero"] for u in INPUT_CHANNELS}
        responses.clear()
        try:
            await client.write_gatt_char(cc.COMMAND_CHAR, payload, response=False)
            status = "sent"
        except Exception as e:
            status = f"rejected: {e}"
        await asyncio.sleep(1.8)
        gained = {u: stats[u]["nonzero"] - before[u] for u in INPUT_CHANNELS}
        total = sum(gained.values())
        note = f"  ⚡ tail data on {sum(1 for v in gained.values() if v)} channel(s)" if total else ""
        if responses:
            note += f"  reply={responses[0].hex()[:28]}"
        print(f"   {label:<20} {payload.hex()[:32]:<34} {status}{note}")
        for u, n in gained.items():
            if n:
                print(f"   {'':<20} -> {INPUT_CHANNELS[u]}: {n} reports")
        if total and hit is None:
            hit = (label, payload)

    # The controller can drop the link mid-run, which invalidates service
    # discovery; cleanup must never mask the results we came for.
    for uuid in list(INPUT_CHANNELS) + [cc.RESPONSE_CHAR]:
        try:
            await client.stop_notify(uuid)
        except Exception:
            pass
    try:
        await client.disconnect()
    except Exception:
        pass

    print("\n" + "=" * 68)
    live = {u: st for u, st in stats.items() if st["sample"] is not None}
    print("per-channel totals:")
    for uuid, label in INPUT_CHANNELS.items():
        st = stats[uuid]
        print(f"   {label:<38} {st['count']:5d} reports, "
              f"{st['nonzero']} with tail data")
    print()
    if live:
        print("✅ TAIL DATA PRESENT")
        if hit:
            print(f"   first appeared after: {hit[0]}  ({hit[1].hex()})")
        for uuid, st in live.items():
            print(f"\n   {INPUT_CHANNELS[uuid]}  (len={len(st['sample'])})")
            print(f"   {st['sample'].hex()}")
            print(f"   {describe_tail(st['sample'])}")
        print("\n   Re-run while moving the controller: if those numbers track"
              "\n   rotation, that is the IMU and we have the offsets.")
    else:
        print("❌ No motion data. Report bytes 12+ stayed zero throughout.")
        print("   The IMU likely needs the full USB-style init sequence, not")
        print("   just command 0x0C, or a different arg. Not a dead end, but")
        print("   it needs the complete handshake to be worked out first.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default=None)
    sys.exit(asyncio.run(main(ap.parse_args())))
