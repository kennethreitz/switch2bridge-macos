#!/usr/bin/env python3
"""
Guided BLE report capture for the Switch 2 Pro Controller
=========================================================

Records raw input reports while walking you through a fixed script of
motions and button presses. The resulting JSONL is enough to work out the
report layout by hand: which byte offsets carry the IMU, which bits carry
the Switch 2-only buttons, and how far the sticks actually travel.

    python3 tools/capture_packets.py

Quit the Switch2 Bridge menubar app first — the controller only accepts one
BLE connection at a time.

Writes ./capture.jsonl (override with --out) plus a summary to stdout.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient, BleakScanner  # noqa: E402

INPUT_CHAR_UUID = "7492866c-ec3e-4619-8258-32755ffcc0f9"
COMMAND_CHAR_UUID = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"

# Selecting input report 0x09 is what makes the controller start sending the
# extended payload; without it bytes 12+ stay zero. Header offset 2 is the
# transport byte (0x01 = Bluetooth). Verified against real hardware.
ENABLE_MOTION_COMMANDS = [
    bytes([0x0C, 0x91, 0x01, 0x02, 0x00, 0x04, 0x00, 0x00, 0x27, 0, 0, 0]),
    bytes([0x0C, 0x91, 0x01, 0x04, 0x00, 0x04, 0x00, 0x00, 0x27, 0, 0, 0]),
    bytes([0x03, 0x91, 0x01, 0x0A, 0x00, 0x04, 0x00, 0x00, 0x09, 0, 0, 0]),
]
NINTENDO_COMPANY_IDS = (0x0553, 0x057E)
SWITCH2_PRO_PID_LE = b"\x69\x20"

# (phase id, seconds, instruction). "rest" must come first: it is the
# baseline every other phase is compared against.
SCRIPT = [
    ("rest", 6, "Put the controller FLAT ON THE TABLE and do not touch it."),
    ("pitch", 6, "Pick it up. Tilt it NOSE UP and NOSE DOWN, repeatedly."),
    ("yaw", 6, "Turn it LEFT and RIGHT (like a steering wheel lying flat)."),
    ("roll", 6, "Roll it CLOCKWISE and COUNTER-CLOCKWISE (like a steering wheel)."),
    ("shake", 5, "Shake it gently along each axis. Then set it down."),
    ("rest2", 4, "Flat on the table again, untouched."),
    ("btn_C", 4, "Hold the C button down for the whole phase."),
    ("btn_GL", 4, "Hold the LEFT grip button (GL) down."),
    ("btn_GR", 4, "Hold the RIGHT grip button (GR) down."),
    ("btn_home", 4, "Hold HOME down."),
    ("btn_capture", 4, "Hold CAPTURE (square button) down."),
    ("stick_L", 6, "Roll the LEFT stick around its full outer edge, slowly."),
    ("stick_R", 6, "Roll the RIGHT stick around its full outer edge, slowly."),
    ("triggers", 5, "Squeeze ZL and ZR fully, several times."),
    ("idle_end", 4, "Hands off. Flat on the table."),
]


async def find_controller(timeout=20.0):
    print(f"Scanning up to {timeout:.0f}s… hold the pair button on the back if needed.")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        devices = await BleakScanner.discover(timeout=5.0, return_adv=True)
        for address, (device, adv) in devices.items():
            for cid in NINTENDO_COMPANY_IDS:
                payload = adv.manufacturer_data.get(cid)
                if payload and SWITCH2_PRO_PID_LE in payload:
                    return address, device.name or "Switch 2 Pro Controller"
            if device.name and "Pro Controller" in device.name:
                return address, device.name
    return None, None


async def main(out_path, address=None, args_no_motion=False):
    if address is None:
        address, name = await find_controller()
        if not address:
            print("❌ Controller not found. Is it advertising? Is the menubar app quit?")
            return 1
        print(f"✅ Found {name} @ {address}")

    records = []
    state = {"phase": "warmup", "count": 0, "lengths": set()}

    def on_data(_sender, data: bytes):
        state["count"] += 1
        state["lengths"].add(len(data))
        records.append(
            {
                "t": round(time.monotonic(), 6),
                "phase": state["phase"],
                "hex": bytes(data).hex(),
            }
        )

    client = BleakClient(address, timeout=20.0)
    await client.connect()
    if not client.is_connected:
        print("❌ Could not connect.")
        return 1
    print("✅ Connected. Starting capture.\n")
    await client.start_notify(INPUT_CHAR_UUID, on_data)

    if not args_no_motion:
        for command in ENABLE_MOTION_COMMANDS:
            try:
                await client.write_gatt_char(COMMAND_CHAR_UUID, command, response=False)
                await asyncio.sleep(0.25)
            except Exception as e:
                print(f"  (motion enable failed: {e})")
        print("✅ Requested extended input report 0x09 (motion payload)")

    try:
        await asyncio.sleep(1.0)  # let notifications settle
        for phase, seconds, instruction in SCRIPT:
            state["phase"] = phase
            print(f"\n=== {phase} ({seconds}s) ===\n>>> {instruction}")
            for remaining in range(seconds, 0, -1):
                print(f"    {remaining}…", end="\r", flush=True)
                await asyncio.sleep(1.0)
            print(f"    done ({state['count']} packets so far)      ")
    finally:
        try:
            await client.stop_notify(INPUT_CHAR_UUID)
        except Exception:
            pass
        await client.disconnect()

    Path(out_path).write_text("\n".join(json.dumps(r) for r in records) + "\n")

    print(f"\n📦 Wrote {len(records)} packets to {out_path}")
    print(f"   Report length(s) seen: {sorted(state['lengths'])}")
    if records:
        print(f"   First report: {records[0]['hex']}")
    print("\nSend that file (or just this output) back to finish the decode.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="capture.jsonl")
    ap.add_argument("--address", default=None, help="skip scanning, connect directly")
    ap.add_argument("--no-motion", action="store_true",
                    help="do not request the extended report")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.out, args.address, args.no_motion)))
