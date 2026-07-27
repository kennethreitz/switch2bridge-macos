#!/usr/bin/env python3
"""
Decode a capture from tools/capture_packets.py
==============================================

Finds the report layout empirically rather than by guessing:

* per-byte variance against the "rest" baseline tells us which offsets are
  live at all, and which move only during a given motion phase
* int16 pairs whose variance spikes during pitch/yaw/roll (but not at rest)
  are gyro candidates; ones that move during "shake" are accelerometer
* bits that are set for a whole btn_* phase and clear everywhere else are
  that button's bit

    python3 tools/analyze_capture.py capture.jsonl
"""

import argparse
import json
import statistics
import struct
import sys
from collections import defaultdict

KNOWN_BYTES = {
    0: "counter/report id?", 1: "?", 2: "buttons b2", 3: "buttons b3",
    4: "buttons b4", 5: "LX lo", 6: "LX hi | LY lo", 7: "LY hi",
    8: "RX lo", 9: "RX hi | RY lo", 10: "RY hi",
}
MOTION_PHASES = ("pitch", "yaw", "roll", "shake")


def load(path):
    by_phase = defaultdict(list)
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        by_phase[rec["phase"]].append(bytes.fromhex(rec["hex"]))
    return by_phase


def stdev(values):
    return statistics.pstdev(values) if len(values) > 1 else 0.0


def main(path):
    by_phase = load(path)
    if not by_phase:
        print("empty capture")
        return 1

    all_packets = [p for pkts in by_phase.values() for p in pkts]
    length = max(len(p) for p in all_packets)
    print(f"packets: {len(all_packets)}   report length: {length} bytes")
    print(f"phases:  {', '.join(f'{k}({len(v)})' for k, v in by_phase.items())}\n")

    rest = by_phase.get("rest") or by_phase.get("rest2") or []
    if not rest:
        print("!! no 'rest' phase — cannot establish a baseline")
        return 1

    # ---- per-byte activity ----
    print("=== per-byte stdev by phase (blank = quiet) ===")
    header = "off  known                 " + "".join(f"{p[:6]:>8}" for p in by_phase)
    print(header)
    live_offsets = []
    for off in range(length):
        cells, rest_sd = [], 0.0
        for phase, pkts in by_phase.items():
            vals = [p[off] for p in pkts if len(p) > off]
            sd = stdev(vals)
            if phase == "rest":
                rest_sd = sd
            cells.append(f"{sd:8.1f}" if sd > 0.5 else " " * 8)
        row_max = max(
            (stdev([p[off] for p in pkts if len(p) > off]) for pkts in by_phase.values()),
            default=0.0,
        )
        if row_max > 0.5:
            live_offsets.append((off, rest_sd, row_max))
            print(f"{off:3d}  {KNOWN_BYTES.get(off, ''):<20} " + "".join(cells))
    print()

    # ---- gyro / accel candidates: int16 pairs active only during motion ----
    print("=== int16 candidates (little-endian, signed) ===")
    print("off   rest_sd  " + "".join(f"{p[:7]:>9}" for p in MOTION_PHASES if p in by_phase))
    candidates = []
    for off in range(11, length - 1):
        def sd_for(pkts):
            vals = [
                struct.unpack_from("<h", p, off)[0] for p in pkts if len(p) >= off + 2
            ]
            return stdev(vals)

        rest_sd = sd_for(rest)
        motion_sds = {p: sd_for(by_phase[p]) for p in MOTION_PHASES if p in by_phase}
        if not motion_sds:
            continue
        peak = max(motion_sds.values())
        # live during motion, quiet at rest → IMU-shaped
        if peak > 50 and peak > rest_sd * 4:
            candidates.append((off, rest_sd, motion_sds, peak))
            cells = "".join(f"{motion_sds[p]:9.0f}" for p in motion_sds)
            print(f"{off:3d}  {rest_sd:8.1f}  {cells}")
    if not candidates:
        print("  (none — this report may carry no IMU data)")
    print()

    if candidates:
        best = max(candidates, key=lambda c: c[3])
        print(f"Strongest motion offset: {best[0]}")
        print("IMU blocks are usually 3 consecutive int16 (x,y,z). Contiguous runs:")
        offs = sorted(c[0] for c in candidates)
        runs, run = [], [offs[0]]
        for o in offs[1:]:
            if o == run[-1] + 2:
                run.append(o)
            else:
                runs.append(run)
                run = [o]
        runs.append(run)
        for r in runs:
            if len(r) >= 3:
                print(f"  offsets {r} → {len(r)} axes starting at byte {r[0]}")
        print()

    # ---- button bit discovery ----
    print("=== button bits (set throughout a btn_* phase, clear at rest) ===")
    rest_or = defaultdict(int)
    for p in rest:
        for off in range(min(len(p), length)):
            rest_or[off] |= p[off]
    found = False
    for phase, pkts in by_phase.items():
        if not phase.startswith("btn_") or not pkts:
            continue
        common = None
        for p in pkts:
            mask = {off: p[off] for off in range(min(len(p), length))}
            common = mask if common is None else {
                o: common[o] & mask.get(o, 0) for o in common
            }
        hits = [
            (o, v & ~rest_or[o])
            for o, v in (common or {}).items()
            if v & ~rest_or[o]
        ]
        label = phase[4:]
        if hits:
            found = True
            for o, bits in hits:
                print(f"  {label:<8} byte {o:2d} bit {bits:#04x}")
        else:
            print(f"  {label:<8} (no dedicated bit found)")
    if not found:
        print("  (nothing conclusive — was each button held for its whole phase?)")
    print()

    # ---- stick range ----
    print("=== stick extents (12-bit raw) ===")
    for label, phase, lo, hi in (
        ("LX", "stick_L", 5, 6), ("LY", "stick_L", 6, 7),
        ("RX", "stick_R", 8, 9), ("RY", "stick_R", 9, 10),
    ):
        pkts = by_phase.get(phase, [])
        if not pkts:
            continue
        if label.endswith("X"):
            vals = [p[lo] | ((p[hi] & 0x0F) << 8) for p in pkts if len(p) > hi]
        else:
            vals = [((p[lo] & 0xF0) >> 4) | (p[hi] << 4) for p in pkts if len(p) > hi]
        if vals:
            rest_vals = []
            for p in rest:
                if len(p) > hi:
                    rest_vals.append(
                        p[lo] | ((p[hi] & 0x0F) << 8) if label.endswith("X")
                        else ((p[lo] & 0xF0) >> 4) | (p[hi] << 4)
                    )
            center = round(statistics.mean(rest_vals)) if rest_vals else "?"
            print(f"  {label}: min={min(vals):4d} max={max(vals):4d} rest_center={center}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("capture", nargs="?", default="capture.jsonl")
    sys.exit(main(ap.parse_args().capture))
