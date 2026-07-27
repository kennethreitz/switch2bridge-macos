"""Headless tests for controller state, stick calibration and config migration."""
import json
import math
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import Switch2Bridge as S2B
from controller_state import (
    STICK_RAW_CENTER,
    ControllerState,
    StickCalibration,
    axis_from_raw,
    shape_stick,
)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def close(a, b, tol=1e-6):
    return abs(a - b) < tol


# ============ axis_from_raw ============
print("== axis_from_raw ==")
check("centre reads zero", close(axis_from_raw(STICK_RAW_CENTER), 0.0))
check("clamps positive", axis_from_raw(4095) == 1.0)
check("clamps negative", axis_from_raw(0) == -1.0)
check("half_range of zero is safe", axis_from_raw(3000, half_range=0) == 0.0)

# The bug this replaced: dividing by the nominal 2048 capped a fully pushed
# stick at ~0.8, so emulators never saw full deflection. Measured travel is
# 1514-1767 counts, and the real extremes from the capture must reach 1.0.
print("== full deflection (regression) ==")
for label, raw, center in (("LX max", 3751, 1984), ("LX min", 389, 1984),
                           ("LY max", 3656, 2142), ("LY min", 481, 2142),
                           ("RX min", 501, 2140), ("RY max", 3772, 2157)):
    old = (raw - 2048) / 2048.0
    new = axis_from_raw(raw, center=center)
    check(f"{label}: reaches full range (was {old:+.2f})", abs(new) == 1.0, f"{new:+.2f}")

# ============ shape_stick ============
print("== shape_stick ==")
check("inside deadzone is centred", shape_stick(0.05, 0.0, 0.08, 0.95) == (0.0, 0.0))
check("exactly at deadzone is centred", shape_stick(0.08, 0.0, 0.08, 0.95) == (0.0, 0.0))
x, y = shape_stick(0.95, 0.0, 0.08, 0.95)
check("at saturation reads full", close(x, 1.0) and close(y, 0.0), (x, y))
x, y = shape_stick(1.0, 1.0, 0.08, 0.95)
check("clamped to unit circle", close(math.hypot(x, y), 1.0), math.hypot(x, y))
x, y = shape_stick(0.5, 0.0, 0.0, 1.0)
check("no deadzone passes through", close(x, 0.5), x)
# a radial deadzone must not carve a cross-shaped dead region out of diagonals
x, y = shape_stick(0.07, 0.07, 0.08, 0.95)
check("diagonal just outside radial deadzone lives",
      math.hypot(x, y) > 0.0, (x, y))
x, y = shape_stick(0.0, 0.0, 0.08, 0.95)
check("dead centre is exactly zero", (x, y) == (0.0, 0.0))
check("at deadzone edge with zero span is centred",
      shape_stick(0.5, 0.0, 0.5, 0.5) == (0.0, 0.0))
check("degenerate span does not divide by zero",
      shape_stick(0.6, 0.0, 0.5, 0.5) == (1.0, 0.0))

# ============ StickCalibration ============
print("== StickCalibration ==")
cal = StickCalibration()
check("starts uncalibrated", not cal.calibrated)
check("defaults to nominal centre", cal.centers['lx'] == STICK_RAW_CENTER)

# a quiet stick: centres get learned
for _ in range(StickCalibration.SAMPLE_COUNT):
    cal.observe({'lx': 1984, 'ly': 2142, 'rx': 2140, 'ry': 2157})
check("calibrates after enough quiet samples", cal.calibrated)
check("learned lx centre", cal.centers['lx'] == 1984, cal.centers['lx'])
check("learned ry centre", cal.centers['ry'] == 2157, cal.centers['ry'])
check("learned centre reads as zero", close(cal.value('ry', 2157), 0.0))

# drift really is cancelled: 2157 would otherwise read as a held stick
naive = axis_from_raw(2157, center=STICK_RAW_CENTER)
check("uncalibrated drift would be non-zero", abs(naive) > 0.06, naive)

# a stick being moved during calibration must be rejected, not averaged
cal2 = StickCalibration()
for i in range(StickCalibration.SAMPLE_COUNT):
    cal2.observe({'lx': 2048 + i * 20, 'ly': 2048, 'rx': 2048, 'ry': 2048})
check("moving stick defers calibration", not cal2.calibrated)
check("moving stick keeps nominal centre", cal2.centers['lx'] == STICK_RAW_CENTER)

# then settles once the stick is released
for _ in range(StickCalibration.SAMPLE_COUNT):
    cal2.observe({'lx': 2000, 'ly': 2048, 'rx': 2048, 'ry': 2048})
check("calibrates once the stick settles", cal2.calibrated and cal2.centers['lx'] == 2000)

cal2.reset()
check("reset clears calibration", not cal2.calibrated)
check("reset restores nominal centre", cal2.centers['lx'] == STICK_RAW_CENTER)

cal3 = StickCalibration(auto_center=False)
for _ in range(StickCalibration.SAMPLE_COUNT * 2):
    cal3.observe({'lx': 1900, 'ly': 1900, 'rx': 1900, 'ry': 1900})
check("auto_center off never calibrates", not cal3.calibrated)

# ============ ControllerState ============
print("== ControllerState ==")
st = ControllerState(buttons={'A': True, 'B': False})
check("pressed reads truthy", st.pressed('A') and not st.pressed('B'))
check("unknown button is not pressed", not st.pressed('NOPE'))
check("no motion by default", not st.has_motion)
check("motion detected", ControllerState(gyro=(0.0, 1.0, 0.0)).has_motion)
check("repr lists held buttons", 'A' in repr(st))

# ============ config migration ============
print("== migration ==")
tmpdir = Path(tempfile.mkdtemp())
S2B.CONFIG_DIR = tmpdir
S2B.MAPPINGS_FILE = tmpdir / "mappings.json"

v1 = {
    "version": 1,
    "buttons": {"A": "z"},
    "sticks": {"threshold": 0.5, "left": {"up": "w"}, "right": {}},
    "dsu": {"enabled": True, "host": "127.0.0.1", "port": 26760},
}
S2B.MAPPINGS_FILE.write_text(json.dumps(v1))
m = S2B.Mappings()
ok = m.load()
check("v1 file loads cleanly", ok is True, m.last_error)
check("keyboard defaults off after migration", m.keyboard_enabled is False)
check("migration warns about the change",
      "keyboard" in (m.last_warning or "").lower(), m.last_warning)
written = json.loads(S2B.MAPPINGS_FILE.read_text())
check("migration bumps version", written["version"] == S2B.Mappings.CONFIG_VERSION)
check("migration adds keyboard block", written["keyboard"]["enabled"] is False)
check("migration adds motion block", "motion" in written)
check("migration adds calibration", "calibration" in written["sticks"])
check("migration preserves user mapping", written["buttons"]["A"] == "z")
check("migration preserves dsu port", written["dsu"]["port"] == 26760)

# migrating again is a no-op and must not re-warn
m2 = S2B.Mappings()
m2.load()
check("second load does not re-warn",
      "keyboard" not in (m2.last_warning or "").lower(), m2.last_warning)

# ============ keyboard toggle persistence ============
print("== keyboard toggle ==")
m2.set_keyboard_enabled(True)
check("toggle applied in memory", m2.keyboard_enabled is True)
check("toggle persisted",
      json.loads(S2B.MAPPINGS_FILE.read_text())["keyboard"]["enabled"] is True)
S2B.MAPPINGS_FILE.write_text("{broken")
m2.set_keyboard_enabled(False)
check("corrupt file untouched by toggle", S2B.MAPPINGS_FILE.read_text() == "{broken")

# ============ dsu aliases + motion validation ============
print("== aliases & motion config ==")
M = S2B.Mappings


def apply(**overrides):
    cfg = json.loads(json.dumps(M.DEFAULT))
    for block, values in overrides.items():
        cfg[block].update(values)
    mm = M()
    mm._apply(cfg)
    return mm


mm = apply(dsu={"aliases": {"GL": "L", "C": "HOME"}})
check("valid aliases accepted", mm.dsu_aliases == {"GL": "L", "C": "HOME"}, mm.dsu_aliases)
mm = apply(dsu={"aliases": {"A": "L"}})
check("non-aliasable source rejected", mm.dsu_aliases == {} and "aliasable" in mm.last_warning)
mm = apply(dsu={"aliases": {"GL": "NOPE"}})
check("unknown alias target rejected", mm.dsu_aliases == {} and "NOPE" in mm.last_warning)
mm = apply(dsu={"aliases": {"GL": None}})
check("null alias ignored", mm.dsu_aliases == {})

mm = apply(motion={"enabled": True})
check("motion without offsets disables itself", mm.motion_enabled is False)
check("motion without offsets warns", "offset" in (mm.last_warning or ""), mm.last_warning)
mm = apply(motion={"enabled": True, "accel_offset": 12, "gyro_offset": 18})
check("motion with offsets enabled", mm.motion_enabled is True)
try:
    apply(motion={"accel_offset": -1})
    check("negative offset rejected", False)
except ValueError:
    check("negative offset rejected", True)

mm = apply(sticks={"calibration": {"auto_center": False, "half_range": 1800}})
check("calibration config read", mm.stick_half_range == 1800 and not mm.stick_auto_center)
mm = apply(sticks={"calibration": {"half_range": 99999}})
check("out-of-range half_range clamped", mm.stick_half_range == 1500)
mm = apply(sticks={"deadzone": 0.9, "saturation": 0.2})
check("saturation below deadzone falls back",
      mm.stick_deadzone == 0.08 and mm.stick_saturation == 0.95)

# ============ motion decoding ============
print("== motion decode ==")
mm = apply(motion={"enabled": True, "accel_offset": 12, "gyro_offset": 18,
                   "accel_scale": 1.0, "gyro_scale": 2.0})
br = S2B.ControllerBridge(mm)


def report(accel=(0, 0, 0), gyro=(0, 0, 0), length=112):
    d = bytearray(length)
    d[5] = d[8] = STICK_RAW_CENTER & 0xFF
    d[6] = d[9] = ((STICK_RAW_CENTER >> 8) & 0x0F) | ((STICK_RAW_CENTER & 0x0F) << 4)
    d[7] = d[10] = (STICK_RAW_CENTER >> 4) & 0xFF
    if length >= 18:
        struct.pack_into("<3h", d, 12, *accel)
    if length >= 24:
        struct.pack_into("<3h", d, 18, *gyro)
    return bytes(d)


br._on_data(None, report(accel=(100, -200, 300), gyro=(10, -20, 30)))
check("accel decoded", br.last_state.accel == (100.0, -200.0, 300.0), br.last_state.accel)
check("gyro decoded and scaled", br.last_state.gyro == (20.0, -40.0, 60.0), br.last_state.gyro)

mm_off = apply(motion={"enabled": False, "accel_offset": 12, "gyro_offset": 18})
br_off = S2B.ControllerBridge(mm_off)
br_off._on_data(None, report(accel=(100, 200, 300)))
check("motion disabled stays zeroed", not br_off.last_state.has_motion)

# a short report must not blow up the int16 unpack
br._on_data(None, report(accel=(1, 2, 3), length=14))
check("short report survives motion decode", br.last_state is not None)
check("short report yields zero accel", br.last_state.accel == (0.0, 0.0, 0.0),
      br.last_state.accel)

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
    sys.exit(1)
print("ALL STATE TESTS PASSED")

# ============ command protocol (BlueRetro RE, verified on hardware) ============
print("== controller commands ==")
import controller_commands as cc

led = cc.player_lights_command(0x01)
check("led frame is 16 bytes", len(led) == cc.FRAME_LEN, len(led))
check("led frame matches captured payload",
      led.hex() == "09910007000800000100000000000000", led.hex())
check("led pattern byte lands at index 8", cc.player_lights_command(0x0F)[8] == 0x0F)
check("led pattern masked to 4 bits", cc.player_lights_command(0xFF)[8] == 0x0F)

spi = cc.spi_read_command(0x1FC040, 0x0B)
check("spi frame matches captured payload",
      spi.hex() == "02910004000800000b7e000040c01f00", spi.hex())
check("spi address is little-endian",
      cc.spi_read_command(0x000130A8, 9)[12:16].hex() == "a8300100")

# reply framing, taken from a real controller (result byte 0x78, not 0xf8)
reply = bytes.fromhex(
    "020100041078000018000000a8300100b347837616612e6664" + "ff" * 15
)
parsed = cc.parse_spi_reply(reply)
check("spi reply parses", parsed is not None)
addr, data = parsed
check("spi reply address echoes", addr == 0x000130A8, hex(addr))
check("spi reply data extracted", data.hex().startswith("b347837616612e6664"))
check("non-spi reply rejected", cc.parse_spi_reply(bytes.fromhex("0901000710780000")) is None)
check("short reply rejected", cc.parse_spi_reply(b"\x02\x01") is None)

block = cc.decode_stick_block(data)
check("stick block decodes", block is not None)
centre, tpos, tneg = block
check("decoded centre", centre == (1971, 2100), centre)
check("decoded +travel", tpos == (1654, 1553), tpos)
check("decoded -travel", tneg == (1582, 1606), tneg)
# the decode must reproduce the extremes seen in the 2455-packet capture
check("predicts measured LX min", centre[0] - tneg[0] == 389)
check("predicts measured LY max", abs((centre[1] + tpos[1]) - 3656) <= 5)
check("erased block rejected", cc.decode_stick_block(b"\xff" * 9) is None)
check("implausible block rejected", cc.decode_stick_block(bytes(9)) is None)
check("short block rejected", cc.decode_stick_block(b"\x01\x02") is None)

# ============ factory calibration applied to the sticks ============
print("== factory calibration ==")
left = ((1971, 2100), (1654, 1553), (1582, 1606))
right = ((2144, 2126), (1521, 1631), (1617, 1631))
cal = StickCalibration()
check("applies factory blocks", cal.apply_factory(left, right) is True)
check("marked as factory", cal.from_factory and cal.calibrated)
check("centre adopted", cal.centers['lx'] == 1971 and cal.centers['ry'] == 2126)
check("asymmetric travel kept",
      cal.travel_pos['lx'] == 1654 and cal.travel_neg['lx'] == 1582)
check("centre reads zero", close(cal.value('lx', 1971), 0.0))
check("measured LX min reaches -1.0", cal.value('lx', 389) == -1.0)
check("measured RX max reaches ~1.0", cal.value('rx', 3666) >= 0.99, cal.value('rx', 3666))
check("beyond calibrated travel clamps", cal.value('lx', 4095) == 1.0)
# asymmetry must actually matter: same distance either side reads differently
pos = cal.value('lx', 1971 + 1500)
neg = cal.value('lx', 1971 - 1500)
check("asymmetric travel changes the scale", abs(pos) != abs(neg), (pos, neg))

cal_none = StickCalibration()
check("no blocks -> no change", cal_none.apply_factory(None, None) is False)
check("no blocks -> not factory", not cal_none.from_factory)
cal_one = StickCalibration()
check("one block still applies", cal_one.apply_factory(left, None) is True)
check("unset side keeps default centre", cal_one.centers['rx'] == STICK_RAW_CENTER)

# a reset must not discard calibration read from flash
cal.reset()
check("reset keeps factory travel", cal.travel_pos['lx'] == 1654)
