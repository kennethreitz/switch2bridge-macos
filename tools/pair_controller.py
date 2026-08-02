#!/usr/bin/env python3
"""
Pair the controller with this Mac
=================================

The controller does not use Bluetooth SMP. Nintendo runs their own key exchange
over the command channel, and the controller stores the result in flash so it
will reconnect to a known host instead of advertising for any console to claim.

Conducted over USB here, which is also how a console pairs a controller over a
cable: the host address is carried in the payload, so the link it travels over
does not decide what gets stored.

    python3 tools/pair_controller.py            # dry run, nothing is written
    python3 tools/pair_controller.py --commit   # actually pair

**`--commit` writes the controller's flash.** The pad keeps two host entries,
so committing may displace the one your Switch 2 uses, and you would then need
to re-pair it with the console. The dry run is not a partial rehearsal: step
three is a cryptographic check, so if it passes, the exchange is proven correct
without anything being written.

Quit "Switch2 Bridge.app" first — it holds the command interface.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import controller_pairing as cp  # noqa: E402
import usb_transport  # noqa: E402

READ_TIMEOUT_MS = 1000


def make_sender(device, verbose):
    """One request/reply over the vendor bulk endpoint."""
    def send(frame):
        if verbose:
            print(f"      -> {frame.hex()}")
        device.write(usb_transport.ENDPOINT_OUT, frame, READ_TIMEOUT_MS)
        # Replies to other traffic can be queued ahead of ours, so read until
        # a pairing reply turns up rather than trusting the first packet.
        for _attempt in range(6):
            try:
                reply = bytes(device.read(usb_transport.ENDPOINT_IN, 96,
                                          READ_TIMEOUT_MS))
            except Exception:
                return b""
            if verbose:
                print(f"      <- {reply.hex()}")
            if reply and reply[0] == cp.CMD_PAIRING:
                return reply
        return b""
    return send


def main(args):
    if not usb_transport.dependencies_available():
        print("Needs pyusb and hidapi:\n"
              "    pip install pyusb hidapi\n"
              "    brew install libusb")
        return 1
    if not usb_transport.is_connected():
        print("❌ No Switch 2 Pro Controller on USB. Use a data cable — "
              "charge-only cables do not enumerate.")
        # A Switch 1 pad is easy to grab by mistake, and "not found" is a
        # confusing thing to read while looking at a plugged-in controller.
        try:
            import hid
            if hid.enumerate(0x057E, 0x2009):
                print("   A Switch 1 Pro Controller (057e:2009) is plugged in. "
                      "The Pro 2 is 057e:2069 — this is the other pad.")
        except Exception:
            pass
        return 1

    if args.host_address:
        host = cp.parse_address(args.host_address)
    else:
        host = cp.local_bluetooth_address()
        if host is None:
            print("❌ Could not read this Mac's Bluetooth address. Pass it "
                  "with --host-address.")
            return 1

    print(f"Host address : {cp.format_address(host)}")
    print(f"Secondary    : {cp.format_address(cp.secondary_address(host))}")
    print(f"Mode         : {'COMMIT — writes flash' if args.commit else 'dry run — writes nothing'}\n")

    import usb.core
    import usb.util

    device = usb.core.find(idVendor=usb_transport.VENDOR_ID,
                           idProduct=usb_transport.PRODUCT_ID,
                           backend=usb_transport.usb_backend())
    if device is None:
        print("❌ libusb cannot see the controller.")
        return 1
    try:
        usb.util.claim_interface(device, usb_transport.COMMAND_INTERFACE)
    except Exception as e:
        print(f"❌ Could not claim the command interface: {e}")
        print("   Quit 'Switch2 Bridge.app' — it holds interface 1 while wired.")
        return 1

    try:
        # The controller answers commands from a cold plug-in, so no full init
        # is needed; just wake the command channel.
        device.write(usb_transport.ENDPOINT_OUT,
                     bytes([0x07, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),
                     READ_TIMEOUT_MS)
        try:
            device.read(usb_transport.ENDPOINT_IN, 64, 400)
        except Exception:
            pass
        time.sleep(0.05)

        print("Running the exchange:")
        result = cp.pair(
            make_sender(device, args.verbose),
            host,
            transport=cp.TRANSPORT_USB,
            commit=args.commit,
        )
    except cp.PairingError as e:
        print(f"\n❌ {e}")
        return 1
    finally:
        try:
            usb.util.release_interface(device, usb_transport.COMMAND_INTERFACE)
        except Exception:
            pass

    print("\n   step 1  exchange addresses   ok")
    print("   step 2  exchange keys        ok")
    print("   step 3  confirm LTK          ok — AES check passed")
    if result["committed"]:
        print("   step 4  finalise            ok — written to flash "
              f"{cp.FLASH_PAIRING_BLOCK:#010x}")
    else:
        print("   step 4  finalise            SKIPPED (dry run)")

    print(f"\nDerived LTK: {result['ltk'].hex()}")
    if result["committed"]:
        print("\n✅ Paired. The controller now knows this Mac as a host.")
        print("   If your Switch 2 stops reconnecting to it, re-pair it there.")
    else:
        print("\n✅ Exchange verified — both sides derived the same key.")
        print("   Nothing was written. Re-run with --commit to pair for real.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true",
                        help="actually write the pairing to the controller")
    parser.add_argument("--host-address",
                        help="override this Mac's Bluetooth address")
    parser.add_argument("--verbose", action="store_true",
                        help="show every frame and reply")
    sys.exit(main(parser.parse_args()))
