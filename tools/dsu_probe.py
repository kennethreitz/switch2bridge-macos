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
    # Motion peaks are reported separately: sticks are judged against
    # full deflection, the IMU against gravity and against zero at rest.
    peaks.update({"|a|": 0.0, "gyro": 0.0})
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
        ax, ay, az, gp, gy, gr = struct.unpack_from("<6f", pkt, 76)
        # An accelerometer at rest reads 1g. Showing the magnitude rather than
        # just "motion: yes" is what tells a working IMU from a wrong offset,
        # since a bad decode still produces confident-looking numbers.
        magnitude = (ax * ax + ay * ay + az * az) ** 0.5
        peaks["|a|"] = max(peaks["|a|"], magnitude)
        peaks["gyro"] = max(peaks["gyro"], abs(gp), abs(gy), abs(gr))

        if magnitude:
            motion = f"|a|={magnitude:.2f}g gyro({gp:+6.1f},{gy:+6.1f},{gr:+6.1f})"
        else:
            motion = "motion: none"

        line = (f"L({lx:+.2f},{ly:+.2f}) R({rx:+.2f},{ry:+.2f})  "
                f"{motion}  {' '.join(held) if held else '-'}")
        print(f"\r{line:<110}", end="", flush=True)

    print(f"\n\n📊 {count} packets received")
    if not count:
        print("   ❌ Nothing arrived — the pad is not streaming.")
        return 1
    print("   Peak deflection per axis (1.00 means full range is reachable):")
    for name, value in list(peaks.items()):
        if name in ("|a|", "gyro"):
            continue
        flag = "✅" if value > 0.98 else ("•" if value > 0.5 else " ")
        print(f"     {flag} {name}: {value:.2f}")
    if peak <= 0.5:
        print("   (sticks barely moved — push them to the edges to test range)")

    print("\n   Motion:")
    if not peaks["|a|"]:
        print("     ❌ no motion in the stream — wired only, so check the cable")
    else:
        ok = 0.85 < peaks["|a|"] < 1.25
        print(f"     {'✅' if ok else '⚠️ '} peak |accel| {peaks['|a|']:.2f}g "
              f"(about 1.00 at rest means the decode is right)")
        turned = peaks["gyro"] > 20.0
        print(f"     {'✅' if turned else '•'} peak gyro {peaks['gyro']:.0f} deg/s"
              f"{'' if turned else '  (turn the controller to exercise it)'}")
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
