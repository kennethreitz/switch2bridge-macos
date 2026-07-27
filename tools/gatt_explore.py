#!/usr/bin/env python3
"""
Enumerate the controller's GATT tree
====================================

The bridge currently connects and only subscribes to notifications — it
never writes anything, so the controller is never told it has been claimed
by a host. That is the suspected cause of three separate defects: pairing
mode never ends, the LEDs never respond, and the IMU never starts.

Fixing any of them needs to start from what the device actually exposes, so
this dumps every service, characteristic, property and descriptor, reads
everything readable, and lists the write targets worth probing.

    python3 tools/gatt_explore.py --address <uuid>

Quit the Switch2 Bridge menubar app first.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient, BleakScanner  # noqa: E402

NINTENDO_COMPANY_IDS = (0x0553, 0x057E)
SWITCH2_PRO_PID_LE = b"\x69\x20"
INPUT_CHAR_UUID = "7492866c-ec3e-4619-8258-32755ffcc0f9"

# Characteristics we already know about, so anything new stands out
KNOWN = {
    INPUT_CHAR_UUID: "input reports (currently subscribed)",
    "7492866c-ec3e-4619-8258-32755ffcc0f8": "suspected output (README: 'not working')",
}


def describe(props):
    return ", ".join(props) if props else "(none)"


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


async def main(address, do_read):
    if address is None:
        print("Scanning…")
        address = await find()
        if not address:
            print("❌ Controller not found.")
            return 1

    client = BleakClient(address, timeout=20.0)
    await client.connect()
    if not client.is_connected:
        print("❌ Could not connect.")
        return 1
    print(f"✅ Connected to {address}\n")

    writable = []
    notifiable = []

    for service in client.services:
        print(f"── service {service.uuid}")
        if service.description and service.description != "Unknown":
            print(f"   {service.description}")
        for char in service.characteristics:
            note = KNOWN.get(char.uuid.lower(), "")
            marker = "  ← " + note if note else ""
            print(f"   • char {char.uuid}  [{describe(char.properties)}]{marker}")

            props = set(char.properties)
            if props & {"write", "write-without-response"}:
                writable.append((char, props))
            if props & {"notify", "indicate"}:
                notifiable.append(char)

            if do_read and "read" in props:
                try:
                    value = await client.read_gatt_char(char)
                    printable = value.decode("utf-8", "replace").strip()
                    printable = "".join(c if c.isprintable() else "." for c in printable)
                    print(f"       read: {value.hex()}  {printable!r}")
                except Exception as e:
                    print(f"       read failed: {e}")

            for desc in char.descriptors:
                print(f"       descriptor {desc.uuid}")

    print("\n" + "=" * 60)
    print("WRITE TARGETS (candidates for the handshake / LED / IMU commands)")
    if writable:
        for char, props in writable:
            kind = "no-response" if "write-without-response" in props else "with-response"
            print(f"  {char.uuid}  [{kind}]  max_write={getattr(char, 'max_write_without_response_size', '?')}")
    else:
        print("  none — the controller exposes no writable characteristic")

    print("\nNOTIFY SOURCES (where a command reply would arrive)")
    for char in notifiable:
        tag = " (input reports)" if char.uuid.lower() == INPUT_CHAR_UUID else ""
        print(f"  {char.uuid}{tag}")

    await client.disconnect()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default=None)
    ap.add_argument("--no-read", action="store_true",
                    help="skip reading readable characteristics")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.address, not args.no_read)))
