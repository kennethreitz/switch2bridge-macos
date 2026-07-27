#!/usr/bin/env python3
"""
Minimal DSU client — verify what emulators actually receive
===========================================================

Subscribes to the bridge's DSU server and prints the pad state live, so you
can confirm the whole chain works without launching an emulator.

    python3 tools/dsu_probe.py

Shows a live line, and on exit reports the peak stick deflection seen — the
number that proves calibration is working (it should reach 1.00).
"""

import argparse
import socket
import struct
import sys
import time
import zlib

MSG_VERSION = 0x100000
MSG_PORTS = 0x100001
MSG_DATA = 0x100002

BUTTONS1 = ((0x01, "Share"), (0x02, "L3"), (0x04, "R3"), (0x08, "Options"),
            (0x10, "Up"), (0x20, "Right"), (0x40, "Down"), (0x80, "Left"))
BUTTONS2 = ((0x01, "L2"), (0x02, "R2"), (0x04, "L1"), (0x08, "R1"),
            (0x10, "Triangle"), (0x20, "Circle"), (0x40, "Cross"), (0x80, "Square"))


def packet(msg_type, payload=b""):
    data = struct.pack("<I", msg_type) + payload
    header = struct.pack("<4sHHII", b"DSUC", 1001, len(data), 0, 0xC0FFEE)
    pkt = bytearray(header + data)
    struct.pack_into("<I", pkt, 8, zlib.crc32(bytes(pkt)) & 0xFFFFFFFF)
    return bytes(pkt)


def axis(byte):
    """0..255 with 128 centre -> -1.0..1.0"""
    return (byte - 128) / 127.0


def main(host, port, seconds):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    addr = (host, port)

    sock.sendto(packet(MSG_VERSION), addr)
    try:
        reply, _ = sock.recvfrom(1024)
        print(f"✅ DSU server v{struct.unpack_from('<H', reply, 20)[0]} at {host}:{port}")
    except socket.timeout:
        print(f"❌ No response from {host}:{port} — is the bridge running?")
        return 1

    sock.sendto(packet(MSG_PORTS, struct.pack("<i", 1) + bytes([0])), addr)
    try:
        reply, _ = sock.recvfrom(1024)
        state = reply[21]
        print(f"   Pad slot 0: {'connected' if state == 2 else 'NOT connected'}"
              f"  model={reply[22]} conn={reply[23]}")
        if state != 2:
            print("   ⚠️  Connect the controller in the menubar app first.")
    except socket.timeout:
        print("   (no port reply)")

    print(f"\nStreaming for {seconds}s — move the sticks and press buttons.\n")
    peak = 0.0
    peaks = {"LX": 0.0, "LY": 0.0, "RX": 0.0, "RY": 0.0}
    count = 0
    deadline = time.monotonic() + seconds
    last_sub = 0.0

    while time.monotonic() < deadline:
        now = time.monotonic()
        if now - last_sub > 1.0:            # re-subscribe; registration expires
            sock.sendto(packet(MSG_DATA, bytes([0, 0]) + b"\x00" * 6), addr)
            last_sub = now
        try:
            pkt, _ = sock.recvfrom(2048)
        except socket.timeout:
            continue
        if len(pkt) < 100 or struct.unpack_from("<I", pkt, 16)[0] != MSG_DATA:
            continue
        count += 1

        lx, ly = axis(pkt[40]), axis(pkt[41])
        rx, ry = axis(pkt[42]), axis(pkt[43])
        for name, value in (("LX", lx), ("LY", ly), ("RX", rx), ("RY", ry)):
            peaks[name] = max(peaks[name], abs(value))
        peak = max(peak, abs(lx), abs(ly), abs(rx), abs(ry))

        held = [n for bit, n in BUTTONS1 if pkt[36] & bit]
        held += [n for bit, n in BUTTONS2 if pkt[37] & bit]
        if pkt[38]:
            held.append("PS")
        if pkt[39]:
            held.append("Touch")
        gyro = struct.unpack_from("<6f", pkt, 76)

        line = (f"L({lx:+.2f},{ly:+.2f}) R({rx:+.2f},{ry:+.2f})  "
                f"motion={'yes' if any(gyro) else 'no '}  "
                f"{' '.join(held) if held else '-'}")
        print(f"\r{line:<90}", end="", flush=True)

    print(f"\n\n📊 {count} packets received")
    if not count:
        print("   ❌ Nothing arrived — the pad is not streaming.")
        return 1
    print("   Peak deflection per axis (1.00 means full range is reachable):")
    for name, value in peaks.items():
        flag = "✅" if value > 0.98 else ("•" if value > 0.5 else " ")
        print(f"     {flag} {name}: {value:.2f}")
    if peak <= 0.5:
        print("   (sticks barely moved — push them to the edges to test range)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=26760)
    ap.add_argument("--seconds", type=int, default=25)
    args = ap.parse_args()
    try:
        sys.exit(main(args.host, args.port, args.seconds))
    except KeyboardInterrupt:
        print()
