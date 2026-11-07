#!/usr/bin/env python3
"""
Measure the Switch 2 Pro Controller's BLE input rate
====================================================

The bridge already shows packets per second in the menu bar. This tool
adds the two numbers that say *why* the rate is what it is:

  * the gap between notifications (the connection interval, if the
    controller sends one report per event)
  * the byte-0 counter. A step of 1 means nothing was skipped. A jump
    means the controller produced reports that never arrived.

It listens on both input characteristics the controller exposes:

  ...f9   what this bridge subscribes to
  ...f8   what a Switch 2 console subscribes to

By default it only listens. `--write-rate-descriptor` also writes the
bytes a console writes (`85 00`) to the custom descriptor on `...f8`
(ATT handle 0x0010, UUID 679d5510-...) and measures again. That write is
what community captures label "set report rate". Hosts that send it still
report one notification per connection event, so the gap is not expected
to move. It is off the connect path on purpose.

    python3 tools/measure_rate.py                 # dry run, prints the plan
    python3 tools/measure_rate.py --go            # measure only
    python3 tools/measure_rate.py --go --write-rate-descriptor

Quit the Switch2 Bridge menu bar app first. The controller accepts one
BLE connection at a time.

250 Hz is not an expected result. A 4 ms gap is the USB poll. Over BLE
the gap should sit on the connection interval macOS negotiated (about
30 ms on the hardware this project was written against).
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from report_rate import ReportRateMeter  # noqa: E402

NINTENDO_COMPANY_IDS = (0x0553, 0x057E)
SWITCH2_PRO_PID_LE = b"\x69\x20"

INPUT_F8 = "7492866c-ec3e-4619-8258-32755ffcc0f8"  # console input, report 0x09
INPUT_F9 = "7492866c-ec3e-4619-8258-32755ffcc0f9"  # what the bridge reads
CHANNELS = {
    INPUT_F9: "f9  (what the bridge reads)",
    INPUT_F8: "f8  (what the console reads)",
}
# Custom descriptor on the input characteristics. The console writes
# 85 00 to the one on f8 (handle 0x0010) just before subscribing.
RATE_DESC = "679d5510-5a24-4dee-9557-95df80486ecb"
CONSOLE_RATE_BYTES = bytes((0x85, 0x00))


async def find(timeout=20.0):
    from bleak import BleakScanner
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for address, (device, adv) in devices.items():
        for cid in NINTENDO_COMPANY_IDS:
            payload = adv.manufacturer_data.get(cid)
            if payload and SWITCH2_PRO_PID_LE in payload:
                return address
        if device.name and "Pro Controller" in device.name:
            return address
    return None


def report(meters, label):
    print(f"\n   {label}")
    for uuid, name in CHANNELS.items():
        line = meters[uuid].format_line(name)
        stats = meters[uuid].summary()
        if stats is None:
            print(f"      {name:<32} silent")
        elif line is None:
            print(f"      {name:<32} {stats['count']} reports "
                  f"(window still short)")
        else:
            print(f"      {line}")


async def read_rate_descriptors(client):
    """Return (descriptor, parent_uuid, raw_value) for every rate descriptor."""
    found = []
    for service in client.services:
        for char in service.characteristics:
            for desc in char.descriptors:
                if str(desc.uuid).lower() != RATE_DESC:
                    continue
                try:
                    value = bytes(await client.read_gatt_descriptor(desc))
                except Exception as exc:
                    value = f"unreadable ({exc})".encode()
                found.append((desc, str(char.uuid).lower(), value))
    return found


async def measure(client, meters, seconds):
    for meter in meters.values():
        meter.reset()
    await asyncio.sleep(seconds)


async def main(args):
    if not args.go:
        print("PLAN — nothing is sent in dry-run mode\n")
        print("Connects, subscribes to ...f9 and ...f8, and prints Hz, the")
        print("inter-report gap, and byte-0 counter skips.\n")
        print("With --write-rate-descriptor it then writes "
              f"{CONSOLE_RATE_BYTES.hex(' ')} to the")
        print(f"{RATE_DESC}")
        print("descriptor on ...f8 (the write a Switch 2 does before subscribing)")
        print("and measures again. Expect the gap to stay on whatever interval")
        print("macOS negotiated. A counter that starts jumping, with the gap")
        print("unchanged, would mean the controller sampled faster than the")
        print("link could deliver.\n")
        print("Re-run with --go to measure. Add --write-rate-descriptor to")
        print("send that one write.")
        return 0

    from bleak import BleakClient

    address = args.address or await find()
    if not address:
        print("Controller not found.")
        return 1

    client = BleakClient(address, timeout=20.0)
    await client.connect()
    print(f"Connected to {address}")

    meters = {uuid: ReportRateMeter() for uuid in CHANNELS}
    loop = asyncio.get_running_loop()

    def handler(uuid):
        def on_data(_sender, payload):
            payload = bytes(payload)
            if not payload:
                return
            meters[uuid].observe(payload[0], loop.time(), False)
        return on_data

    for uuid in CHANNELS:
        try:
            await client.start_notify(uuid, handler(uuid))
            print(f"   subscribed {CHANNELS[uuid]}")
        except Exception as exc:
            print(f"   could not subscribe {CHANNELS[uuid]}: {exc}")

    try:
        descriptors = await read_rate_descriptors(client)
    except Exception as exc:
        descriptors = []
        print(f"   descriptor walk failed: {exc}")
    if descriptors:
        print("   rate descriptors:")
        for _desc, parent, value in descriptors:
            shown = value.hex() if isinstance(value, (bytes, bytearray)) else value
            print(f"      parent {parent}  value {shown}")
    else:
        print("   rate descriptor not found")

    await measure(client, meters, args.seconds)
    report(meters, f"BEFORE — {args.seconds:.0f}s, listen only")

    if args.write_rate_descriptor:
        target = next((item for item in descriptors
                       if item[1] == INPUT_F8), None)
        if target is None:
            print("\n   ...f8 has no rate descriptor; not writing")
        else:
            desc, _parent, before = target
            print(f"\n   writing {CONSOLE_RATE_BYTES.hex()} to the ...f8 "
                  "rate descriptor")
            try:
                await client.write_gatt_descriptor(desc, CONSOLE_RATE_BYTES)
            except Exception as exc:
                print(f"   write failed: {exc}")
            else:
                try:
                    after = bytes(await client.read_gatt_descriptor(desc))
                    print(f"   descriptor now {after.hex()} "
                          f"(was {before.hex() if isinstance(before, (bytes, bytearray)) else before})")
                except Exception as exc:
                    print(f"   wrote, but read-back failed: {exc}")
                await measure(client, meters, args.seconds)
                report(meters, f"AFTER — {args.seconds:.0f}s, descriptor written")

    for uuid in CHANNELS:
        try:
            await client.stop_notify(uuid)
        except Exception:
            pass
    try:
        await client.disconnect()
    except Exception:
        pass

    print("\nRead the p50 gap. ~30 ms with counter skips 0 is the macOS")
    print("connection interval, one report per event. That is the limit.")
    print("A ~4 ms gap would be 250 Hz and is not expected over BLE.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default=None)
    ap.add_argument("--go", action="store_true", help="connect and measure")
    ap.add_argument("--seconds", type=float, default=6.0,
                    help="how long to listen in each phase")
    ap.add_argument("--write-rate-descriptor", action="store_true",
                    help="write the console's 85 00 descriptor value and measure again")
    sys.exit(asyncio.run(main(ap.parse_args())))
