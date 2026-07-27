#!/usr/bin/env python3
"""
Read stick calibration out of the controller's SPI flash
========================================================

Protocol from darthcloud / german77's RE work:
https://github.com/darthcloud/BlueRetro/issues/1249

The bridge currently normalises sticks with measured constants. The
controller stores its own calibration, which is per-unit and authoritative.

Request   02 91 00 04 00 08 00 00 <len> 7e 00 00 <addr LE32>
Reply     02 01 00 04 00 f8 00 00 <len> 00 00 00 <addr LE32> <data>
              ReportType 0x02 (SPI), Command 0x04 (SPI read), Result 0xf8 = ACK

Calibration blocks follow the Switch 1 layout: magic + centre + max + min,
where max/min are *travel distances* from centre, not absolute positions.

    python3 tools/read_calibration.py --address <uuid>

Quit the Switch2 Bridge menubar app first. Read-only: nothing is written to
flash, only SPI *read* commands are issued.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakClient, BleakScanner  # noqa: E402

NINTENDO_COMPANY_IDS = (0x0553, 0x057E)
SWITCH2_PRO_PID_LE = b"\x69\x20"

CMD_CHAR = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"
RESPONSE_CHAR = "c765a961-d9d8-4d36-a20a-5315b111836a"

# Result byte differs between firmware revisions: darthcloud saw 0xf8,
# this controller replies 0x78. The reliable success test is that the
# address is echoed back correctly, so that is what we check.
ACK_BYTES = (0xF8, 0x78)

# (address, length, description)
TARGETS = [
    (0x000130A8, 0x20, "Factory stick calibration (LEFT confirmed here)"),
    (0x1FC000, 0x18, "Motion calibration (user)"),
    (0x1FC040, 0x0B, "User stick calibration, LEFT"),
    (0x1FC060, 0x0B, "User stick calibration, RIGHT"),
]

# Swept by --scan looking for the right-stick block and motion data
SCAN_RANGES = [
    (0x00013000, 0x00013200, "factory calibration region"),
    (0x1FC000, 0x1FC100, "user calibration region"),
]


def spi_read_cmd(address, length):
    return bytes([0x02, 0x91, 0x00, 0x04, 0x00, 0x08, 0x00, 0x00,
                  length, 0x7E, 0x00, 0x00,
                  address & 0xFF, (address >> 8) & 0xFF,
                  (address >> 16) & 0xFF, (address >> 24) & 0xFF])


def unpack_pair(chunk):
    """Three bytes -> two 12-bit values, the same packing the sticks use."""
    x = chunk[0] | ((chunk[1] & 0x0F) << 8)
    y = ((chunk[1] & 0xF0) >> 4) | (chunk[2] << 4)
    return x, y


def decode_stick_block(data):
    """Factory layout, confirmed against a real input capture:
    centre(3) + travel_max(3) + travel_min(3), each three bytes holding
    two 12-bit values. Returns (centre, max_travel, min_travel).
    """
    if len(data) < 9:
        return None
    return unpack_pair(data[0:3]), unpack_pair(data[3:6]), unpack_pair(data[6:9])


def plausible_stick_block(decoded):
    """Centre near mid-scale and travels in the observed 1200-2000 range."""
    if decoded is None:
        return False
    centre, tmax, tmin = decoded
    return (all(1500 < c < 2600 for c in centre)
            and all(1000 < t < 2048 for t in tmax + tmin))


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


async def main(args):
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

    inbox = asyncio.Queue()

    def on_response(_sender, payload):
        inbox.put_nowait(bytes(payload))

    await client.start_notify(RESPONSE_CHAR, on_response)
    char = client.services.get_characteristic(CMD_CHAR)
    if char is None:
        print("❌ Command characteristic not present.")
        await client.disconnect()
        return 1

    async def spi_read(addr, length):
        """Returns the data bytes, or None. Success = the address echoes back."""
        while not inbox.empty():
            inbox.get_nowait()
        await client.write_gatt_char(char, spi_read_cmd(addr, length), response=False)
        try:
            reply = await asyncio.wait_for(inbox.get(), timeout=2.0)
        except asyncio.TimeoutError:
            return None, None
        if len(reply) < 16:
            return None, reply
        echoed = int.from_bytes(reply[12:16], "little")
        if echoed != addr:
            return None, reply
        return reply[16:16 + length], reply

    found = []

    if args.scan:
        print("Scanning for non-erased blocks (0xFF means erased)…\n")
        for start, end, label in SCAN_RANGES:
            print(f"── {label}: 0x{start:08X}-0x{end:08X}")
            step = 0x20
            for addr in range(start, end, step):
                data, _raw = await spi_read(addr, step)
                if data is None:
                    continue
                if all(b == 0xFF for b in data):
                    continue
                print(f"   0x{addr:08X}: {data.hex()}")
                for offset in range(0, max(1, len(data) - 8)):
                    decoded = decode_stick_block(data[offset:offset + 9])
                    if plausible_stick_block(decoded):
                        centre, tmax, tmin = decoded
                        print(f"      ⚡ stick block at +0x{offset:02X}: "
                              f"centre={centre} +travel={tmax} -travel={tmin}")
                        found.append((addr + offset, decoded))
            print()
    else:
        for addr, length, label in TARGETS:
            data, raw = await spi_read(addr, length)
            print(f"0x{addr:08X}  {label}")
            if data is None:
                print(f"   ✗ no valid reply ({raw.hex() if raw else 'timeout'})\n")
                continue
            print(f"   data: {data.hex()}")
            if all(b == 0xFF for b in data):
                print("   (erased / unset)\n")
                continue
            for offset in range(0, max(1, len(data) - 8)):
                decoded = decode_stick_block(data[offset:offset + 9])
                if plausible_stick_block(decoded):
                    centre, tmax, tmin = decoded
                    print(f"   ⚡ stick block at +0x{offset:02X}")
                    print(f"      centre   {centre}")
                    print(f"      +travel  {tmax}")
                    print(f"      -travel  {tmin}")
                    print(f"      range    x {centre[0]-tmin[0]}..{centre[0]+tmax[0]}"
                          f"   y {centre[1]-tmin[1]}..{centre[1]+tmax[1]}")
                    found.append((addr + offset, decoded))
            print()

    await client.stop_notify(RESPONSE_CHAR)
    await client.disconnect()

    print("=" * 66)
    if found:
        print(f"✅ {len(found)} calibration block(s) found:\n")
        for addr, (centre, tmax, tmin) in found:
            print(f"   0x{addr:08X}  centre={centre} +travel={tmax} -travel={tmin}")
        if len(found) < 2:
            print("\n   Only one stick located. Re-run with --scan to sweep for"
                  "\n   the other one.")
    else:
        print("❌ No calibration blocks decoded.")
        print("   Try --scan to sweep the calibration regions.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default=None)
    ap.add_argument("--scan", action="store_true",
                    help="sweep the calibration regions for non-erased blocks")
    sys.exit(asyncio.run(main(ap.parse_args())))
