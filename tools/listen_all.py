#!/usr/bin/env python3
"""
Subscribe to every notify characteristic at once
================================================

The bridge listens to one characteristic out of seven. Before concluding
that data (motion, battery, analog triggers) is absent, we should check
whether it is simply arriving on a channel nobody subscribed to.

This is a read-only experiment: it writes nothing to the controller.

    python3 tools/listen_all.py --address <uuid>

Quit the Switch2 Bridge menubar app first.
"""

import argparse
import asyncio
import collections
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient, BleakScanner  # noqa: E402

NINTENDO_COMPANY_IDS = (0x0553, 0x057E)
SWITCH2_PRO_PID_LE = b"\x69\x20"
INPUT_CHAR = "7492866c-ec3e-4619-8258-32755ffcc0f9"

PHASES = [
    ("rest", 5, "Controller FLAT ON THE TABLE, untouched."),
    ("motion", 8, "Pick it up. Rotate it through pitch, yaw and roll."),
    ("buttons", 5, "Press several buttons and both triggers."),
    ("sticks", 5, "Roll both sticks around their edges."),
    ("rest2", 4, "Flat on the table again, untouched."),
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


async def main(address):
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

    # uuid -> phase -> list of payloads
    data = collections.defaultdict(lambda: collections.defaultdict(list))
    state = {"phase": "warmup"}

    def make_handler(uuid):
        def handler(_sender, payload):
            data[uuid][state["phase"]].append(bytes(payload))
        return handler

    subscribed, failed = [], []
    for service in client.services:
        for char in service.characteristics:
            if "notify" not in char.properties and "indicate" not in char.properties:
                continue
            try:
                await client.start_notify(char, make_handler(char.uuid))
                subscribed.append(char.uuid)
            except Exception as e:
                failed.append((char.uuid, str(e)))

    print(f"Subscribed to {len(subscribed)} channel(s):")
    for uuid in subscribed:
        print(f"   {uuid}{'  (known input)' if uuid.lower() == INPUT_CHAR else ''}")
    for uuid, err in failed:
        print(f"   ✗ {uuid}: {err}")
    print()

    try:
        for phase, seconds, instruction in PHASES:
            state["phase"] = phase
            print(f"=== {phase} ({seconds}s) ===\n>>> {instruction}")
            for remaining in range(seconds, 0, -1):
                print(f"    {remaining}… ", end="\r", flush=True)
                await asyncio.sleep(1.0)
            total = sum(len(v) for v in data.values() for v in v.values())
            print(f"    done ({total} packets total)      ")
    finally:
        for uuid in subscribed:
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass
        await client.disconnect()

    # ---------- report ----------
    print("\n" + "=" * 72)
    print("PER-CHANNEL ACTIVITY")
    print("=" * 72)

    silent = []
    for uuid in subscribed:
        phases = data[uuid]
        total = sum(len(v) for v in phases.values())
        if total == 0:
            silent.append(uuid)
            continue
        lengths = sorted({len(p) for v in phases.values() for p in v})
        tag = "  ← the channel we already use" if uuid.lower() == INPUT_CHAR else "  ← NEW"
        print(f"\n{uuid}{tag}")
        print(f"   packets={total}  length(s)={lengths}")
        print("   " + "  ".join(f"{ph}:{len(v)}" for ph, v in phases.items()))

        sample = next(p for v in phases.values() if v for p in v)
        print(f"   sample: {sample.hex()[:96]}{'…' if len(sample) > 48 else ''}")

        # which bytes move, and do they move specifically during motion?
        width = min(lengths[0], 64)
        rest = phases.get("rest", []) + phases.get("rest2", [])
        movers = []
        for off in range(width):
            def sd(pkts):
                vals = [p[off] for p in pkts if len(p) > off]
                return statistics.pstdev(vals) if len(vals) > 1 else 0.0
            rest_sd = sd(rest)
            motion_sd = sd(phases.get("motion", []))
            if motion_sd > 2.0 and motion_sd > rest_sd * 3:
                movers.append(off)
        if movers:
            print(f"   ⚡ bytes active during MOTION but quiet at rest: {movers}")

    if silent:
        print("\nSilent channels (subscribed, never fired):")
        for uuid in silent:
            print(f"   {uuid}")

    print("\n" + "=" * 72)
    interesting = [u for u in subscribed
                   if u.lower() != INPUT_CHAR and sum(len(v) for v in data[u].values())]
    if interesting:
        print("VERDICT: additional channels are streaming data:")
        for uuid in interesting:
            print(f"   {uuid}")
        print("These were never subscribed to by the bridge.")
    else:
        print("VERDICT: only the known input characteristic streams.")
        print("Other data likely needs a command written first.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default=None)
    sys.exit(asyncio.run(main(ap.parse_args().address)))
