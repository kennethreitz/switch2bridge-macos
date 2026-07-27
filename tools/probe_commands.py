#!/usr/bin/env python3
"""
Send real commands to the controller (confirmed protocol)
=========================================================

Protocol from darthcloud's BlueRetro RE work:
https://github.com/darthcloud/BlueRetro/issues/1249

Command channel
    649d4ac9-8eb7-4e6c-af44-1ea54fe5f005   (ATT handle 0x0014)
        accepts commands directly:  <cmd_id> 0x91 [...]
    3dacbc7e-6955-40b5-8eaf-6f9809e8b379   (ATT handle 0x0016)
        same, but requires 33 leading 0x00 bytes

Response channel
    c765a961-d9d8-4d36-a20a-5315b111836a   (ATT handle 0x001a)
        async responses for writes to either command handle

Extra input reports the bridge does not currently read
    ab7de9be-89fe-49ad-828f-118f09df7fd2   (0x000a) HID input report, format 1
    7492866c-ec3e-4619-8258-32755ffcc0f8   (0x000e) HID input report, format 2
                                           (matches USB report 0x0A)

    python3 tools/probe_commands.py             # dry run: print the plan
    python3 tools/probe_commands.py --go        # send the player-LED command

Quit the Switch2 Bridge menubar app first.
"""

import argparse
import asyncio
import collections
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient, BleakScanner  # noqa: E402

NINTENDO_COMPANY_IDS = (0x0553, 0x057E)
SWITCH2_PRO_PID_LE = b"\x69\x20"

CMD_CHAR = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"        # handle 0x0014, no padding
CMD_CHAR_PADDED = "3dacbc7e-6955-40b5-8eaf-6f9809e8b379"  # handle 0x0016, 33x 0x00
RESPONSE_CHAR = "c765a961-d9d8-4d36-a20a-5315b111836a"    # handle 0x001a
INPUT_F1 = "ab7de9be-89fe-49ad-828f-118f09df7fd2"         # handle 0x000a
INPUT_F2 = "7492866c-ec3e-4619-8258-32755ffcc0f8"         # handle 0x000e
INPUT_CURRENT = "7492866c-ec3e-4619-8258-32755ffcc0f9"    # what the bridge uses

LABELS = {
    CMD_CHAR: "cmd (0x0014)",
    CMD_CHAR_PADDED: "cmd padded (0x0016)",
    RESPONSE_CHAR: "response (0x001a)",
    INPUT_F1: "input fmt1 (0x000a)",
    INPUT_F2: "input fmt2 (0x000e)",
    INPUT_CURRENT: "input (bridge)",
}


def led_command(pattern):
    """Player-lights command.

    09 91 00 07 00 08 00 00 <pattern> 00 00 00 00 00 00 00
      ReportType 0x09 (player lights), ReportMode 0x91 (request),
      Command 0x07 (set pattern). Bit per LED, so 0x01 = LED 1.
    """
    return bytes([0x09, 0x91, 0x00, 0x07, 0x00, 0x08, 0x00, 0x00,
                  pattern, 0, 0, 0, 0, 0, 0, 0])


# (name, payload, why it is safe)
PROBES = [
    ("led-1", led_command(0x01), "player LED 1 — visible, non-persistent"),
    ("led-1+2", led_command(0x03), "player LEDs 1+2 — proves the pattern byte"),
    ("led-all", led_command(0x0F), "all four LEDs"),
    ("led-1-again", led_command(0x01), "back to LED 1"),
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


def print_plan(target, padded):
    print("PLAN — nothing is sent in dry-run mode\n")
    print(f"Command endpoint : {target}")
    print(f"Framing          : {'33x 0x00 prefix + command' if padded else 'command sent directly'}")
    print(f"Watching         : {RESPONSE_CHAR} (async responses)")
    print(f"                   {INPUT_F1} (input format 1)")
    print(f"                   {INPUT_F2} (input format 2)\n")
    print("Commands, in order:\n")
    for name, payload, why in PROBES:
        print(f"   {name:<14} {payload.hex()}")
        print(f"   {'':<14} {why}\n")
    print("Each is followed by a 1.2s pause. Watch the controller's LEDs:")
    print("they are the success signal — no log needed to see it work.\n")
    print("Re-run with --go to send.")


async def main(args):
    target = CMD_CHAR_PADDED if args.padded else CMD_CHAR
    if args.char:
        target = args.char

    if not args.go:
        print_plan(target, args.padded)
        return 0

    address = args.address
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

    received = collections.deque()
    seen = collections.Counter()
    tails = {}

    def make_handler(uuid):
        def handler(_sender, payload):
            payload = bytes(payload)
            seen[uuid] += 1
            if uuid in (INPUT_F1, INPUT_F2, INPUT_CURRENT):
                if any(payload[12:]) and uuid not in tails:
                    tails[uuid] = payload.hex()
                return
            received.append((time.monotonic(), uuid, payload))
        return handler

    subscribed = []
    for service in client.services:
        for char in service.characteristics:
            if "notify" in char.properties or "indicate" in char.properties:
                try:
                    await client.start_notify(char, make_handler(char.uuid))
                    subscribed.append(char.uuid)
                except Exception as e:
                    print(f"   ✗ subscribe {char.uuid}: {e}")
    print(f"Listening on {len(subscribed)} channel(s), "
          f"including {LABELS.get(RESPONSE_CHAR)}.\n")
    await asyncio.sleep(1.0)

    char = client.services.get_characteristic(target)
    if char is None:
        print(f"❌ {target} not present on this controller.")
        await client.disconnect()
        return 1

    prefix = b"\x00" * 33 if args.padded else b""
    hits = []

    print(f"── sending to {target}\n")
    for name, payload, _why in PROBES:
        received.clear()
        wire = prefix + payload
        try:
            await client.write_gatt_char(char, wire, response=False)
            status = "sent"
        except Exception as e:
            status = f"rejected: {type(e).__name__}: {e}"
        await asyncio.sleep(1.2)

        replies = list(received)
        note = f"  ⚡ {len(replies)} reply" if replies else ""
        print(f"   {name:<14} {payload.hex()}  {status}{note}")
        for _t, uuid, rpayload in replies[:3]:
            print(f"       ← {LABELS.get(uuid, uuid)}: {rpayload.hex()[:80]}")
        if replies:
            hits.append((name, payload, replies))

    for uuid in subscribed:
        try:
            await client.stop_notify(uuid)
        except Exception:
            pass
    await client.disconnect()

    print("\n" + "=" * 68)
    print("CHANNEL TRAFFIC")
    for uuid, count in seen.most_common():
        print(f"   {LABELS.get(uuid, uuid):<24} {count} packet(s)")
    if tails:
        print("\n⚡ input reports with NON-ZERO data past byte 12 "
              "(candidate motion payload):")
        for uuid, sample in tails.items():
            print(f"   {LABELS.get(uuid, uuid)}: {sample[:100]}")
    if hits:
        print("\n✅ The command channel responds — LED control should be live.")
        print("   Did the controller's lights change? That is the real signal.")
    else:
        print("\n⚠️  No async replies. If the LEDs still changed, the command")
        print("    worked and this controller just does not ACK. If nothing")
        print("    happened, try --padded to use handle 0x0016 framing.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default=None)
    ap.add_argument("--go", action="store_true", help="actually send (default: dry run)")
    ap.add_argument("--padded", action="store_true",
                    help="use handle 0x0016 framing (33x 0x00 prefix)")
    ap.add_argument("--char", default=None, help="override the command characteristic")
    sys.exit(asyncio.run(main(ap.parse_args())))
