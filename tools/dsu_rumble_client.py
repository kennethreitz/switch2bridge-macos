#!/usr/bin/env python3
"""
Drive rumble the way a DSU client should — to prove the receiving end
=====================================================================

Rumble is the half of the DSU protocol nothing implements. Cemu 2.6 marks it
TODO and never emits it, so a bridge that receives it correctly looks identical
to one that ignores it: silence either way.

This sends what a client *should* send, so the receiving end can be proven on
its own. If the controller buzzes here, the relay works and any remaining
problem is in the emulator's sending side rather than in the protocol or here.

    python3 tools/dsu_rumble_client.py            # ask, then drive both motors
    python3 tools/dsu_rumble_client.py --timeout  # show the safety timeout

Needs the bridge running with a controller connected.

Both messages are marked "(Unofficial)" in v1993/cemuhook-protocol — extensions
to rajkosto's original rather than part of it — which is why nothing implements
them and why the framing is worth pinning down in one place:

    0x110001  motor info   client sends an 8-byte controller header;
                           server replies with 11 bytes + a motor count
    0x110002  rumble       client only. 8-byte header, motor id, intensity 0-255
"""

import argparse
import socket
import struct
import sys
import time
import zlib

MSG_MOTOR_INFO = 0x110001
MSG_RUMBLE = 0x110002

PROTOCOL_VERSION = 1001
CLIENT_ID = 0xCEEDBEEF & 0xFFFFFFFF
# The header a client puts in front of both messages. Slot 0, and zeroes for
# "any pad" — the bridge exposes one.
CONTROLLER_HEADER = bytes(8)


def packet(msg_type, payload=b""):
    data = struct.pack("<I", msg_type) + payload
    header = struct.pack("<4sHHII", b"DSUC", PROTOCOL_VERSION, len(data), 0,
                         CLIENT_ID)
    pkt = bytearray(header + data)
    struct.pack_into("<I", pkt, 8, zlib.crc32(bytes(pkt)) & 0xFFFFFFFF)
    return bytes(pkt)


def rumble_packet(motor, intensity):
    return packet(MSG_RUMBLE,
                  CONTROLLER_HEADER + bytes([motor & 0xFF, intensity & 0xFF]))


def ask_motor_count(sock, addr):
    sock.sendto(packet(MSG_MOTOR_INFO, CONTROLLER_HEADER), addr)
    try:
        reply, _ = sock.recvfrom(1024)
    except socket.timeout:
        print("   no reply to the motor-info query")
        return None
    if len(reply) < 32 or struct.unpack_from("<I", reply, 16)[0] != MSG_MOTOR_INFO:
        print(f"   unexpected reply to motor info: {reply[:32].hex()}")
        return None
    return reply[-1]


def hold(sock, addr, motor, intensity, seconds):
    """Drive one motor, re-sending as a real client must.

    The protocol says clients re-send two to ten times a second, and servers
    drop an effect that goes quiet so a client that dies does not leave the pad
    buzzing. A client that sends once and stops is therefore indistinguishable
    from one that crashed, and gets switched off.
    """
    end = time.time() + seconds
    sent = 0
    while time.time() < end:
        sock.sendto(rumble_packet(motor, intensity), addr)
        sent += 1
        time.sleep(0.2)   # 5 Hz, inside the 2-10 the protocol asks for
    return sent


def main(args):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    addr = (args.host, args.port)

    print(f"Asking {args.host}:{args.port} how many motors it has...")
    motors = ask_motor_count(sock, addr)
    if motors is None:
        print("❌ No motor info. Is the bridge running?")
        return 1
    print(f"   motor count: {motors}")
    if motors == 0:
        print("   The server reports no rumble support — with no controller")
        print("   connected that is the honest answer rather than a fault.")
        return 1

    if args.timeout:
        print("\nStarting a effect and then going silent, to show the timeout.")
        print("The pad should buzz, then stop by itself within ~5s.")
        sock.sendto(rumble_packet(0, 255), addr)
        for remaining in range(8, 0, -1):
            print(f"\r   silent for {9 - remaining}s...", end="", flush=True)
            time.sleep(1)
        print("\n   If it stopped on its own, the safety timeout works.")
        return 0

    print("\nDriving each motor in turn — the controller should buzz.\n")
    for motor in range(motors):
        for intensity, label in ((80, "gentle"), (180, "medium"), (255, "full")):
            print(f"   motor {motor}  {label:<7} intensity {intensity:3d}")
            hold(sock, addr, motor, intensity, 1.0)
        sock.sendto(rumble_packet(motor, 0), addr)
        time.sleep(0.5)

    print("\n   both motors together, full")
    end = time.time() + 1.5
    while time.time() < end:
        sock.sendto(rumble_packet(0, 255), addr)
        sock.sendto(rumble_packet(1, 255), addr)
        time.sleep(0.2)

    # Explicit zeroes rather than relying on the timeout, which is what a
    # well-behaved client does on the way out.
    for motor in range(motors):
        sock.sendto(rumble_packet(motor, 0), addr)
    print("\nDone — sent explicit zeroes to stop.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=26760)
    parser.add_argument("--timeout", action="store_true",
                        help="demonstrate the go-quiet safety timeout instead")
    sys.exit(main(parser.parse_args()))
