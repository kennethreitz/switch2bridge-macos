#!/usr/bin/env python3
"""
Replay the Switch 2 console's own BLE init sequence
===================================================

Extracted from darthcloud's decrypted OTA capture of a real Switch 2
connecting to a Pro Controller 2 (BlueRetro issue #1249,
sw2_pro2_reconn_sc2_rumble_crackle.pcap).

This is what the console actually sends, in order, over the command
characteristic. It includes several commands this project has never sent -
0x07, 0x10, 0x16, 0x11 (twice) and a report config of type 0x0A - and reads
input from a different characteristic than we do.

Measures, before and after:
  * report rate on each input channel, to see whether anything changes the
    30 ms connection interval macOS negotiates
  * whether bytes past the button/stick block become non-zero (motion)

    python3 tools/replay_console_init.py            # dry run, prints the plan
    python3 tools/replay_console_init.py --go

Quit the Switch2 Bridge menubar app first.
"""

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import controller_commands as cc  # noqa: E402
from bleak import BleakClient, BleakScanner  # noqa: E402

NINTENDO_COMPANY_IDS = (0x0553, 0x057E)
SWITCH2_PRO_PID_LE = b"\x69\x20"

# The console reads input here (ATT handle 0x000e); we normally use ...f9.
INPUT_F8 = "7492866c-ec3e-4619-8258-32755ffcc0f8"
INPUT_F9 = "7492866c-ec3e-4619-8258-32755ffcc0f9"
INPUT_FD2 = "ab7de9be-89fe-49ad-828f-118f09df7fd2"
CHANNELS = {
    INPUT_F9: "f9  (what the bridge reads)",
    INPUT_F8: "f8  (what the CONSOLE reads)",
    INPUT_FD2: "fd2 (format 1)",
}

# Verbatim from the decrypted console trace, in order.
CONSOLE_INIT = [
    ("status 0x07", "0791010100000000"),
    ("spi 0x00013000", "0291010400080000407e000000300100"),
    ("unknown 0x10", "1091010100000000"),
    ("unknown 0x16", "1691010100000000"),
    ("report config 0x0A/0x02", "0a9101020004000003000000"),
    ("player led", "09910107000800000100000000000000"),
    ("feature mask 0x27", "0c9101020004000027000000"),
    ("spi 0x00013080", "0291010400080000407e000080300100"),
    ("spi 0x000130c0", "0291010400080000407e0000c0300100"),
    ("spi 0x001fc040", "0291010400080000407e000040c01f00"),
    ("spi 0x00013040", "0291010400080000107e000040300100"),
    ("spi 0x00013100", "0291010400080000187e000000310100"),
    ("unknown 0x11/0x03", "1191010300000000"),
    ("spi 0x00013060", "0291010400080000207e000060300100"),
    ("vibration config", "0a9101080014000001ffffffffffffff"),
    ("unknown 0x11/0x01", "1191010100000000"),
    ("enable features 0x27", "0c9101040004000027000000"),
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


class Meter:
    def __init__(self):
        self.stamps = []
        self.tail = 0
        self.sample = None

    def add(self, payload):
        self.stamps.append(time.monotonic())
        if len(payload) > 12 and any(payload[12:]):
            self.tail += 1
            if self.sample is None:
                self.sample = payload

    def rate(self):
        if len(self.stamps) < 3:
            return 0.0, 0.0
        gaps = [(b - a) * 1000 for a, b in zip(self.stamps, self.stamps[1:])]
        gaps = [g for g in gaps if 0 < g < 500]
        if not gaps:
            return 0.0, 0.0
        span = self.stamps[-1] - self.stamps[0]
        return len(self.stamps) / span if span else 0.0, statistics.median(gaps)

    def reset(self):
        self.stamps.clear()


def report(meters, label):
    print(f"\n   {label}")
    for uuid, name in CHANNELS.items():
        hz, gap = meters[uuid].rate()
        tail = meters[uuid].tail
        if not meters[uuid].stamps:
            print(f"      {name:<28} silent")
        else:
            print(f"      {name:<28} {hz:6.1f} Hz   p50 gap {gap:5.1f} ms"
                  f"   tail-data {tail}")


async def main(args):
    if not args.go:
        print("PLAN — nothing is sent in dry-run mode\n")
        print("Replays the console's own init sequence, in order:\n")
        for name, hexs in CONSOLE_INIT:
            print(f"   {name:<26} {hexs}")
        print("\nRate is measured on all three input characteristics before "
              "and after.\nRe-run with --go to send.")
        return 0

    address = args.address or await find()
    if not address:
        print("❌ Controller not found.")
        return 1

    client = BleakClient(address, timeout=20.0)
    await client.connect()
    print(f"✅ Connected to {address}")

    meters = {uuid: Meter() for uuid in CHANNELS}

    def handler(uuid):
        def cb(_s, payload):
            meters[uuid].add(bytes(payload))
        return cb

    for uuid in CHANNELS:
        try:
            await client.start_notify(uuid, handler(uuid))
        except Exception as e:
            print(f"   ✗ subscribe {uuid}: {e}")

    replies = []
    try:
        await client.start_notify(cc.RESPONSE_CHAR,
                                  lambda _s, p: replies.append(bytes(p)))
    except Exception:
        pass

    await asyncio.sleep(5.0)
    report(meters, "BEFORE — baseline")

    print("\n   sending the console sequence…")
    acked = 0
    for name, hexs in CONSOLE_INIT:
        replies.clear()
        try:
            await client.write_gatt_char(cc.COMMAND_CHAR, bytes.fromhex(hexs),
                                         response=False)
        except Exception as e:
            print(f"      {name:<26} write failed: {e}")
            continue
        await asyncio.sleep(0.12)
        if replies:
            acked += 1
        print(f"      {name:<26} {'ack ' + replies[0].hex()[:24] if replies else 'no reply'}")
    print(f"   {acked}/{len(CONSOLE_INIT)} acknowledged")

    for m in meters.values():
        m.reset()
    await asyncio.sleep(6.0)
    report(meters, "AFTER — post console init")

    for uuid in list(CHANNELS) + [cc.RESPONSE_CHAR]:
        try:
            await client.stop_notify(uuid)
        except Exception:
            pass
    try:
        await client.disconnect()
    except Exception:
        pass

    print("\n" + "=" * 68)
    best = max((meters[u].rate()[0] for u in CHANNELS), default=0)
    if best > 45:
        print(f"⚡ RATE CHANGED — {best:.0f} Hz. The connection interval moved.")
    else:
        print(f"Rate unchanged (~{best:.0f} Hz). The interval is still whatever")
        print("macOS negotiated; no command in the console's sequence alters it.")
    for uuid, name in CHANNELS.items():
        if meters[uuid].sample is not None:
            print(f"\n⚡ {name} carries data past byte 12:")
            print(f"   {meters[uuid].sample.hex()[:120]}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default=None)
    ap.add_argument("--go", action="store_true", help="actually send")
    sys.exit(asyncio.run(main(ap.parse_args())))
