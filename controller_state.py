"""
Neutral controller state for Switch2 Bridge
===========================================

The BLE parser decodes a report into a `ControllerState`; every output
backend (DSU, keyboard) renders that same state its own way. Keeping this
in the middle means the wire format is decoded exactly once and backends
never reach into raw bytes.
"""

import logging
import math
import statistics
import time

log = logging.getLogger(__name__)

# DSU clients compare the stamp on a packet against their own clock to
# measure the hop, so it has to be a clock both processes agree on.
# CLOCK_UPTIME_RAW is nanoseconds since boot — the same thing
# mach_absolute_time reads, and so the same thing Swift's
# DispatchTime.uptimeNanoseconds reports. time.monotonic() is *not* usable
# here: on macOS its reference point is the start of the calling process,
# so two processes reading it get numbers hours apart.
_UPTIME_CLOCK = getattr(time, 'CLOCK_UPTIME_RAW', None)


def monotonic_us():
    """Microseconds on a clock other processes on this machine can read."""
    if _UPTIME_CLOCK is not None:
        return time.clock_gettime_ns(_UPTIME_CLOCK) // 1000
    return time.monotonic_ns() // 1000

# Every button the Switch 2 Pro Controller reports, in a stable order.
# GL/GR are the Switch 2 grip buttons, C is the new Switch 2 button.
BUTTON_NAMES = (
    'A', 'B', 'X', 'Y',
    'L', 'R', 'ZL', 'ZR',
    '+', '-', 'HOME', 'CAPT', 'C',
    'LS', 'RS', 'GL', 'GR',
    'DUP', 'DDOWN', 'DLEFT', 'DRIGHT',
)

# Sticks report 12-bit unsigned values, nominally centred at half scale.
STICK_RAW_CENTER = 2048
# Measured travel is nowhere near the full 2048: a fully deflected stick
# moves 1514-1767 counts off centre depending on axis and direction.
# Dividing by 2048 caps the output at ~0.80-0.86, so the stick never reads
# fully pushed. This sits just under the *weakest* measured direction so
# every axis can still reach full scale; anything beyond it clamps.
STICK_RAW_HALF_RANGE = 1500

# Resting centre also drifts per unit (measured up to 109 counts off 2048),
# so it is learned at connect time rather than assumed.
AXES = ('lx', 'ly', 'rx', 'ry')


class ControllerState:
    """One decoded input report.

    Sticks are normalised floats in [-1.0, 1.0] with +Y up and +X right.
    Motion is (x, y, z) accelerometer in g and (pitch, yaw, roll) gyro in
    deg/s — zeros when the report carries no motion or decoding is off.
    """

    __slots__ = ('buttons', 'lx', 'ly', 'rx', 'ry', 'accel', 'gyro', 'timestamp_us')

    def __init__(self, buttons=None, lx=0.0, ly=0.0, rx=0.0, ry=0.0,
                 accel=(0.0, 0.0, 0.0), gyro=(0.0, 0.0, 0.0), timestamp_us=0):
        self.buttons = buttons if buttons is not None else {}
        self.lx, self.ly, self.rx, self.ry = lx, ly, rx, ry
        self.accel = accel
        self.gyro = gyro
        self.timestamp_us = timestamp_us

    def pressed(self, name):
        return bool(self.buttons.get(name))

    @property
    def has_motion(self):
        return any(self.accel) or any(self.gyro)

    def __repr__(self):
        held = ",".join(n for n in BUTTON_NAMES if self.buttons.get(n)) or "-"
        return (
            f"<ControllerState [{held}] "
            f"L=({self.lx:+.2f},{self.ly:+.2f}) R=({self.rx:+.2f},{self.ry:+.2f})>"
        )


def axis_from_raw(raw, center=STICK_RAW_CENTER, half_range=STICK_RAW_HALF_RANGE):
    """12-bit stick reading → [-1.0, 1.0], clamped."""
    if half_range <= 0:
        return 0.0
    return max(-1.0, min(1.0, (raw - center) / float(half_range)))


class StickCalibration:
    """Per-axis centre and travel.

    The nominal 2048 centre is wrong on real hardware — every unit rests a
    little off-centre, which shows up as permanent drift. Rather than ship a
    fudge factor, the resting centre is measured from the first few reports
    after connecting, and only accepted when those reports agree closely
    enough that the sticks were plainly untouched.
    """

    # Reports to average before locking a centre in
    SAMPLE_COUNT = 60
    # Reject the sample if the stick moved more than this during it, which
    # means the user was holding a stick while we tried to calibrate
    MAX_SPREAD = 200

    def __init__(self, center=STICK_RAW_CENTER, half_range=STICK_RAW_HALF_RANGE,
                 auto_center=True):
        self.default_center = center
        self.half_range = half_range
        self.auto_center = auto_center
        self.centers = {axis: center for axis in AXES}
        # Per-direction travel, overwritten by the controller's own
        # calibration when we can read it.
        self.travel_pos = {axis: half_range for axis in AXES}
        self.travel_neg = {axis: half_range for axis in AXES}
        self.from_factory = False
        self.calibrated = False
        self._samples = {axis: [] for axis in AXES}

    def reset(self):
        """Re-run auto-centering on the next reports.

        Factory travel values survive a reset: they come from the
        controller's flash, not from watching the sticks.
        """
        self.centers = {axis: self.default_center for axis in AXES}
        self.calibrated = False
        self._samples = {axis: [] for axis in AXES}

    def apply_factory(self, left, right):
        """Adopt calibration blocks read out of the controller's SPI flash.

        Each block is (centre, +travel, -travel) as (x, y) pairs. Travel is
        asymmetric on real hardware — one direction routinely reaches ~100
        counts further than the other — so each direction is stored
        separately rather than averaged into one half-range.
        """
        blocks = {'l': left, 'r': right}
        applied = []
        for side, block in blocks.items():
            if block is None:
                continue
            centre, travel_pos, travel_neg = block
            for index, coord in enumerate(('x', 'y')):
                axis = f"{side}{coord}"
                self.centers[axis] = centre[index]
                self.travel_pos[axis] = travel_pos[index]
                self.travel_neg[axis] = travel_neg[index]
                applied.append(axis)
        if applied:
            self.from_factory = True
            self.calibrated = True  # a stored centre beats a sampled one
            log.info(
                "factory stick calibration applied for %s", ", ".join(applied)
            )
        return bool(applied)

    def observe(self, raws):
        """Feed one report's raw axis values while calibration is pending."""
        if self.calibrated or not self.auto_center:
            return
        for axis in AXES:
            self._samples[axis].append(raws[axis])
        if len(self._samples['lx']) < self.SAMPLE_COUNT:
            return

        centers, spreads = {}, {}
        for axis in AXES:
            values = self._samples[axis]
            centers[axis] = statistics.median(values)
            spreads[axis] = max(values) - min(values)

        worst = max(spreads.values())
        if worst > self.MAX_SPREAD:
            # Sticks were being moved — drop the sample and try again
            log.info("stick calibration deferred (spread %d > %d)",
                     worst, self.MAX_SPREAD)
            self._samples = {axis: [] for axis in AXES}
            return

        self.centers = centers
        self.calibrated = True
        log.info(
            "stick centres calibrated: %s (spread %d)",
            ", ".join(f"{a}={centers[a]:.0f}" for a in AXES), worst,
        )

    def value(self, axis, raw):
        """Raw reading -> [-1.0, 1.0], using per-direction travel."""
        delta = raw - self.centers[axis]
        span = self.travel_pos[axis] if delta >= 0 else self.travel_neg[axis]
        if span <= 0:
            return 0.0
        return max(-1.0, min(1.0, delta / float(span)))

    def observe_only_centre(self):
        """True when centring came from sampling rather than from flash."""
        return self.calibrated and not self.from_factory


def shape_stick(x, y, deadzone=0.0, saturation=1.0):
    """Apply a *radial* deadzone and saturation rescale to one stick.

    Real controllers deadzone on the vector magnitude, not per axis — a
    per-axis deadzone leaves a cross-shaped dead region and makes diagonals
    snap. Below `deadzone` the stick reads centred; at or above `saturation`
    it reads fully deflected, with the range in between rescaled to use the
    whole travel. The result is clamped to the unit circle.
    """
    magnitude = math.hypot(x, y)
    if magnitude <= deadzone or magnitude == 0.0:
        return 0.0, 0.0
    span = saturation - deadzone
    if span <= 1e-6:
        scaled = 1.0
    else:
        scaled = min((magnitude - deadzone) / span, 1.0)
    return x / magnitude * scaled, y / magnitude * scaled
