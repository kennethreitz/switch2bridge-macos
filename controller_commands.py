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
