#!/usr/bin/env python3
"""
Live button-bit viewer
======================

Prints which report bits are set, as you press. Use it to confirm a bit
assignment in seconds instead of inferring it from a timed capture.

    python3 tools/live_buttons.py --address <uuid>

Quit the Switch2 Bridge menubar app first.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient, BleakScanner  # noqa: E402

INPUT_CHAR_UUID = "7492866c-ec3e-4619-8258-32755ffcc0f9"
NINTENDO_COMPANY_IDS = (0x0553, 0x057E)
SWITCH2_PRO_PID_LE = b"\x69\x20"

# What we currently believe each bit is, so mislabels are obvious on screen
KNOWN = {
    2: {0x01: "B", 0x02: "A", 0x04: "Y", 0x08: "X",
        0x10: "R", 0x20: "ZR", 0x40: "+", 0x80: "RS"},
    3: {0x01: "DDOWN", 0x02: "DRIGHT", 0x04: "DLEFT", 0x08: "DUP",
        0x10: "L", 0x20: "ZL", 0x40: "-", 0x80: "LS"},
    4: {0x01: "HOME", 0x02: "CAPT?", 0x04: "GR", 0x08: "GL", 0x10: "C?"},
}


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


async def main(address):
    if address is None:
        print("Scanning…")
        address = await find()
        if not address:
            print("❌ Controller not found.")
            return 1

    last = [None]

    def on_data(_s, data: bytes):
        if len(data) < 5:
            return
        key = (data[2], data[3], data[4])
        if key == last[0]:
            return
        last[0] = key
        parts = []
        for off in (2, 3, 4):
            for bit in (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80):
                if data[off] & bit:
                    parts.append(f"b{off}:{bit:#04x}={KNOWN.get(off, {}).get(bit, '???')}")
        line = "  ".join(parts) if parts else "(nothing held)"
        print(f"\r{' ' * 100}\r{line}", flush=True)

    client = BleakClient(address, timeout=20.0)
    await client.connect()
    await client.start_notify(INPUT_CHAR_UUID, on_data)
    print("Connected. Press buttons — Ctrl-C to stop.\n")
    print(">>> Press and HOLD the C button, then the LEFT grip (GL), one at a time.\n")
    try:
        while True:
            await asyncio.sleep(0.5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        try:
            await client.stop_notify(INPUT_CHAR_UUID)
        except Exception:
            pass
        await client.disconnect()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default=None)
    try:
        sys.exit(asyncio.run(main(ap.parse_args().address)))
    except KeyboardInterrupt:
        pass
