#!/usr/bin/env python3
"""
Does input report 0x05 — and therefore motion — work over Bluetooth?
====================================================================

Report `0x05` carries plaintext accel and gyro, and is verified over USB. That
does not settle Bluetooth: the report is selected by a command, but it has to
arrive somewhere, and the console reads input from a different characteristic
than this project does. ndeadly's captures are named for handles `0x000A` and
`0x000E`, which suggests the two report formats land on different attributes.

So this subscribes to every input characteristic at once, asks for report
`0x05`, and reports which one — if any — starts carrying it.

    python3 tools/ble_motion.py
    python3 tools/ble_motion.py --address <uuid> --seconds 8

Unplug the USB cable first (a wired controller will not advertise), and quit
the Switch2 Bridge menubar app.
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

MOTION_REPORT_ID = 0x05
TRANSPORT_BLUETOOTH = 0x01

# Every characteristic seen carrying input, so nothing is missed by guessing.
INPUT_CHANNELS = {
    "7492866c-ec3e-4619-8258-32755ffcc0f9": "input f9 (the bridge reads this)",
    "7492866c-ec3e-4619-8258-32755ffcc0f8": "input f8 (the console reads this)",
    "ab7de9be-89fe-49ad-828f-118f09df7fd2": "input fd2",
    "ab7de9be-89fe-49ad-828f-118f09df7fde": "input fde",
}

# Offsets into a report 0x05 body (no HID report-id byte over Bluetooth, so
# these are one lower than the USB numbers).
OFF_SENSOR_TS = 0x2A
OFF_ACCEL = 0x30
OFF_GYRO = 0x36
G = 9.80665
ACCEL_SCALE = G * 8.0 / 32767.0
GYRO_COEFF = 34.8


def command(report_type, cmd_id, payload=b"", length=None):
    head = bytes([report_type, 0x91, TRANSPORT_BLUETOOTH, cmd_id, 0x00,
                  length if length is not None else len(payload), 0x00, 0x00])
    return head + payload


STEPS = (
    ("feature-mask 0x27",
     command(0x0C, 0x02, bytes([0x27, 0, 0, 0]), length=0x04)),
    ("enable-features 0x27",
     command(0x0C, 0x04, bytes([0x27, 0, 0, 0]), length=0x04)),
    ("select report 0x05",
     command(0x03, 0x0A, bytes([MOTION_REPORT_ID, 0, 0, 0]), length=0x04)),
)


def s16(buf, offset):
    return struct.unpack_from("<h", buf, offset)[0]


# One g reads as this many counts on a +/-8g 16-bit scale. An accelerometer at
# rest always measures gravity, so a triple whose magnitude sits near this is
# almost certainly the accel block — whatever offset it happens to live at.
ONE_G_COUNTS = 32767 / 8.0


def find_accel_block(packets):
    """Search every offset for three int16s that behave like an accelerometer.

    Assuming SDL's USB offsets is what made the first version of this tool
    answer the wrong question. Gravity is a property of the sensor rather than
    of the report layout, so searching for it finds the block over any
    transport and any format.
    """
    if not packets:
        return []
    width = min(len(p) for p in packets)
    hits = []
    sample = packets[::max(1, len(packets) // 30)][:30]
    for offset in range(0, width - 6, 1):
        magnitudes = []
        for pkt in sample:
            x, y, z = (s16(pkt, offset), s16(pkt, offset + 2), s16(pkt, offset + 4))
            magnitudes.append((x * x + y * y + z * z) ** 0.5)
        if not magnitudes:
            continue
        mean = sum(magnitudes) / len(magnitudes)
        if abs(mean - ONE_G_COUNTS) / ONE_G_COUNTS >= 0.15:
            continue
        spread = max(magnitudes) - min(magnitudes)
        # Gravity is *steady* on a controller sitting still. Magnitude alone is
        # not enough: high-entropy bytes land near 1g by chance often enough to
        # produce a confident-looking false positive, and one did. Demanding a
        # near-constant magnitude is what separates a real sensor from noise,
        # so this must be run with the controller stationary.
        if spread > ONE_G_COUNTS * 0.12:
            continue
        hits.append((offset, mean, spread))
    return hits


def find_live_counters(packets, width=4):
    """Offsets holding a value that advances almost every packet.

    A sensor clock looks like this; a packet counter does too, so the caller
    still has to tell them apart, but it narrows the search enormously.
    """
    if len(packets) < 8:
        return []
    size = min(len(p) for p in packets)
    live = []
    for offset in range(0, size - width):
        values = [int.from_bytes(p[offset:offset + width], "little") for p in packets]
        distinct = len(set(values))
        if distinct < len(packets) * 0.8:
            continue
        rising = sum(1 for a, b in zip(values, values[1:]) if b > a)
        if rising >= len(values) - 2:
            step = (values[-1] - values[0]) / max(1, len(values) - 1)
            live.append((offset, step))
    return live


def describe_bytes(packets):
    """Which byte positions ever change, as a compact map."""
    if not packets:
        return ""
    width = min(len(p) for p in packets)
    out = []
    for offset in range(width):
        values = {p[offset] for p in packets}
        out.append("." if len(values) == 1 else ("#" if len(values) > 8 else "+"))
    return "".join(out)


async def find_controller(timeout):
    print(f"Scanning for {timeout:.0f}s — the controller must be unplugged.")
    found = {}

    def seen(device, adv):
        for company, payload in (adv.manufacturer_data or {}).items():
            if company in NINTENDO_COMPANY_IDS and SWITCH2_PRO_PID_LE in payload:
                found.setdefault(device.address, device)

    scanner = BleakScanner(detection_callback=seen)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()
    if not found:
        return None
    device = next(iter(found.values()))
    print(f"Found {device.name or 'controller'} at {device.address}")
    return device


async def run(args):
    address = args.address
    if address is None:
        device = await find_controller(args.scan)
        if device is None:
            print("❌ No Pro 2 advertising. Unplug the cable and make sure the "
                  "controller is on (hold the pairing button if needed).")
            return 1
        address = device.address

    async with BleakClient(address, timeout=20.0) as client:
        print(f"Connected.\n")

        # Handles matter here: the capture filenames name handles, not UUIDs.
        print("Characteristics that can notify:")
        notifiers = []
        for service in client.services:
            for char in service.characteristics:
                if "notify" not in char.properties:
                    continue
                label = INPUT_CHANNELS.get(char.uuid.lower(), "")
                print(f"   handle {char.handle:#06x}  {char.uuid}  {label}")
                notifiers.append(char)

        received = {}

        def make_handler(char):
            def handler(_sender, data):
                bucket = received.setdefault(char.uuid, [])
                bucket.append(bytes(data))
            return handler

        for char in notifiers:
            try:
                await client.start_notify(char, make_handler(char))
            except Exception as e:
                print(f"   (cannot subscribe to {char.uuid}: {e})")

        print("\nBaseline — what arrives before asking for report 0x05:")
        await asyncio.sleep(2.0)
        baseline = {uuid: len(pkts) for uuid, pkts in received.items()}
        for uuid, count in baseline.items():
            sample = received[uuid][-1]
            print(f"   {uuid}  {count:4d} packets, first byte {sample[0]:#04x}, "
                  f"{len(sample)} bytes")

        print("\nSending the report-select sequence:")
        for name, frame in STEPS:
            received.pop(cc.RESPONSE_CHAR, None)
            try:
                await client.write_gatt_char(cc.COMMAND_CHAR, frame, response=False)
            except Exception as e:
                print(f"   {name:<24} WRITE FAILED: {e}")
                continue
            await asyncio.sleep(0.3)
            # The reply says whether the firmware took it. Judged by the echoed
            # command rather than the result byte, which differs per transport.
            replies = received.get(cc.RESPONSE_CHAR, [])
            if not replies:
                print(f"   {name:<24} written, no reply")
                continue
            reply = replies[-1]
            state = {0x01: "accepted", 0x04: "REJECTED"}.get(
                reply[1] if len(reply) > 1 else -1, "unknown")
            print(f"   {name:<24} reply {reply[:8].hex()}  {state}")

        received.clear()
        print(f"\nListening for {args.seconds:.0f}s — move the controller.\n")
        await asyncio.sleep(args.seconds)

        print("Result per characteristic:")
        verdict = False
        # Report every subscribed characteristic, including the silent ones.
        # "fd2 and f8 sent nothing" is the interesting half of this answer:
        # bleak shows declaration handles, so those two are value handles
        # 0x000A and 0x000E — the pair ndeadly's motion captures are named for.
        for char in notifiers:
            uuid = char.uuid
            packets = received.get(uuid, [])
            label = INPUT_CHANNELS.get(uuid.lower(), "")
            if not packets:
                print(f"\n   {uuid}  {label}")
                print(f"      handle {char.handle:#06x} (value {char.handle + 1:#06x})"
                      f" — silent, 0 packets")
                continue
            lengths = sorted({len(p) for p in packets})
            print(f"\n   {uuid}  {label}")
            print(f"      handle {char.handle:#06x} (value {char.handle + 1:#06x}), "
                  f"{len(packets)} packets ({len(packets)/args.seconds:.0f} Hz), "
                  f"lengths {lengths}")
            print(f"      full sample: {packets[-1].hex()}")
            print(f"      changing bytes (. fixed, + few, # many):")
            print(f"        {describe_bytes(packets)}")

            accel = find_accel_block(packets)
            if accel:
                for offset, mean, spread in accel:
                    magnitude = mean / ONE_G_COUNTS * G
                    print(f"      ACCEL-LIKE at offset {offset} (0x{offset:02x}): "
                          f"|a| = {magnitude:.2f} m/s^2, spread {spread:.0f} counts")
                verdict = True
            else:
                print("      no offset holds a 1g-magnitude int16 triple")

            counters = find_live_counters(packets)
            if counters:
                shown = ", ".join(f"0x{o:02x} (+{s:.0f}/pkt)" for o, s in counters[:6])
                print(f"      advancing 32-bit fields at: {shown}")
            else:
                print("      no advancing 32-bit field (no live sensor clock)")

        print("\n" + "=" * 66)
        if verdict:
            print("✅ An accelerometer block is present over Bluetooth — motion")
            print("   is not USB-only.")
        else:
            print("❌ Nothing that behaves like an accelerometer, at any offset,")
            print("   on any characteristic. On this firmware motion looks")
            print("   USB-only, which would make the wired path a prerequisite")
            print("   for it rather than a speedup.")

        for char in notifiers:
            try:
                await client.stop_notify(char)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", help="skip scanning and connect to this address")
    parser.add_argument("--scan", type=float, default=10.0, help="scan seconds")
    parser.add_argument("--seconds", type=float, default=8.0,
                        help="how long to listen after selecting the report")
    sys.exit(asyncio.run(run(parser.parse_args())))
