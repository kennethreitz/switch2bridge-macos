"""
Bluetooth pairing for the Switch 2 Pro Controller
=================================================

The controller does not use Bluetooth SMP. Nintendo runs their own key exchange
over the same command channel as the rest of the protocol — command `0x15` —
and the controller commits the result to its flash, so that afterwards it will
reconnect to a known host instead of advertising for any console to claim.

Four steps:

    0x15/0x01   exchange addresses   two host addresses, primary and secondary
    0x15/0x04   exchange keys        send A1, receive B1, LTK = A1 xor rev(B1)
    0x15/0x02   confirm LTK          send A2, expect AES128-ECB(LTK, A2) back
    0x15/0x03   finalise             commits host + LTK to flash 0x001FA000

Only the last step writes anything. The first three are a pure exchange, and
because step three is a cryptographic check, a successful confirm proves the
whole thing worked without touching the controller's flash — which makes a dry
run genuinely meaningful rather than a partial rehearsal.

Every multi-byte field goes on the wire byte-reversed. Callers work in natural
(MSB-first) order throughout; the frame builders handle the reversal, so it is
never something two layers can disagree about.

The exchange is transport-agnostic: it is conducted over whichever channel the
caller provides, and the host address it stores comes from the payload rather
than the link, which is how a console pairs a controller over a cable.

Reverse engineering by ndeadly (`commands.md`, `bluetooth_interface.md`);
frame layouts cross-checked against murphyjt/wavebird.
"""

import ctypes
import ctypes.util
import logging
import secrets

log = logging.getLogger(__name__)

CMD_PAIRING = 0x15
SUB_EXCHANGE_ADDRESSES = 0x01
SUB_CONFIRM_LTK = 0x02
SUB_FINALISE = 0x03
SUB_EXCHANGE_KEYS = 0x04

TRANSPORT_USB = 0x00
TRANSPORT_BLUETOOTH = 0x01

ADDRESS_LEN = 6
KEY_LEN = 16
# Replies open with the same 8-byte header shape the other commands use.
ACK_HEADER_LEN = 8
# Where the controller keeps host addresses and their key once finalised.
FLASH_PAIRING_BLOCK = 0x001FA000


class PairingError(Exception):
    """Raised when a step is unanswered, malformed, or fails its crypto check."""


# --- AES-128 ECB ---------------------------------------------------------
# Only ever used on a single 16-byte block for the LTK confirmation, which is
# what makes ECB the right mode here rather than a hazard. CommonCrypto ships
# with macOS, so this avoids a dependency for one block of AES.

_kCCEncrypt = 0
_kCCAlgorithmAES = 0
_kCCOptionECBMode = 0x0002
_kCCSuccess = 0

_libsystem = ctypes.CDLL(ctypes.util.find_library("System"))


def aes128_ecb(key, block):
    """Encrypt one 16-byte block. Returns 16 bytes."""
    if len(key) != KEY_LEN or len(block) != KEY_LEN:
        raise ValueError("AES-128 needs a 16-byte key and a 16-byte block")
    out = ctypes.create_string_buffer(KEY_LEN)
    written = ctypes.c_size_t(0)
    status = _libsystem.CCCrypt(
        ctypes.c_uint32(_kCCEncrypt),
        ctypes.c_uint32(_kCCAlgorithmAES),
        ctypes.c_uint32(_kCCOptionECBMode),
        ctypes.c_char_p(bytes(key)), ctypes.c_size_t(KEY_LEN),
        None,
        ctypes.c_char_p(bytes(block)), ctypes.c_size_t(KEY_LEN),
        out, ctypes.c_size_t(KEY_LEN),
        ctypes.byref(written),
    )
    if status != _kCCSuccess or written.value != KEY_LEN:
        raise PairingError(f"AES failed (status {status})")
    return out.raw[:KEY_LEN]


# --- addresses -----------------------------------------------------------

def parse_address(text):
    """"C0:C7:DB:11:C4:3F" -> six bytes in natural order."""
    parts = str(text).replace("-", ":").split(":")
    if len(parts) != ADDRESS_LEN:
        raise ValueError(f"not a Bluetooth address: {text!r}")
    try:
        return bytes(int(p, 16) for p in parts)
    except ValueError:
        raise ValueError(f"not a Bluetooth address: {text!r}") from None


def format_address(address):
    return ":".join(f"{b:02X}" for b in address)


def secondary_address(primary):
    """The console always sends a second address: the first, LSB minus one.

    Both entries share one key. Mirroring that is what makes the controller
    treat us the way it treats a console rather than a special case.
    """
    if len(primary) != ADDRESS_LEN:
        raise ValueError("address must be 6 bytes")
    return primary[:5] + bytes([(primary[5] - 1) & 0xFF])


# --- frames --------------------------------------------------------------

def _frame(subcommand, payload, transport):
    return bytes([CMD_PAIRING, 0x91, transport, subcommand,
                  0x00, len(payload), 0x00, 0x00]) + payload


def exchange_addresses_frame(primary, secondary, transport=TRANSPORT_USB):
    payload = bytes([0x00, 0x02]) + primary[::-1] + secondary[::-1]
    return _frame(SUB_EXCHANGE_ADDRESSES, payload, transport)


def exchange_keys_frame(host_key, transport=TRANSPORT_USB):
    return _frame(SUB_EXCHANGE_KEYS, bytes([0x00]) + host_key[::-1], transport)


def confirm_ltk_frame(challenge, transport=TRANSPORT_USB):
    return _frame(SUB_CONFIRM_LTK, bytes([0x00]) + challenge[::-1], transport)


def finalise_frame(transport=TRANSPORT_USB):
    return _frame(SUB_FINALISE, bytes([0x00]), transport)


# --- replies -------------------------------------------------------------

def reply_body(reply, subcommand, step):
    """Validate a reply's echoed header and return everything after it.

    Validated by the echoed command and subcommand rather than the result
    byte, which is 0xF8 over USB and 0x78 over Bluetooth on the same unit.
    """
    if not reply:
        raise PairingError(f"{step}: no reply")
    if len(reply) < ACK_HEADER_LEN:
        raise PairingError(f"{step}: reply too short ({len(reply)} bytes)")
    if reply[0] != CMD_PAIRING or reply[3] != subcommand:
        raise PairingError(
            f"{step}: unexpected reply header {reply[:ACK_HEADER_LEN].hex()}"
        )
    return reply[ACK_HEADER_LEN:]


def _key_from_body(body, step):
    """Pairing bodies are a status byte then a 16-byte value, wire order."""
    if len(body) < 1 + KEY_LEN:
        raise PairingError(f"{step}: body too short ({len(body)} bytes)")
    return body[1:1 + KEY_LEN]


def derive_ltk(host_key, peer_key_wire):
    """LTK = A1 xor reverse(B1). Both sides end up holding the same key."""
    peer = peer_key_wire[::-1]
    return bytes(a ^ b for a, b in zip(host_key, peer))


# --- the exchange --------------------------------------------------------

def pair(send, host_address, transport=TRANSPORT_USB, commit=False,
         rng=secrets.token_bytes):
    """Run the pairing exchange.

    `send` takes one command frame and returns the controller's reply. It is
    whatever channel the caller has — a bulk endpoint over USB, or a GATT
    write plus notification over Bluetooth.

    With `commit` false the first three steps run and the finalise is skipped,
    which verifies the whole exchange cryptographically while leaving the
    controller's flash untouched. Nothing is persisted until commit is asked
    for explicitly, because it is not reversible from here.

    Returns a dict with the derived key and the addresses that were offered.
    """
    if len(host_address) != ADDRESS_LEN:
        raise ValueError("host address must be 6 bytes")
    secondary = secondary_address(host_address)

    # Step 1 — offer the host addresses. The body tells us the controller's
    # own address, which we have no use for; what matters is that it accepted.
    reply_body(
        send(exchange_addresses_frame(host_address, secondary, transport)),
        SUB_EXCHANGE_ADDRESSES, "exchange addresses",
    )

    # Step 2 — trade key halves and combine them.
    host_key = rng(KEY_LEN)
    body = reply_body(
        send(exchange_keys_frame(host_key, transport)),
        SUB_EXCHANGE_KEYS, "exchange keys",
    )
    ltk = derive_ltk(host_key, _key_from_body(body, "exchange keys"))

    # Step 3 — prove both sides derived the same key before committing to it.
    challenge = rng(KEY_LEN)
    body = reply_body(
        send(confirm_ltk_frame(challenge, transport)),
        SUB_CONFIRM_LTK, "confirm LTK",
    )
    answer = _key_from_body(body, "confirm LTK")
    expected = aes128_ecb(ltk, challenge)
    if answer != expected:
        raise PairingError(
            "LTK confirmation mismatch — the controller derived a different "
            f"key (expected {expected.hex()}, got {answer.hex()})"
        )

    result = {
        "ltk": ltk,
        "primary_address": host_address,
        "secondary_address": secondary,
        "committed": False,
    }
    if not commit:
        log.info("pairing verified but not committed (dry run)")
        return result

    # Step 4 — the only step that writes. Everything above is verified by now.
    reply_body(send(finalise_frame(transport)), SUB_FINALISE, "finalise")
    result["committed"] = True
    log.info("pairing committed to flash %#010x for host %s",
             FLASH_PAIRING_BLOCK, format_address(host_address))
    return result


def local_bluetooth_address():
    """This Mac's Bluetooth adapter address, or None if it cannot be read.

    CoreBluetooth does not expose the local adapter, so this goes through
    IOBluetooth, which does.
    """
    try:
        import objc
        objc.loadBundle(
            "IOBluetooth", globals(),
            bundle_path="/System/Library/Frameworks/IOBluetooth.framework",
        )
        controller = objc.lookUpClass("IOBluetoothHostController").defaultController()
        if controller is None:
            return None
        return parse_address(controller.addressAsString())
    except Exception as e:
        log.warning("could not read the local Bluetooth address: %s", e)
        return None
