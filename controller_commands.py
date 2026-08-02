"""
Switch 2 Pro Controller command protocol
========================================

Reverse engineering by darthcloud and german77:
https://github.com/darthcloud/BlueRetro/issues/1249

Commands are written to a dedicated characteristic and answered
asynchronously on another. Every command shares one 16-byte frame:

    <report_type> 0x91 0x00 <command> 0x00 0x08 0x00 0x00 <8 byte payload>

0x91 marks a request; replies come back with 0x01 in that slot.

Verified against real hardware: SPI reads return the factory stick
calibration, and the player-lights command visibly changes the LEDs.
"""

import logging
import struct

log = logging.getLogger(__name__)

# ATT handle 0x0014 — takes commands with no padding.
COMMAND_CHAR = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"
# ATT handle 0x0016 — same commands, but needs 33 leading zero bytes.
COMMAND_CHAR_PADDED = "3dacbc7e-6955-40b5-8eaf-6f9809e8b379"
# ATT handle 0x001a — async replies for either command characteristic.
RESPONSE_CHAR = "c765a961-d9d8-4d36-a20a-5315b111836a"

REPORT_SPI = 0x02
REPORT_PLAYER_LIGHTS = 0x09

CMD_SPI_READ = 0x04
CMD_SET_PLAYER_LIGHTS = 0x07

MODE_REQUEST = 0x91
MODE_REPLY = 0x01

# The result byte differs between firmware revisions (0xf8 upstream, 0x78 on
# the unit this was developed against), so replies are validated by checking
# that the requested address is echoed back rather than by matching it.
FRAME_LEN = 16

# Factory stick calibration. Joy-Con 2 units only populate the left block;
# the Pro Controller 2 uses both.
FACTORY_STICK_LEFT = 0x000130A8
FACTORY_STICK_RIGHT = 0x000130E8
STICK_BLOCK_LEN = 9


def _frame(report_type, command, payload=b""):
    head = bytes([report_type, MODE_REQUEST, 0x00, command, 0x00, 0x08, 0x00, 0x00])
    return (head + payload).ljust(FRAME_LEN, b"\x00")


def spi_read_command(address, length):
    payload = bytes([length, 0x7E, 0x00, 0x00,
                     address & 0xFF, (address >> 8) & 0xFF,
                     (address >> 16) & 0xFF, (address >> 24) & 0xFF])
    return _frame(REPORT_SPI, CMD_SPI_READ, payload)


def player_lights_command(pattern):
    """One bit per LED, so 0x01 lights player 1 and 0x0F lights all four."""
    return _frame(REPORT_PLAYER_LIGHTS, CMD_SET_PLAYER_LIGHTS,
                  bytes([pattern & 0x0F]))


def parse_spi_reply(reply):
    """Returns (address, data) for a well-formed SPI reply, else None."""
    if len(reply) < FRAME_LEN or reply[0] != REPORT_SPI:
        return None
    if reply[3] != CMD_SPI_READ:
        return None
    length = reply[8]
    address = int.from_bytes(reply[12:16], "little")
    return address, reply[FRAME_LEN:FRAME_LEN + length]


# --- input report 0x05 ---
#
# A second input format, and the only one that carries motion. Offsets below
# are into the report *body*, i.e. with the USB HID report-id byte already
# stripped, so they are one lower than the numbers in SDL's driver.
#
# This is a different layout from the report the Bluetooth path reads, not an
# extension of it: the sticks move, the counter widens to four bytes, and the
# buttons become a single u32. Hence a separate decoder rather than extra
# fields on the existing one.

MOTION_REPORT_ID = 0x05

R05_COUNTER = 0          # u32
R05_BUTTONS = 4          # u32
R05_LEFT_STICK = 10      # 3 bytes, same 12-bit packing as report 0x09
R05_RIGHT_STICK = 13
R05_BATTERY_MV = 31      # u16, millivolts
R05_SENSOR_TIMESTAMP = 0x2A   # u32; the only trustworthy IMU liveness signal
R05_ACCEL = 0x30         # X, then Z (negated), then Y — three i16
R05_GYRO = 0x36          # X, then Y (negated), then Z — three i16
R05_MIN_LEN = R05_GYRO + 6

# One g over a +/-8g 16-bit range, and degrees/sec per count. The gyro figure
# is SDL's 34.8 rad/s full scale converted to deg/s, which lands on the 1/16.4
# these mappings already defaulted to.
ACCEL_COUNTS_PER_G = 4096.0
GYRO_COUNTS_PER_DPS = 16.4

# Report 0x05 packs every button into one u32. Bit numbers per ndeadly's
# hid_reports.md, cross-checked against SDL and murphyjt/wavebird.
R05_BUTTON_BITS = (
    (0, 'Y'), (1, 'X'), (2, 'B'), (3, 'A'),
    (6, 'R'), (7, 'ZR'),
    (8, '-'), (9, '+'), (10, 'RS'), (11, 'LS'),
    (12, 'HOME'), (13, 'CAPT'), (14, 'C'),
    (16, 'DDOWN'), (17, 'DUP'), (18, 'DRIGHT'), (19, 'DLEFT'),
    (22, 'L'), (23, 'ZL'),
    (24, 'GR'), (25, 'GL'),
)


def decode_report05_motion(body, gyro_bias_dps=(0.0, 0.0, 0.0)):
    """(accel in g, gyro in deg/s) from a report 0x05 body.

    Axis order and the two negations follow SDL. Returns zeros when the body
    is too short rather than raising, since a truncated report should cost a
    frame of motion and not the whole input stream.
    """
    if len(body) < R05_MIN_LEN:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    ax, az, ay = struct.unpack_from("<3h", body, R05_ACCEL)
    gx, gy, gz = struct.unpack_from("<3h", body, R05_GYRO)
    accel = (ax / ACCEL_COUNTS_PER_G,
             ay / ACCEL_COUNTS_PER_G,
             -az / ACCEL_COUNTS_PER_G)
    gyro = (gx / GYRO_COUNTS_PER_DPS - gyro_bias_dps[0],
            -gy / GYRO_COUNTS_PER_DPS - gyro_bias_dps[1],
            gz / GYRO_COUNTS_PER_DPS - gyro_bias_dps[2])
    return accel, gyro


def report05_sensor_timestamp(body):
    """The IMU's own clock. Static means motion is off, whatever accel says."""
    if len(body) < R05_SENSOR_TIMESTAMP + 4:
        return 0
    return int.from_bytes(
        body[R05_SENSOR_TIMESTAMP:R05_SENSOR_TIMESTAMP + 4], "little")


def report05_battery_mv(body):
    if len(body) < R05_BATTERY_MV + 2:
        return 0
    return int.from_bytes(body[R05_BATTERY_MV:R05_BATTERY_MV + 2], "little")


# --- HD rumble ---
#
# Rumble is the mirror image of the command channel. Commands only work on the
# vendor interface and are silently ignored as HID output reports; rumble only
# works as a HID output report and is *acknowledged* but ignored when sent as
# command 0x0A/0x08 on the vendor interface. Verified on hardware both ways
# round — the ACK for the vendor-interface version is what makes it misleading.

RUMBLE_REPORT_ID = 0x02
RUMBLE_REPORT_LEN = 64
# Carrier frequencies for the two actuators. These are what SDL settled on.
RUMBLE_HIGH_FREQ = 0x187
RUMBLE_LOW_FREQ = 0x112
# Amplitude is clamped well below full scale. SDL's comment is worth repeating:
# the motors are strong enough that it is "a game controller, not a massage
# device", and driving them flat out may not be good for the hardware.
RUMBLE_MAX_AMPLITUDE = 29000
# The motors stop on their own if not refreshed, so an active effect has to be
# re-sent at roughly this interval for as long as it should be felt.
RUMBLE_RESEND_INTERVAL = 0.012
# Offset of the right actuator's block; it mirrors the left one byte for byte.
_RUMBLE_RIGHT_OFFSET = 0x11


def scale_amplitude(amplitude):
    """A 0-65535 amplitude mapped into the range the motors are driven at."""
    clamped = max(0, min(0xFFFF, int(amplitude)))
    return (clamped * RUMBLE_MAX_AMPLITUDE) // 0xFFFF


def encode_hd_rumble(high_amplitude, low_amplitude,
                     high_freq=RUMBLE_HIGH_FREQ, low_freq=RUMBLE_LOW_FREQ):
    """One five-byte HD rumble frame.

    Two frequency/amplitude pairs bit-packed together: 12-bit frequencies and
    10-bit amplitudes straddling byte boundaries, so every byte carries parts
    of more than one field.
    """
    return bytes([
        high_freq & 0xFF,
        ((high_amplitude >> 4) & 0xFC) | ((high_freq >> 8) & 0x03),
        ((high_amplitude >> 12) | (low_freq << 4)) & 0xFF,
        (low_amplitude & 0xC0) | ((low_freq >> 4) & 0x3F),
        (low_amplitude >> 8) & 0xFF,
    ])


# Bluetooth carries rumble on its own characteristic rather than through the
# command channel. Write-without-response, so nothing acknowledges a bad frame.
VIBRATION_CHAR = "cc483f51-9258-427d-a939-630c31f72b05"


def ble_rumble_payload(low_amplitude, high_amplitude, sequence):
    """Bluetooth vibration write: 0x00, then one 16-byte block per actuator.

    The block is the same shape as the wired report — a sequence byte followed
    by a single HD frame. Tested against the alternatives on hardware: three
    repeated frames per block also works, but dropping the sequence byte does
    nothing at all, so it is required rather than padding.
    """
    block = bytearray(16)
    block[0] = 0x50 | (sequence & 0x0F)
    block[1:6] = encode_hd_rumble(scale_amplitude(high_amplitude),
                                  scale_amplitude(low_amplitude))
    return bytes([0x00]) + bytes(block) + bytes(block)


def rumble_report(low_amplitude, high_amplitude, sequence):
    """USB HID output report 0x02 driving both actuators.

    Amplitudes are 0-65535 and get scaled down here, so callers work in the
    same units DSU and SDL use. Zero on both stops the motors.
    """
    body = bytearray(RUMBLE_REPORT_LEN)
    body[0] = RUMBLE_REPORT_ID
    body[1] = 0x50 | (sequence & 0x0F)
    body[2:7] = encode_hd_rumble(scale_amplitude(high_amplitude),
                                 scale_amplitude(low_amplitude))
    # Right actuator repeats the left block, sequence byte included.
    body[_RUMBLE_RIGHT_OFFSET:_RUMBLE_RIGHT_OFFSET + 6] = body[1:7]
    return bytes(body)


def unpack_pair(chunk):
    """Three bytes -> two 12-bit values, same packing the sticks use."""
    return (chunk[0] | ((chunk[1] & 0x0F) << 8),
            ((chunk[1] & 0xF0) >> 4) | (chunk[2] << 4))


def decode_stick_block(data):
    """centre, +travel, -travel — three 12-bit pairs, nine bytes.

    Returns None when the block is erased (all 0xFF) or implausible, so a
    controller with no stored calibration falls back to defaults instead of
    being fed nonsense.
    """
    if data is None or len(data) < STICK_BLOCK_LEN:
        return None
    block = data[:STICK_BLOCK_LEN]
    if all(b == 0xFF for b in block):
        return None
    centre = unpack_pair(block[0:3])
    travel_pos = unpack_pair(block[3:6])
    travel_neg = unpack_pair(block[6:9])
    if not all(1200 < c < 2900 for c in centre):
        return None
    if not all(600 < t <= 2048 for t in travel_pos + travel_neg):
        return None
    return centre, travel_pos, travel_neg
