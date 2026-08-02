#!/usr/bin/env python3
"""
Switch2 Bridge - macOS Menubar App
==================================

A clean menubar app to connect your Switch 2 Pro Controller and expose it
to emulators as a real analog gamepad over DSU (cemuhook) — no driver, no
permissions. A legacy keyboard bridge is still available for emulators that
read nothing but the keyboard, but it is off by default.

Author: Aurélien Desert
License: MIT
"""

import asyncio
import contextlib
import json
import logging
import struct
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ============================================================
# DEPENDENCY CHECK
# ============================================================

try:
    import rumps
except ImportError:
    print("❌ rumps not installed — run: pip install rumps")
    sys.exit(1)

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("❌ bleak not installed — run: pip install bleak")
    sys.exit(1)

AXIsProcessTrusted = None
try:
    from ApplicationServices import AXIsProcessTrusted
except ImportError:
    try:
        from HIServices import AXIsProcessTrusted  # older pyobjc layout
    except ImportError:
        pass

from controller_state import (
    BUTTON_NAMES,
    ControllerState,
    StickCalibration,
    monotonic_us,
    shape_stick,
)
import controller_commands as cc
import controller_pairing
from dsu_server import ALIASABLE_BUTTONS, DSUServer
from outputs import SPECIAL_KEY_NAMES, KeyboardOutput, pynput_available
import usb_transport

SMAppService = None
try:
    from ServiceManagement import SMAppService
except ImportError:
    pass  # "Start at Login" simply won't be offered


# ============================================================
# CONSTANTS
# ============================================================

APP_NAME = "Switch2 Bridge"
APP_VERSION = "1.9.1"  # single source of truth — read by setup_app.py & build_dmg.sh
INPUT_CHAR_UUID = "7492866c-ec3e-4619-8258-32755ffcc0f9"

# Nintendo company identifiers seen in BLE advertisements:
# 0x0553 is the Bluetooth SIG assigned ID, 0x057E is Nintendo's USB VID
# (both observed in the wild depending on firmware)
NINTENDO_COMPANY_IDS = (0x0553, 0x057e)
# Switch 2 Pro Controller product ID 0x2069, little-endian as it appears on the wire
SWITCH2_PRO_PID_LE = b'\x69\x20'

SCAN_TIMEOUT = 5.0
CONNECT_TIMEOUT = 15.0
# Keep scanning this long on the first search — pairing-mode advertising is
# easy to miss with a single short window
INITIAL_SCAN_WINDOW = 30.0
# After an unexpected drop, keep trying to reconnect for this long
RECONNECT_WINDOW = 60.0

CONFIG_DIR = Path.home() / "Library" / "Application Support" / "Switch2Bridge"
MAPPINGS_FILE = CONFIG_DIR / "mappings.json"

LOG_DIR = Path.home() / "Library" / "Logs" / "Switch2Bridge"
LOG_DIR.mkdir(parents=True, exist_ok=True)
_log_handler = RotatingFileHandler(
    LOG_DIR / "bridge.log", maxBytes=1_000_000, backupCount=2
)
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
log = logging.getLogger(__name__)


# ============================================================
# MAPPINGS — load/save user-editable JSON
# ============================================================

class Mappings:
    """User-editable button + stick mappings.

    Loaded from ~/Library/Application Support/Switch2Bridge/mappings.json.
    On first launch, the default mapping is written there so users can edit it.
    A value of null (or "<none>") leaves that button unmapped.
    """

    CONFIG_VERSION = 2

    DEFAULT = {
        "version": CONFIG_VERSION,
        # The controller is exposed as a real analog gamepad over DSU. The
        # keyboard bridge is legacy: it needs Accessibility and throws away
        # analog precision, so it stays off unless explicitly enabled.
        "dsu": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 26760,
            # DSU has no slot for the Switch 2-only buttons; fold them onto a
            # DSU button here if you want them, e.g. "C": "HOME".
            "aliases": {"GL": None, "GR": None, "C": None},
        },
        "keyboard": {"enabled": False},
        "controller": {
            # Which player LED to light once connected, one bit per LED
            # (1 = player 1, 0x0F = all four, 0 = leave them alone)
            "player_light": 1,
            # Prefer a wired connection when the controller is plugged in:
            # 250 Hz and a 4 ms interval, against 33 Hz / 30 ms over Bluetooth
            "usb": True,
            # Connect without clicking. A plugged-in controller is picked up
            # as soon as it appears; Bluetooth gets one search at launch,
            # since it needs the pair button held anyway.
            "auto_connect": True,
        },
        "motion": {
            # The Switch 2 IMU layout is not confirmed. Run
            # tools/capture_packets.py, then tools/analyze_capture.py to find
            # the offsets for your firmware and set them here.
            "enabled": False,
            "accel_offset": None,
            "gyro_offset": None,
            "accel_scale": 1.0 / 4096.0,   # raw int16 -> g
            "gyro_scale": 1.0 / 16.4,      # raw int16 -> deg/s
        },
        "buttons": {
            "A": "z", "B": "x", "X": "c", "Y": "v",
            "L": "q", "R": "e", "ZL": "1", "ZR": "3",
            "+": "p", "-": "m", "HOME": "h", "CAPT": "o",
            "C": None,
            "LS": "f", "RS": "g", "GL": "9", "GR": "0",
            "DUP": "<up>", "DDOWN": "<down>",
            "DLEFT": "<left>", "DRIGHT": "<right>",
        },
        "sticks": {
            "threshold": 0.5,       # keyboard bridge only
            "deadzone": 0.08,       # radial, applied to the analog output
            "saturation": 0.95,     # deflection treated as "fully pushed"
            # The controller stores its own per-unit calibration in flash;
            # reading it beats any constant. auto_center/half_range are the
            # fallback for firmware that will not answer.
            "calibration": {
                "use_factory": True,
                "auto_center": True,
                "half_range": 1500,
            },
            "left":  {"up": "w", "down": "s", "left": "a", "right": "d"},
            "right": {"up": "i", "down": "k", "left": "j", "right": "l"},
        },
    }

    BUTTON_NAMES = frozenset(BUTTON_NAMES)
    STICK_DIRECTIONS = frozenset(("up", "down", "left", "right"))
    DSU_ALIAS_TARGETS = frozenset(BUTTON_NAMES) - frozenset(ALIASABLE_BUTTONS)

    THRESHOLD_MIN, THRESHOLD_MAX = 0.1, 0.9

    def __init__(self):
        self.buttons = {}
        self.stick_threshold = 0.5
        self.stick_deadzone = 0.08
        self.stick_saturation = 0.95
        self.stick_half_range = 1500
        self.stick_auto_center = True
        self.use_factory_calibration = True
        self.player_light = 1
        self.usb_enabled = True
        self.auto_connect = True
        self.left_stick = {}
        self.right_stick = {}
        self.dsu_enabled = True
        self.dsu_host = "127.0.0.1"
        self.dsu_port = 26760
        self.dsu_aliases = {}
        self.keyboard_enabled = False
        self.motion_enabled = False
        self.motion_accel_offset = None
        self.motion_gyro_offset = None
        self.motion_accel_scale = 1.0 / 4096.0
        self.motion_gyro_scale = 1.0 / 16.4
        # Consumed by the UI tick: error → alert, warning → notification
        self.last_error = None
        self.last_warning = None

    # --- IO ---

    @classmethod
    def ensure_default_file(cls):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not MAPPINGS_FILE.exists():
            with open(MAPPINGS_FILE, "w") as f:
                json.dump(cls.DEFAULT, f, indent=2)
            log.info("wrote default mappings to %s", MAPPINGS_FILE)

    def load(self):
        """Load mappings from disk, falling back to defaults on error.

        Returns True only when the file was read and applied cleanly.
        """
        self.last_error = None
        self.last_warning = None
        cfg = self.DEFAULT
        ok = True
        try:
            self.ensure_default_file()
            with open(MAPPINGS_FILE) as f:
                cfg = json.load(f)
        except Exception as e:
            log.exception("failed to read mappings.json")
            self.last_error = f"Could not read mappings.json: {e}\nUsing defaults."
            cfg = self.DEFAULT
            ok = False

        migration_note = self._migrate(cfg) if ok else None

        try:
            self._apply(cfg)
            log.info("mappings loaded from %s", MAPPINGS_FILE)
            if migration_note:
                self.last_warning = migration_note
        except Exception as e:
            log.exception("invalid mappings.json")
            self.last_error = f"Invalid mappings.json: {e}\nUsing defaults."
            self._apply(self.DEFAULT)
            ok = False
        return ok

    # --- internals ---

    def _migrate(self, cfg):
        """Add blocks introduced after the user's file was written.

        Only ever *adds* missing keys — existing values are left alone, so a
        hand-edited file survives an upgrade intact. Returns a note to show
        the user when the migration changes how the app behaves.
        """
        if not isinstance(cfg, dict):
            return None
        was_v1 = int(cfg.get("version", 1) or 1) < 2
        added = []
        for block in ("dsu", "keyboard", "motion", "controller"):
            if not isinstance(cfg.get(block), dict):
                cfg[block] = dict(self.DEFAULT[block])
                added.append(block)
            else:
                for key, value in self.DEFAULT[block].items():
                    if key not in cfg[block]:
                        cfg[block][key] = value
        sticks = cfg.get("sticks")
        if isinstance(sticks, dict):
            for key in ("deadzone", "saturation", "calibration"):
                sticks.setdefault(key, self.DEFAULT["sticks"][key])
        if not added and not was_v1:
            return None

        cfg["version"] = self.CONFIG_VERSION
        try:
            with open(MAPPINGS_FILE, "w") as f:
                json.dump(cfg, f, indent=2)
            log.info("migrated mappings.json to v%d (added: %s)",
                     self.CONFIG_VERSION, ", ".join(added) or "nothing")
        except Exception:
            log.exception("could not write migrated mappings.json")

        if "keyboard" in added:
            return (
                "Updated config: the controller now works as a real analog "
                "gamepad over DSU, and the keyboard bridge is off.\n"
                "Re-enable it from the menubar (Keyboard bridge) if an "
                "emulator of yours only reads the keyboard."
            )
        return None

    def _apply(self, cfg):
        buttons = cfg.get("buttons", {})
        sticks = cfg.get("sticks", {})
        if not isinstance(buttons, dict):
            raise ValueError('"buttons" must be an object')
        if not isinstance(sticks, dict):
            raise ValueError('"sticks" must be an object')

        warnings = []

        unknown = sorted(set(buttons) - self.BUTTON_NAMES)
        if unknown:
            warnings.append(f"Unknown button name(s) ignored: {', '.join(unknown)}")

        self.buttons = {
            name: self._parse_key(v)
            for name, v in buttons.items() if name in self.BUTTON_NAMES
        }

        try:
            threshold = float(sticks.get("threshold", 0.5))
        except (TypeError, ValueError):
            raise ValueError('"sticks.threshold" must be a number')
        clamped = min(max(threshold, self.THRESHOLD_MIN), self.THRESHOLD_MAX)
        if clamped != threshold:
            warnings.append(f"Stick threshold {threshold} out of range, using {clamped}")
        self.stick_threshold = clamped

        def number(block, key, default, lo, hi):
            try:
                value = float(block.get(key, default))
            except (TypeError, ValueError):
                raise ValueError(f'"sticks.{key}" must be a number')
            bounded = min(max(value, lo), hi)
            if bounded != value:
                warnings.append(f"sticks.{key} {value} out of range, using {bounded}")
            return bounded

        self.stick_deadzone = number(sticks, "deadzone", 0.08, 0.0, 0.9)
        self.stick_saturation = number(sticks, "saturation", 0.95, 0.1, 1.0)
        if self.stick_saturation <= self.stick_deadzone:
            warnings.append(
                f"sticks.saturation ({self.stick_saturation}) must exceed "
                f"deadzone ({self.stick_deadzone}); using defaults"
            )
            self.stick_deadzone, self.stick_saturation = 0.08, 0.95

        calibration = sticks.get("calibration", {})
        if not isinstance(calibration, dict):
            raise ValueError('"sticks.calibration" must be an object')
        self.stick_auto_center = bool(calibration.get("auto_center", True))
        self.use_factory_calibration = bool(calibration.get("use_factory", True))
        try:
            half_range = int(calibration.get("half_range", 1500))
        except (TypeError, ValueError):
            raise ValueError('"sticks.calibration.half_range" must be an integer')
        if not (200 <= half_range <= 2048):
            warnings.append(
                f"sticks.calibration.half_range {half_range} out of range, using 1500"
            )
            half_range = 1500
        self.stick_half_range = half_range

        parsed_sticks = {}
        for side in ("left", "right"):
            side_cfg = sticks.get(side, {})
            if not isinstance(side_cfg, dict):
                raise ValueError(f'"sticks.{side}" must be an object')
            unknown = sorted(set(side_cfg) - self.STICK_DIRECTIONS)
            if unknown:
                warnings.append(
                    f"Unknown {side} stick direction(s) ignored: {', '.join(unknown)}"
                )
            parsed_sticks[side] = {
                d: self._parse_key(k)
                for d, k in side_cfg.items() if d in self.STICK_DIRECTIONS
            }
        self.left_stick = parsed_sticks["left"]
        self.right_stick = parsed_sticks["right"]

        dsu = cfg.get("dsu", {})
        if not isinstance(dsu, dict):
            raise ValueError('"dsu" must be an object')
        self.dsu_enabled = bool(dsu.get("enabled", True))
        self.dsu_host = str(dsu.get("host", "127.0.0.1"))
        try:
            port = int(dsu.get("port", 26760))
        except (TypeError, ValueError):
            raise ValueError('"dsu.port" must be an integer')
        if not (1024 <= port <= 65535):
            warnings.append(f"DSU port {port} out of range, using 26760")
            port = 26760
        self.dsu_port = port

        aliases_cfg = dsu.get("aliases", {})
        if not isinstance(aliases_cfg, dict):
            raise ValueError('"dsu.aliases" must be an object')
        aliases = {}
        for source, target in aliases_cfg.items():
            if target is None:
                continue
            if source not in ALIASABLE_BUTTONS:
                warnings.append(
                    f"dsu.aliases: {source} is not aliasable "
                    f"(only {', '.join(ALIASABLE_BUTTONS)})"
                )
                continue
            if target not in self.DSU_ALIAS_TARGETS:
                warnings.append(f"dsu.aliases: unknown target button {target!r}")
                continue
            aliases[source] = target
        self.dsu_aliases = aliases

        controller = cfg.get("controller", {})
        if not isinstance(controller, dict):
            raise ValueError('"controller" must be an object')
        try:
            light = int(controller.get("player_light", 1))
        except (TypeError, ValueError):
            raise ValueError('"controller.player_light" must be an integer')
        if not (0 <= light <= 0x0F):
            warnings.append(
                f"controller.player_light {light} out of range (0-15), using 1"
            )
            light = 1
        self.player_light = light
        self.usb_enabled = bool(controller.get("usb", True))
        self.auto_connect = bool(controller.get("auto_connect", True))

        keyboard_cfg = cfg.get("keyboard", {})
        if not isinstance(keyboard_cfg, dict):
            raise ValueError('"keyboard" must be an object')
        self.keyboard_enabled = bool(keyboard_cfg.get("enabled", False))
        if self.keyboard_enabled and not pynput_available():
            warnings.append(
                "Keyboard bridge is enabled but pynput is not installed; "
                "install it or leave the bridge off and use DSU."
            )

        motion = cfg.get("motion", {})
        if not isinstance(motion, dict):
            raise ValueError('"motion" must be an object')
        self.motion_accel_offset = self._parse_offset(motion, "accel_offset")
        self.motion_gyro_offset = self._parse_offset(motion, "gyro_offset")
        try:
            self.motion_accel_scale = float(motion.get("accel_scale", 1.0 / 4096.0))
            self.motion_gyro_scale = float(motion.get("gyro_scale", 1.0 / 16.4))
        except (TypeError, ValueError):
            raise ValueError('"motion.accel_scale"/"gyro_scale" must be numbers')
        self.motion_enabled = bool(motion.get("enabled", False))
        if self.motion_enabled and (
            self.motion_accel_offset is None and self.motion_gyro_offset is None
        ):
            warnings.append(
                "motion.enabled is set but no accel_offset/gyro_offset is "
                "configured — motion will stay zeroed. Run "
                "tools/capture_packets.py to find them."
            )
            self.motion_enabled = False

        if warnings:
            self.last_warning = "\n".join(warnings)

    @staticmethod
    def _parse_offset(block, key):
        value = block.get(key)
        if value is None:
            return None
        try:
            offset = int(value)
        except (TypeError, ValueError):
            raise ValueError(f'"motion.{key}" must be an integer or null')
        if offset < 0:
            raise ValueError(f'"motion.{key}" must not be negative')
        return offset

    def set_dsu_enabled(self, enabled):
        """Persist the DSU toggle back into mappings.json (best effort)."""
        self.dsu_enabled = bool(enabled)
        self._persist_flag("dsu", "enabled", self.dsu_enabled)

    def set_keyboard_enabled(self, enabled):
        """Persist the keyboard-bridge toggle back into mappings.json."""
        self.keyboard_enabled = bool(enabled)
        self._persist_flag("keyboard", "enabled", self.keyboard_enabled)

    @staticmethod
    def _persist_flag(block, key, value):
        try:
            Mappings.ensure_default_file()
            with open(MAPPINGS_FILE) as f:
                cfg = json.load(f)
        except Exception:
            # Never clobber a corrupt (but user-authored) file with defaults —
            # the toggle just won't persist until the file is fixed.
            log.exception("mappings.json unreadable, %s.%s not persisted", block, key)
            return
        section = cfg.get(block)
        if not isinstance(section, dict):
            section = cfg[block] = {}
        section[key] = value
        try:
            with open(MAPPINGS_FILE, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            log.exception("could not write mappings.json to persist %s.%s", block, key)

    @classmethod
    def _parse_key(cls, value):
        """Validate a key and return it as a token the keyboard output resolves.

        Tokens stay plain strings so mappings can be parsed and reported even
        when pynput is absent — the keyboard bridge is optional now.
        """
        if value is None or value == "<none>":
            return None
        if not isinstance(value, str):
            raise ValueError(f"key must be a string or null, got {type(value).__name__}")
        if value in SPECIAL_KEY_NAMES:
            return value
        if len(value) == 1:
            # "Z" would be typed as shift+z by pynput; games expect the bare keycode
            if value != value.lower():
                log.info("normalizing key %r to %r", value, value.lower())
            return value.lower()
        raise ValueError(
            f"unknown key {value!r} (use a single character, null, or one of "
            f"{sorted(SPECIAL_KEY_NAMES)})"
        )


# ============================================================
# CONTROLLER BRIDGE (BLE + keyboard, runs in worker thread)
# ============================================================

class ControllerBridge:
    """BLE connection + fan-out to the output backends.

    The BLE report is decoded exactly once into a ControllerState, which is
    then handed to every enabled output. DSU is the real-gamepad path; the
    keyboard bridge is optional and legacy.
    """

    # (byte offset, bit mask, button name)
    _BUTTON_BITS = (
        (2, 0x01, 'B'), (2, 0x02, 'A'), (2, 0x04, 'Y'), (2, 0x08, 'X'),
        (2, 0x10, 'R'), (2, 0x20, 'ZR'), (2, 0x40, '+'), (2, 0x80, 'RS'),
        (3, 0x01, 'DDOWN'), (3, 0x02, 'DRIGHT'), (3, 0x04, 'DLEFT'),
        (3, 0x08, 'DUP'), (3, 0x10, 'L'), (3, 0x20, 'ZL'),
        (3, 0x40, '-'), (3, 0x80, 'LS'),
        # byte 4 is the Switch 2's own block. These assignments come from a
        # timed capture (tools/capture_packets.py): each bit was held late in
        # its own phase and bled into the next, and ZL/ZR served as a control
        # for that lag. This corrects an earlier swap of C and CAPT.
        (4, 0x01, 'HOME'), (4, 0x02, 'CAPT'), (4, 0x04, 'GR'),
        (4, 0x08, 'GL'), (4, 0x10, 'C'),
    )

    MIN_REPORT_LEN = 11
    # Scan window used while watching. Short, so a controller entering
    # pairing mode is picked up quickly and USB is noticed promptly too.
    WATCH_SCAN_TIMEOUT = 3.0
    # How often to look for a cable. Plugging in should upgrade to wired
    # without making the user reconnect, whatever the radio is doing.
    USB_POLL_INTERVAL = 1.0
    # How long to leave a cable alone after failing to claim it. Without this,
    # a cable that enumerates but will not open has the loop abandon Bluetooth
    # and retry it every second for as long as it stays plugged in.
    USB_RETRY_COOLDOWN = 10.0
    # Upper bound on the whole configure step, so a controller that never
    # answers costs us a moment rather than the connection
    CONFIG_TIMEOUT = 3.0
    # Zero frames sent after an effect ends. One dropped stop frame would
    # otherwise leave the motors running.
    RUMBLE_TRAILING_FRAMES = 3
    # How many raw reports "Capture raw packets" writes to the log
    RAW_CAPTURE_LIMIT = 300

    def __init__(self, mappings: Mappings, dsu: DSUServer = None,
                 keyboard_output=None):
        self.mappings = mappings
        self.dsu = dsu
        self.keyboard = keyboard_output
        self.last_state = None       # most recent ControllerState, for the UI
        self._raw_remaining = 0      # >0 while a raw capture is running
        self.calibration = StickCalibration(
            half_range=mappings.stick_half_range,
            auto_center=mappings.stick_auto_center,
        )
        self.watching = False      # keep looking instead of giving up
        self._usb_retry_after = 0.0   # cable we failed to claim: leave it be
        # Rumble state, shared with whichever transport is live. Wired owns its
        # own repeat loop; Bluetooth is driven from the session's event loop.
        self._rumble_state = (0, 0)
        self._rumble_lock = threading.Lock()
        self._rumble_seq = 0
        self.usb = None            # USBTransport while wired
        self.transport = None      # "usb" or "ble" once connected
        self.is_connected = False
        self.is_searching = False
        self.is_connecting = False
        self.is_reconnecting = False
        self.controller_name = None
        self.packet_count = 0
        # Set by worker, read & cleared by the main-thread UI tick
        self.last_error = None
        self.last_notice = None
        self._client = None
        self._stop_event = threading.Event()
        self._thread = None
        self._loop = None
        self._task = None

    # --- key dispatch (delegated to the optional keyboard backend) ---

    def release_all_keys(self):
        if self.keyboard is not None:
            self.keyboard.release_all()

    # --- controller configuration (command channel) ---

    async def _configure_with_timeout(self, client):
        try:
            await asyncio.wait_for(
                self._configure_controller(client), timeout=self.CONFIG_TIMEOUT
            )
        except asyncio.TimeoutError:
            log.info("controller configuration timed out; using defaults")
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("controller configuration failed")

    async def _configure_controller(self, client):
        """Read stick calibration from flash and light a player LED.

        Best effort: a controller or firmware that does not answer simply
        keeps the measured defaults, so a failure here never costs us the
        connection.
        """
        replies = asyncio.Queue()

        def on_reply(_sender, payload):
            replies.put_nowait(bytes(payload))

        try:
            await client.start_notify(cc.RESPONSE_CHAR, on_reply)
        except Exception as e:
            log.info("command channel unavailable (%s); using measured defaults", e)
            return

        async def spi_read(address, length):
            while not replies.empty():
                replies.get_nowait()
            await client.write_gatt_char(
                cc.COMMAND_CHAR, cc.spi_read_command(address, length),
                response=False,
            )
            # The controller also streams input on other channels; only the
            # reply that echoes our address counts.
            for _attempt in range(3):
                try:
                    reply = await asyncio.wait_for(replies.get(), timeout=0.8)
                except asyncio.TimeoutError:
                    return None
                parsed = cc.parse_spi_reply(reply)
                if parsed and parsed[0] == address:
                    return parsed[1]
            return None

        try:
            left = cc.decode_stick_block(
                await spi_read(cc.FACTORY_STICK_LEFT, cc.STICK_BLOCK_LEN)
            )
            right = cc.decode_stick_block(
                await spi_read(cc.FACTORY_STICK_RIGHT, cc.STICK_BLOCK_LEN)
            )
            if self.mappings.use_factory_calibration:
                if self.calibration.apply_factory(left, right):
                    self.last_notice = "Stick calibration read from controller"
                else:
                    log.info("no usable factory calibration; keeping defaults")

            await client.write_gatt_char(
                cc.COMMAND_CHAR,
                cc.player_lights_command(self.mappings.player_light),
                response=False,
            )
        except Exception:
            log.exception("controller configuration failed; continuing anyway")
        finally:
            # Nothing reads this channel once configuration is done, and every
            # extra notification stream is airtime competing with the input
            # report on a 30 ms connection interval.
            try:
                await client.stop_notify(cc.RESPONSE_CHAR)
            except Exception:
                pass

    # --- raw capture (diagnostics) ---

    def start_raw_capture(self, count=None):
        """Dump the next N raw reports to the log, for protocol work."""
        self._raw_remaining = count or self.RAW_CAPTURE_LIMIT
        log.info("raw capture armed for %d reports", self._raw_remaining)

    @property
    def raw_capture_active(self):
        return self._raw_remaining > 0

    # --- BLE input parser ---

    def _decode_motion(self, data):
        """Decode the IMU if the user has configured where it lives.

        The Switch 2 report layout is not published and we refuse to guess:
        with no offsets configured this returns zeros, which DSU clients read
        as "no motion" rather than as garbage.
        """
        m = self.mappings
        if not m.motion_enabled:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)

        def triple(offset, scale):
            if offset is None or len(data) < offset + 6:
                return (0.0, 0.0, 0.0)
            x, y, z = struct.unpack_from("<3h", data, offset)
            return (x * scale, y * scale, z * scale)

        return (
            triple(m.motion_accel_offset, m.motion_accel_scale),
            triple(m.motion_gyro_offset, m.motion_gyro_scale),
        )

    def _build_state(self, data: bytes):
        """Decode one BLE report into a neutral ControllerState."""
        buttons = {
            name: bool(data[offset] & mask)
            for offset, mask, name in self._BUTTON_BITS
        }

        # sticks: 12-bit packed across bytes 5-10
        lx_raw = data[5] | ((data[6] & 0x0F) << 8)
        ly_raw = ((data[6] & 0xF0) >> 4) | (data[7] << 4)
        rx_raw = data[8] | ((data[9] & 0x0F) << 8)
        ry_raw = ((data[9] & 0xF0) >> 4) | (data[10] << 4)

        cal = self.calibration
        cal.observe({'lx': lx_raw, 'ly': ly_raw, 'rx': rx_raw, 'ry': ry_raw})
        deadzone = self.mappings.stick_deadzone
        saturation = self.mappings.stick_saturation
        lx, ly = shape_stick(
            cal.value('lx', lx_raw), cal.value('ly', ly_raw), deadzone, saturation
        )
        rx, ry = shape_stick(
            cal.value('rx', rx_raw), cal.value('ry', ry_raw), deadzone, saturation
        )

        accel, gyro = self._decode_motion(data)
        return ControllerState(
            buttons=buttons, lx=lx, ly=ly, rx=rx, ry=ry,
            accel=accel, gyro=gyro,
            timestamp_us=monotonic_us(),
        )

    def _on_data(self, sender, data: bytes):
        if len(data) < self.MIN_REPORT_LEN:
            return

        self.packet_count += 1

        if self._raw_remaining > 0:
            self._raw_remaining -= 1
            log.info("raw[%3d] len=%d %s",
                     self._raw_remaining, len(data), bytes(data).hex())

        try:
            state = self._build_state(data)
        except Exception:
            log.exception("failed to decode report: %s", bytes(data).hex())
            return
        self.last_state = state

        # Fan out. One failing backend must not stop the others.
        if self.dsu is not None and self.dsu.running:
            try:
                self.dsu.push(state)
            except Exception:
                log.exception("DSU push failed")
        if self.keyboard is not None and self.keyboard.enabled:
            try:
                self.keyboard.push(state)
            except Exception:
                log.exception("keyboard push failed")

    # --- discovery ---

    async def _find_controller(self, timeout=SCAN_TIMEOUT):
        """Returns (address, name) or (None, None)."""
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        seen = []
        for address, (device, adv) in devices.items():
            mfr = ", ".join(
                f"{cid:#06x}:{bytes(p).hex()}"
                for cid, p in adv.manufacturer_data.items()
            )
            seen.append(
                f"{device.name or '?'} rssi={getattr(adv, 'rssi', '?')} [{mfr}]"
            )
            # Primary: Nintendo company ID is the dict key in bleak's manufacturer_data
            for cid in NINTENDO_COMPANY_IDS:
                payload = adv.manufacturer_data.get(cid)
                if payload and SWITCH2_PRO_PID_LE in payload:
                    return address, device.name or "Switch 2 Pro Controller"
            # Fallback: some macOS BLE stacks expose name without manufacturer data
            if device.name and "Pro Controller" in device.name:
                return address, device.name
        # Diagnostic dump: this is what tells us why a controller wasn't matched
        log.info(
            "scan: no controller among %d device(s): %s",
            len(seen), "; ".join(seen[:20]) or "none",
        )
        return None, None

    # --- rumble ---

    def set_rumble(self, low_amplitude, high_amplitude):
        """Drive the motors over whichever transport is connected.

        Returns False when there is nothing to drive, so callers can say so
        rather than leaving a click looking like it did nothing.
        """
        pair = (max(0, min(0xFFFF, int(low_amplitude))),
                max(0, min(0xFFFF, int(high_amplitude))))
        if self.usb is not None and self.usb.connected:
            self.usb.set_rumble(*pair)
            return True
        if self.is_connected and self.transport == "ble":
            with self._rumble_lock:
                self._rumble_state = pair
            return True
        return False

    async def _ble_rumble_loop(self, client):
        """Repeat the current effect over Bluetooth until it is cleared.

        The motors decay unless refreshed, exactly as they do wired. The
        cadence is slower here on purpose: the connection interval is 30 ms, so
        writing faster than that only queues frames the radio cannot carry.
        """
        trailing = 0
        while True:
            with self._rumble_lock:
                low, high = self._rumble_state
            if low or high:
                trailing = self.RUMBLE_TRAILING_FRAMES
            elif trailing > 0:
                trailing -= 1     # explicit zero frames, then go quiet
            else:
                await asyncio.sleep(0.05)
                continue
            self._rumble_seq += 1
            try:
                await client.write_gatt_char(
                    cc.VIBRATION_CHAR,
                    cc.ble_rumble_payload(low, high, self._rumble_seq),
                    response=False,
                )
            except Exception:
                log.debug("BLE rumble write failed", exc_info=True)
                return
            await asyncio.sleep(0.03)

    def _clear_rumble(self):
        with self._rumble_lock:
            self._rumble_state = (0, 0)

    # --- cable watching ---

    # Returned by _until_cable when the cable, not the Bluetooth work, won.
    _CABLE = object()

    async def _cable_appeared(self):
        """Resolve once a usable cable is plugged in."""
        while not self._stop_event.is_set():
            if (
                self.mappings.usb_enabled
                and time.monotonic() >= self._usb_retry_after
                and usb_transport.is_connected()
            ):
                return
            await asyncio.sleep(self.USB_POLL_INTERVAL)

    async def _until_cable(self, coro):
        """Run `coro`, abandoning it if a cable turns up first.

        Every Bluetooth step blocks for seconds at a time — a scan runs its
        timeout out in full, a connect can sit for CONNECT_TIMEOUT — and the
        cable used to be looked at only between steps, so plugging in during
        a scan or a handshake did nothing for up to twenty seconds. Racing
        the two means a cable is noticed within USB_POLL_INTERVAL no matter
        which stage the radio is in.

        Returns whatever `coro` returned, or `_CABLE` if the cable won.
        """
        work = asyncio.ensure_future(coro)
        cable = asyncio.ensure_future(self._cable_appeared())
        try:
            await asyncio.wait({work, cable}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                return work.result()
            return self._CABLE
        finally:
            cable.cancel()
            if not work.done():
                work.cancel()
                # Awaited rather than dropped: a cancelled session still has to
                # run its own cleanup, which tears down the CoreBluetooth link.
                with contextlib.suppress(asyncio.CancelledError):
                    await work

    def _note_cable_upgrade(self):
        """Report the switch to wired as an upgrade rather than a failure."""
        log.info("cable plugged in — upgrading to wired")
        self.last_error = None
        self.last_notice = "Cable connected — switching to wired"

    # --- main async routine ---

    async def _session(self, address, name, reconnected=False):
        """Connect and stream input until disconnect/stop.

        Returns True once input streaming was reached (used to decide whether
        an auto-reconnect is worth attempting). On failure, last_error is set.
        """
        client = BleakClient(address, timeout=CONNECT_TIMEOUT)
        self._client = client
        self.is_connecting = True
        try:
            try:
                await client.connect()
            except Exception as e:
                log.exception("connect failed")
                self.last_error = f"Connection error: {e}"
                return False
            if not client.is_connected:
                self.last_error = "Failed to connect to controller."
                return False

            try:
                await client.start_notify(INPUT_CHAR_UUID, self._on_data)
            except Exception as e:
                log.exception("start_notify failed")
                self.last_error = f"Connection error: {e}"
                return False

            # Re-learn the resting centre for each new session: the drift is
            # per-connection, not per-unit. Factory calibration, read below,
            # supersedes it when available.
            self.calibration.reset()
            self.controller_name = name
            self.transport = "ble"
            self.is_connecting = False
            self.is_connected = True
            self.is_reconnecting = False
            if self.dsu is not None:
                self.dsu.set_connected(True)
            log.info("connected to %s @ %s", name, address)
            if reconnected:
                self.last_notice = f"Reconnected to {name}"

            # Configuration is an enhancement, not a precondition. Run it
            # alongside the stream so a silent controller neither delays the
            # connection nor slows down noticing a drop.
            config_task = asyncio.create_task(self._configure_with_timeout(client))
            # Rumble needs a writer on this loop for as long as the session
            # lasts; the wired transport drives its own from a thread.
            self._clear_rumble()
            rumble_task = asyncio.create_task(self._ble_rumble_loop(client))
            try:
                # A cable arriving here is handled by the caller's race, which
                # cancels this session — wired is 250 Hz against 33 Hz.
                while not self._stop_event.is_set() and client.is_connected:
                    await asyncio.sleep(0.1)
            finally:
                config_task.cancel()
                # Stop the motors before the link goes, or an effect running at
                # disconnect has nothing left to turn it off.
                self._clear_rumble()
                if client.is_connected:
                    with contextlib.suppress(Exception):
                        await client.write_gatt_char(
                            cc.VIBRATION_CHAR,
                            cc.ble_rumble_payload(0, 0, self._rumble_seq + 1),
                            response=False,
                        )
                rumble_task.cancel()
            return True
        finally:
            self.is_connecting = False
            self.is_connected = False
            self.controller_name = None
            self._client = None
            if self.dsu is not None:
                self.dsu.set_connected(False)
            self.release_all_keys()
            try:
                if client.is_connected:
                    await client.stop_notify(INPUT_CHAR_UUID)
            except Exception as e:
                log.warning("BLE stop_notify error: %s", e)
            try:
                # Always disconnect: also cancels a pending CoreBluetooth
                # connection attempt if we were cancelled mid-connect.
                await client.disconnect()
            except Exception as e:
                log.warning("BLE disconnect error: %s", e)

    @staticmethod
    def _scan_error_message(exc):
        """Turn a bleak scan failure into an actionable message."""
        text = str(exc).lower()
        if "unauthorized" in text or "not authorized" in text or "denied" in text:
            return (
                "macOS refused Bluetooth access. Grant it in System Settings → "
                "Privacy & Security → Bluetooth.\nWhen running from source, the "
                "permission belongs to Terminal (or your Python), not the app."
            )
        if "turned off" in text or "powered off" in text:
            return "Bluetooth is turned off. Enable it in Control Center and retry."
        return f"Bluetooth scan failed: {exc}"

    def _try_usb(self):
        """Wired is preferred when available: 250 Hz against 33 Hz.

        Returns True once streaming. Never fatal — falling back to Bluetooth
        is always fine.
        """
        if not self.mappings.usb_enabled or not usb_transport.is_connected():
            return False
        if time.monotonic() < self._usb_retry_after:
            # Still backing off a cable that would not open; stay on Bluetooth.
            return False

        def on_report(body):
            self._on_data(None, body)

        def on_error(message):
            self.last_error = message

        transport = usb_transport.USBTransport(on_report, on_error)
        if not transport.connect():
            # Back off before looking at this cable again, or the loop spends
            # every second abandoning Bluetooth to retry a cable that will not
            # open — which is worse than simply staying on the radio.
            self._usb_retry_after = time.monotonic() + self.USB_RETRY_COOLDOWN
            if transport.last_error:
                log.info("wired connect failed: %s", transport.last_error)
                self.last_notice = "USB found but unavailable — using Bluetooth"
            return False
        self._usb_retry_after = 0.0

        self.calibration.reset()
        if self.mappings.use_factory_calibration:
            left, right = transport.read_stick_calibration()
            if self.calibration.apply_factory(left, right):
                self.last_notice = "Stick calibration read from controller"
        transport.set_player_light(self.mappings.player_light)
        self.usb = transport
        self.transport = "usb"
        self.controller_name = "Switch 2 Pro Controller (wired)"
        self.is_searching = False
        self.is_connecting = False
        self.is_connected = True
        if self.dsu is not None:
            self.dsu.set_connected(True)
        log.info("connected over USB")
        return True

    async def _usb_session(self):
        """Stream from the wired controller until it goes away or we stop."""
        try:
            while not self._stop_event.is_set() and self.usb.connected:
                await asyncio.sleep(0.1)
        finally:
            transport, self.usb = self.usb, None
            self.transport = None
            self.is_connected = False
            self.controller_name = None
            if self.dsu is not None:
                self.dsu.set_connected(False)
            self.release_all_keys()
            if transport is not None:
                transport.disconnect()

    async def _connect_async(self):
        was_connected = False
        deadline = time.monotonic() + INITIAL_SCAN_WINDOW
        try:
            while not self._stop_event.is_set():
                self.is_searching = True
                self.packet_count = 0

                if self._try_usb():
                    await self._usb_session()
                    if self._stop_event.is_set():
                        return
                    # Cable pulled: fall through and look for it over Bluetooth
                    self.last_notice = "USB disconnected — searching Bluetooth"
                    continue
                try:
                    found = await self._until_cable(
                        self._find_controller(
                            self.WATCH_SCAN_TIMEOUT if self.watching else SCAN_TIMEOUT
                        )
                    )
                except Exception as e:
                    log.exception("BLE scan failed")
                    if self.watching and not self._bluetooth_fatal(e):
                        # Bluetooth off, or busy: keep watching rather than
                        # ending a session the user never started.
                        await asyncio.sleep(2.0)
                        continue
                    self.last_error = self._scan_error_message(e)
                    return

                if self._stop_event.is_set():
                    return

                if found is self._CABLE:
                    self._note_cable_upgrade()
                    continue
                address, name = found

                streamed = False
                if address:
                    self.is_searching = False
                    outcome = await self._until_cable(
                        self._session(address, name, reconnected=was_connected)
                    )
                    if self._stop_event.is_set():
                        return
                    if outcome is self._CABLE:
                        self._note_cable_upgrade()
                        continue
                    streamed = outcome
                    if not streamed and was_connected:
                        # Still auto-retrying — keep the failure in the logs
                        # only; a notification per attempt would spam the user.
                        self.last_error = None

                if self._stop_event.is_set():
                    return

                if streamed and self.watching:
                    self.is_reconnecting = False
                    self.last_notice = "Controller disconnected — still watching"
                    log.info("connection dropped; back to watching")
                    continue

                if streamed:
                    # Unexpected drop (controller slept, went out of range…):
                    # retry for RECONNECT_WINDOW before giving up.
                    was_connected = True
                    deadline = time.monotonic() + RECONNECT_WINDOW
                    self.is_reconnecting = True
                    self.last_notice = "Controller disconnected — reconnecting…"
                    log.info("connection dropped, entering reconnect loop")
                    continue

                if address and not was_connected:
                    if self.watching:
                        # Seen but not connectable yet — it is probably still
                        # settling into pairing mode. Keep waiting.
                        self.last_error = None
                        await asyncio.sleep(1.0)
                        continue
                    # Found it but couldn't connect — surface and stop.
                    return

                if self.watching:
                    was_connected = False   # a fresh pairing, not a reconnect
                    await asyncio.sleep(0.5)
                    continue

                if time.monotonic() > deadline:
                    self.last_error = (
                        "Could not reconnect to the controller."
                        if was_connected else
                        "Controller not found after 30 s. Hold the small pair "
                        "button on the back until the LEDs sweep, while the "
                        "search is running.\nNote: the controller will never "
                        "appear in System Settings → Bluetooth — watch the "
                        "menubar instead."
                    )
                    return

                # keep is_searching set during the pause so the UI doesn't flicker
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            log.info("bridge task cancelled")
        finally:
            self.is_searching = False
            self.is_reconnecting = False
            self.watching = False

    # --- public API ---

    @property
    def is_stopping(self):
        return (
            self._stop_event.is_set()
            and self._thread is not None
            and self._thread.is_alive()
        )

    @staticmethod
    def _bluetooth_fatal(exc):
        """Only a permission problem is worth giving up over."""
        text = str(exc).lower()
        return "unauthorized" in text or "not authorized" in text or "denied" in text

    def connect(self, watch=False):
        if self._thread and self._thread.is_alive():
            return
        self.watching = watch
        self._stop_event.clear()
        self.last_error = None
        self.last_notice = None

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                self._task = loop.create_task(self._connect_async())
                try:
                    loop.run_until_complete(self._task)
                except asyncio.CancelledError:
                    pass
            finally:
                self._task = None
                self._loop = None
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def disconnect(self, wait=False, timeout=2.0):
        if not self._stop_event.is_set():
            self._stop_event.set()
            # Interrupt whatever the worker is awaiting (scan, connect, stream)
            loop, task = self._loop, self._task
            if loop is not None and task is not None:
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass  # loop already closed
        if wait and self._thread and self._thread.is_alive():
            self._thread.join(timeout)


# ============================================================
# MENUBAR APP
# ============================================================

class Switch2BridgeApp(rumps.App):
    """
    Menu items are constructed once, then mutated in place via .title.
    We never call self.menu.clear() or reassign self.menu after init — doing
    so dismisses the open dropdown on every Timer tick.
    """

    REFRESH_INTERVAL = 1.0

    def __init__(self):
        super().__init__(APP_NAME, title="🎮", quit_button=None)

        self.mappings = Mappings()
        self.mappings.load()
        self.dsu = DSUServer(
            self.mappings.dsu_host, self.mappings.dsu_port,
            aliases=self.mappings.dsu_aliases,
            on_rumble=self._on_dsu_rumble,
        )
        self.keyboard = KeyboardOutput(self.mappings)
        self.bridge = ControllerBridge(self.mappings, self.dsu, self.keyboard)

        # Long-lived menu items
        self._status_item = rumps.MenuItem("○ Not connected")
        self._detail_item = rumps.MenuItem(" ")
        self._action_item = rumps.MenuItem("Connect Controller", callback=self._on_action)
        self._mapping_item = rumps.MenuItem("Button Mapping…", callback=self._show_mapping)
        self._reveal_item = rumps.MenuItem("Edit mappings file…", callback=self._reveal_mappings)
        self._reload_item = rumps.MenuItem("Reload mappings", callback=self._reload_mappings)
        self._dsu_item = rumps.MenuItem("DSU server", callback=self._toggle_dsu)
        self._keyboard_item = rumps.MenuItem(
            "Keyboard bridge (legacy)", callback=self._toggle_keyboard
        )
        self._recal_item = rumps.MenuItem(
            "Recalibrate sticks", callback=self._recalibrate
        )
        self._rumble_item = rumps.MenuItem("Test rumble", callback=self._test_rumble)
        self._pair_item = rumps.MenuItem(
            "Pair with this Mac…", callback=self._pair_controller
        )
        self._capture_item = rumps.MenuItem(
            "Capture raw packets", callback=self._toggle_raw_capture
        )
        self._login_item = rumps.MenuItem("Start at Login", callback=self._toggle_login)
        self._logs_item = rumps.MenuItem("Open logs…", callback=self._open_logs)
        self._version_item = rumps.MenuItem(f"{APP_NAME} v{APP_VERSION}")
        self._quit_item = rumps.MenuItem("Quit", callback=self._on_quit)

        self.menu = [
            self._status_item,
            self._detail_item,
            None,
            self._action_item,
            None,
            self._mapping_item,
            self._reveal_item,
            self._reload_item,
            None,
            self._dsu_item,
            self._keyboard_item,
            self._recal_item,
            self._rumble_item,
            self._pair_item,
            None,
            self._capture_item,
            self._login_item,
            self._logs_item,
            None,
            self._version_item,
            self._quit_item,
        ]
        self._sync_dsu()
        self._sync_keyboard(prompt=False)  # first tick prompts, once the UI is up
        self._sync_login_item()
        # packets/s: sampled by the UI tick
        self._rate_prev_count = 0
        self._rate_prev_time = time.monotonic()

        self._last_state = None
        self._accessibility_checked = False
        # Warn once per connection that nothing is consuming the input
        self._warned_no_consumer = False
        # Auto-connect bookkeeping: back off after a failure so a missing
        # controller cannot turn into a scan loop.
        self._auto_retry_after = 0.0
        self._auto_bluetooth_tried = False
        self._auto_attempt = False
        # Short error shown in the dropdown while idle — notifications are
        # unreliable when running from source, the menu is always visible
        self._idle_note = None
        self._apply_state('idle')

        self._timer = rumps.Timer(self._tick, self.REFRESH_INTERVAL)
        self._timer.start()

    # --- state machine ---

    def _current_state(self):
        if self.bridge.is_stopping:
            return 'stopping'
        if self.bridge.is_connected:
            return 'connected'
        if self.bridge.is_reconnecting:
            return 'reconnecting'
        if self.bridge.is_connecting:
            return 'connecting'
        if self.bridge.is_searching:
            # Watching is the resting state, not an operation in progress
            return 'watching' if self.bridge.watching else 'searching'
        return 'idle'

    def _apply_state(self, state):
        if state == 'watching':
            self.title = "🎮"
            self._status_item.title = "Waiting for controller…"
            self._detail_item.title = "   Hold the pair button, or plug in USB"
            self._action_item.title = "Stop waiting"
            self._action_item.set_callback(self._on_action)
        elif state == 'searching':
            self.title = "🔍"
            self._status_item.title = "Searching…"
            self._detail_item.title = " "
            self._action_item.title = "Cancel"
            self._action_item.set_callback(self._on_action)
        elif state == 'connecting':
            self.title = "🔗"
            self._status_item.title = "Connecting…"
            self._detail_item.title = " "
            self._action_item.title = "Cancel"
            self._action_item.set_callback(self._on_action)
        elif state == 'reconnecting':
            self.title = "🔍"
            self._status_item.title = "Reconnecting…"
            self._detail_item.title = " "
            self._action_item.title = "Cancel"
            self._action_item.set_callback(self._on_action)
        elif state == 'connected':
            self.title = "🟢"
            self._status_item.title = f"✓ {self.bridge.controller_name or 'Controller'}"
            self._detail_item.title = f"   {self.bridge.packet_count} pkts"
            self._action_item.title = "Disconnect"
            self._action_item.set_callback(self._on_action)
        elif state == 'stopping':
            self.title = "🎮"
            self._status_item.title = "Stopping…"
            self._detail_item.title = " "
            self._action_item.title = "Stopping…"
            self._action_item.set_callback(None)  # disabled
        else:  # idle
            self.title = "🎮"
            self._status_item.title = "○ Not connected"
            self._detail_item.title = " "
            self._action_item.title = "Connect Controller"
            self._action_item.set_callback(self._on_action)
        if state != 'connected':
            self._warned_no_consumer = False  # warn again next connection
        self._last_state = state

    def _tick(self, _):
        """Runs every REFRESH_INTERVAL on the main thread."""
        if not self._accessibility_checked:
            self._accessibility_checked = True
            # DSU needs no permissions — only the keyboard bridge does.
            if self.keyboard.enabled:
                self._check_accessibility()
            self._surface_mappings_messages()

        if self.keyboard.last_error:
            err = self.keyboard.last_error
            self.keyboard.last_error = None
            log.warning("user-visible keyboard error: %s", err)
            self._notify("Keyboard bridge", err)

        if self._capture_item.state and not self.bridge.raw_capture_active:
            self._capture_item.state = 0  # capture finished on its own

        if self.bridge.last_error:
            err = self.bridge.last_error
            self.bridge.last_error = None
            self._idle_note = err.splitlines()[0][:70]
            if self._auto_attempt:
                # Nobody asked for this attempt, so do not interrupt them
                # over it; the menubar still shows what happened.
                log.info("automatic connect failed: %s", err)
            else:
                log.warning("user-visible bridge error: %s", err)
                self._notify("Connection failed", err)
            self._auto_attempt = False

        if self.bridge.last_notice:
            notice = self.bridge.last_notice
            self.bridge.last_notice = None
            log.info("user-visible bridge notice: %s", notice)
            self._notify("Controller", notice)

        if self.dsu.last_error:
            err = self.dsu.last_error
            self.dsu.last_error = None
            log.warning("user-visible DSU error: %s", err)
            self._notify("DSU server", err)

        state = self._current_state()
        if state != self._last_state:
            self._apply_state(state)
        if state == 'idle':
            self._maybe_auto_connect()
        elif state == 'watching':
            self._auto_attempt = False   # it is running; nothing pending
        if state == 'idle' and self._idle_note:
            self._detail_item.title = f"   ⚠️ {self._idle_note}"
        elif state == 'connected':
            # in-place title update — no menu rebuild
            count = self.bridge.packet_count
            now = time.monotonic()
            elapsed = now - self._rate_prev_time
            rate = max(0.0, (count - self._rate_prev_count) / elapsed) if elapsed > 0 else 0.0
            self._rate_prev_count, self._rate_prev_time = count, now
            detail = f"   {count} pkts · {rate:.0f}/s"
            clients = self.dsu.client_count() if self.dsu.running else 0
            if clients:
                detail += f" · DSU: {clients} client{'s' if clients > 1 else ''}"
            self._detail_item.title = detail

            # Reading the controller is useless if nothing consumes it. That
            # state looks identical to "working" without saying so.
            if not clients and not self.keyboard.enabled:
                self._status_item.title = (
                    f"✓ {self.bridge.controller_name or 'Controller'} "
                    "— ⚠️ nothing listening"
                )
                self._detail_item.title = "   No DSU client · keyboard bridge off"
                if not self._warned_no_consumer:
                    self._warned_no_consumer = True
                    self._notify(
                        "Controller connected, but unused",
                        "No emulator is reading the DSU gamepad and the "
                        "keyboard bridge is off. Point your emulator at "
                        f"{self.dsu.host}:{self.dsu.port}, or enable the "
                        "keyboard bridge from this menu.",
                    )
            else:
                self._warned_no_consumer = False

    def _maybe_auto_connect(self):
        """Start watching for the controller, and stay watching.

        One background task covers both transports: it polls USB and scans
        for a controller in pairing mode, connecting to whichever appears.
        There is nothing to click and nothing to retry — pressing the pair
        button or plugging in the cable is the whole interaction.
        """
        if not self.mappings.auto_connect:
            return
        now = time.monotonic()
        if now < self._auto_retry_after:
            return
        # Only Bluetooth needs permission; a wired controller does not.
        if not usb_transport.is_connected() and not self._bluetooth_ready():
            self._auto_retry_after = now + 30.0
            self._idle_note = "Bluetooth permission needed for wireless"
            return

        self._auto_retry_after = now + 5.0
        self._auto_attempt = True
        self._idle_note = None
        log.info("watching for controller (usb + bluetooth)")
        self.bridge.connect(watch=True)

    # --- actions ---

    def _on_action(self, _):
        state = self._current_state()
        if state == 'idle':
            if not self._check_bluetooth():
                return
            self._idle_note = None
            self._auto_attempt = False
            self._auto_bluetooth_tried = True   # respect an explicit choice
            self.bridge.connect()
        elif state != 'stopping':
            # Stop auto-connect from immediately reconnecting what the user
            # just asked to disconnect.
            self.mappings.auto_connect = False
            self.bridge.disconnect()
        self._tick(None)

    def _show_mapping(self, _):
        m = self.mappings

        def fmt(value):
            return value if value else "—"

        lines = ["Output"]
        if self.dsu.running:
            clients = self.dsu.client_count()
            lines.append(
                f"  ✓ DSU gamepad on {self.dsu.host}:{self.dsu.port} "
                f"({clients} client{'' if clients == 1 else 's'})"
            )
            lines.append("    Analog sticks, full range — this is the real controller.")
        else:
            lines.append("  ○ DSU gamepad off — emulators cannot see the controller.")
        lines.append(
            f"  {'✓' if self.keyboard.enabled else '○'} Keyboard bridge "
            f"({'on' if self.keyboard.enabled else 'off'}, legacy)"
        )
        lines.append(
            f"  {'✓' if m.motion_enabled else '○'} Motion "
            f"({'decoding' if m.motion_enabled else 'not configured'})"
        )

        lines += [
            "",
            "DSU button layout (positional)",
            "  A→Circle  B→Cross  X→Triangle  Y→Square",
            "  L/R→L1/R1  ZL/ZR→L2/R2  −→Share  +→Options",
            "  Home→PS  Capture→Touch  LS/RS→L3/R3",
        ]
        aliases = m.dsu_aliases
        lines.append(
            "  " + (
                "  ".join(f"{k}→{v}" for k, v in sorted(aliases.items()))
                if aliases else
                "GL/GR/C: unmapped (no DSU slot — alias them in mappings.json)"
            )
        )
        lines += [
            "",
            f"Sticks: deadzone {m.stick_deadzone}, saturation {m.stick_saturation}",
        ]

        if self.keyboard.enabled:
            b, ls, rs = m.buttons, m.left_stick, m.right_stick
            lines += [
                "",
                "Keyboard bridge mapping",
                f"  A→{fmt(b.get('A'))}  B→{fmt(b.get('B'))}  X→{fmt(b.get('X'))}  Y→{fmt(b.get('Y'))}",
                f"  L→{fmt(b.get('L'))}  R→{fmt(b.get('R'))}  ZL→{fmt(b.get('ZL'))}  ZR→{fmt(b.get('ZR'))}",
                f"  +→{fmt(b.get('+'))}  -→{fmt(b.get('-'))}  Home→{fmt(b.get('HOME'))}  Capture→{fmt(b.get('CAPT'))}",
                f"  GL→{fmt(b.get('GL'))}  GR→{fmt(b.get('GR'))}  LS→{fmt(b.get('LS'))}  RS→{fmt(b.get('RS'))}",
                f"  C→{fmt(b.get('C'))}",
                f"  Left stick: {fmt(ls.get('up'))}/{fmt(ls.get('left'))}/{fmt(ls.get('down'))}/{fmt(ls.get('right'))} (U/L/D/R)",
                f"  Right stick: {fmt(rs.get('up'))}/{fmt(rs.get('left'))}/{fmt(rs.get('down'))}/{fmt(rs.get('right'))} (U/L/D/R)",
                f"  D-Pad: {fmt(b.get('DUP'))}/{fmt(b.get('DLEFT'))}/{fmt(b.get('DDOWN'))}/{fmt(b.get('DRIGHT'))} (U/L/D/R)",
                f"  Threshold: {m.stick_threshold}",
            ]

        lines += ["", f"Edit: {MAPPINGS_FILE}"]
        rumps.alert(title="Controller Output", message="\n".join(lines), ok="OK")

    def _reveal_mappings(self, _):
        Mappings.ensure_default_file()
        try:
            subprocess.Popen(["open", "-R", str(MAPPINGS_FILE)])
        except Exception as e:
            log.exception("could not open Finder")
            rumps.alert(title=APP_NAME, message=f"Could not reveal file: {e}", ok="OK")

    def _reload_mappings(self, _):
        # Release everything currently held to avoid stuck keys with the new map
        self.bridge.release_all_keys()
        ok = self.mappings.load()
        self._sync_dsu()
        self._sync_keyboard()
        self._surface_mappings_messages()
        if ok:
            self._notify("Mappings reloaded", f"Loaded from {MAPPINGS_FILE.name}")

    def _on_dsu_rumble(self, low, high):
        """Pass a DSU client's rumble request through to the controller.

        Works on either transport. With nothing connected this does nothing,
        which is better than failing a request the client is entitled to make.
        """
        self.bridge.set_rumble(low, high)

    def _toggle_dsu(self, _):
        self.mappings.set_dsu_enabled(not self.mappings.dsu_enabled)
        self._sync_dsu()

    def _sync_dsu(self):
        """Reconcile the DSU server with the current settings."""
        m = self.mappings
        settings_changed = (self.dsu.host, self.dsu.port) != (m.dsu_host, m.dsu_port)
        if self.dsu.running and (not m.dsu_enabled or settings_changed):
            self.dsu.stop()
        if settings_changed:
            self.dsu.host, self.dsu.port = m.dsu_host, m.dsu_port
        self.dsu.aliases = dict(m.dsu_aliases)
        if m.dsu_enabled and not self.dsu.running:
            self.dsu.start()
        self.dsu.set_connected(self.bridge.is_connected)
        if self.dsu.running:
            self._dsu_item.title = f"DSU gamepad ({self.dsu.host}:{self.dsu.port})"
            self._dsu_item.state = 1
        else:
            self._dsu_item.title = "DSU gamepad"
            self._dsu_item.state = 0

    def _toggle_keyboard(self, _):
        self.mappings.set_keyboard_enabled(not self.mappings.keyboard_enabled)
        self._sync_keyboard()

    def _sync_keyboard(self, prompt=True):
        """Reconcile the legacy keyboard bridge with the current settings."""
        want = self.mappings.keyboard_enabled
        if want and not self.keyboard.enabled:
            if self.keyboard.start():
                # Typing only works once Accessibility is granted, and the
                # prompt is pointless until the user actually wants keys.
                if prompt:
                    self._check_accessibility()
            else:
                self.mappings.set_keyboard_enabled(False)
        elif not want and self.keyboard.enabled:
            self.keyboard.stop()
        self._keyboard_item.state = 1 if self.keyboard.enabled else 0

    def _recalibrate(self, _):
        self.bridge.calibration.reset()
        self._notify(
            "Sticks", "Let go of both sticks — the centre is being re-measured."
        )

    def _tell(self, subtitle, message):
        """Say something in answer to a click, where it cannot be missed.

        Notifications are the wrong tool for this: macOS drops them silently
        if the app has no notification permission or Focus is on, and the
        result is a menu item that appears to do nothing at all. Anything the
        user explicitly asked for gets a dialog.
        """
        rumps.alert(title=APP_NAME, message=f"{subtitle}\n\n{message}", ok="OK")

    def _wired_transport(self, feature):
        """The USB transport, or None having explained why there isn't one."""
        usb = getattr(self.bridge, "usb", None)
        if usb is not None and usb.connected:
            return usb
        self._tell(
            feature,
            "This needs a USB cable. The controller is either not connected or "
            "connected over Bluetooth, where this is not supported yet.",
        )
        return None

    def _test_rumble(self, _):
        if not self.bridge.is_connected:
            self._tell("Rumble", "Connect a controller first.")
            return

        def demo():
            # Off the UI thread: this holds for over a second, and a menubar
            # app that freezes while buzzing would look like a crash.
            try:
                for amplitude in (18000, 40000, 0xFFFF):
                    if not self.bridge.set_rumble(amplitude, amplitude):
                        return
                    time.sleep(0.3)
                self.bridge.set_rumble(0, 0)
            except Exception:
                log.exception("rumble test failed")

        if not self.bridge.set_rumble(0, 0):
            self._tell("Rumble", "The controller is not connected.")
            return
        threading.Thread(target=demo, name="rumble-test", daemon=True).start()

    def _pair_controller(self, _):
        usb = self._wired_transport("Pairing")
        if usb is None:
            return
        host = controller_pairing.local_bluetooth_address()
        if host is None:
            self._tell("Pairing", "Could not read this Mac's Bluetooth address.")
            return

        # Verify before asking. Step three of the exchange is a cryptographic
        # check, so a clean dry run proves the whole thing works while leaving
        # the controller untouched — no reason to put the warning in front of
        # someone if it was going to fail anyway.
        try:
            usb.run_pairing(host, commit=False)
        except Exception as e:
            log.exception("pairing dry run failed")
            self._tell("Pairing failed", str(e))
            return

        confirmed = rumps.alert(
            title="Pair this controller with your Mac?",
            message=(
                "The controller will remember this Mac "
                f"({controller_pairing.format_address(host)}) and reconnect to "
                "it instead of advertising for any console to claim.\n\n"
                "It only keeps one host's pairing, so your Switch 2 will most "
                "likely stop reconnecting to this controller until you pair it "
                "there again.\n\n"
                "The exchange has already been verified without writing "
                "anything. Continuing is what writes it."
            ),
            ok="Pair",
            cancel="Cancel",
        )
        if confirmed != 1:
            return

        try:
            usb.run_pairing(host, commit=True)
        except Exception as e:
            log.exception("pairing failed")
            self._tell("Pairing failed", str(e))
            return
        self._tell(
            "Paired",
            f"The controller now knows this Mac ({controller_pairing.format_address(host)}).",
        )

    def _toggle_raw_capture(self, _):
        if self.bridge.raw_capture_active:
            self.bridge.start_raw_capture(0)
        else:
            self.bridge.start_raw_capture()
            self._notify(
                "Raw capture",
                f"Logging the next {ControllerBridge.RAW_CAPTURE_LIMIT} reports "
                f"to bridge.log.",
            )
        self._capture_item.state = 1 if self.bridge.raw_capture_active else 0

    def _login_service(self):
        """SMAppService for the main app, or None when unavailable.

        Registration only works from a bundled .app (py2app sets sys.frozen),
        and needs the pyobjc ServiceManagement framework (macOS 13+).
        """
        if SMAppService is None or not getattr(sys, "frozen", False):
            return None
        try:
            return SMAppService.mainAppService()
        except Exception:
            log.exception("SMAppService unavailable")
            return None

    def _sync_login_item(self):
        svc = self._login_service()
        if svc is None:
            self._login_item.set_callback(None)  # disabled outside a bundled .app
            self._login_item.state = 0
            return
        self._login_item.set_callback(self._toggle_login)
        self._login_item.state = 1 if svc.status() == 1 else 0  # 1 = enabled

    def _toggle_login(self, _):
        svc = self._login_service()
        if svc is None:
            return
        try:
            if svc.status() == 1:
                res = svc.unregisterAndReturnError_(None)
            else:
                res = svc.registerAndReturnError_(None)
            ok, err = res if isinstance(res, tuple) else (res, None)
            if not ok:
                raise RuntimeError(err)
        except Exception as e:
            log.exception("login item toggle failed")
            rumps.alert(
                title=APP_NAME,
                message=f"Could not update the login item: {e}",
                ok="OK",
            )
        self._sync_login_item()

    def _open_logs(self, _):
        try:
            subprocess.Popen(["open", str(LOG_DIR)])
        except Exception as e:
            log.exception("could not open logs folder")
            rumps.alert(title=APP_NAME, message=f"Could not open logs: {e}", ok="OK")

    def _on_quit(self, _):
        log.info("quitting")
        self.bridge.disconnect(wait=True, timeout=2.0)
        # Belt & suspenders: never leave a key logically held after exit
        self.keyboard.stop()
        self.dsu.stop()
        rumps.quit_application()

    # --- startup checks ---

    def _check_accessibility(self):
        if AXIsProcessTrusted is None:
            return
        if not AXIsProcessTrusted():
            log.warning("Accessibility permission not granted")
            clicked_ok = rumps.alert(
                title="Accessibility Required",
                message=(
                    "The legacy keyboard bridge needs Accessibility access to "
                    "simulate key presses.\n\n"
                    "Grant access in:\n"
                    "System Settings → Privacy & Security → Accessibility\n\n"
                    "You may need to quit and relaunch after granting access.\n\n"
                    "You can skip this entirely: the DSU gamepad output needs "
                    "no permissions and gives emulators true analog sticks."
                ),
                ok="Open System Settings",
                cancel="Later",
            )
            if clicked_ok == 1:
                subprocess.Popen([
                    "open",
                    "x-apple.systempreferences:com.apple.preference.security"
                    "?Privacy_Accessibility",
                ])

    def _bluetooth_ready(self):
        """Silent permission check, for attempts the user did not ask for.

        _check_bluetooth() puts a dialog up, which is right after a click and
        wrong when we are polling in the background.
        """
        try:
            from CoreBluetooth import CBManager
            return int(CBManager.authorization()) not in (1, 2)
        except Exception:
            return True  # cannot tell; let the scan surface any problem

    def _check_bluetooth(self):
        """Return False (and explain) when Bluetooth permission is denied.

        CBManagerAuthorization: 0 not determined (prompt will appear on first
        scan), 1 restricted, 2 denied, 3 allowed.
        """
        try:
            from CoreBluetooth import CBManager  # ships with bleak's backend
            auth = int(CBManager.authorization())
        except Exception:
            return True  # can't check — let the scan surface any error
        if auth in (1, 2):
            log.warning("Bluetooth permission denied (auth=%s)", auth)
            clicked_ok = rumps.alert(
                title="Bluetooth Permission Required",
                message=(
                    f"macOS is blocking Bluetooth access for {APP_NAME}.\n\n"
                    "Grant it in:\n"
                    "System Settings → Privacy & Security → Bluetooth\n\n"
                    "⚠️ When running from source, the permission belongs to "
                    "Terminal (or your Python interpreter) — enable that entry, "
                    "then relaunch."
                ),
                ok="Open System Settings",
                cancel="Later",
            )
            if clicked_ok == 1:
                subprocess.Popen([
                    "open",
                    "x-apple.systempreferences:com.apple.preference.security"
                    "?Privacy_Bluetooth",
                ])
            return False
        return True

    def _surface_mappings_messages(self):
        if self.mappings.last_error:
            err = self.mappings.last_error
            self.mappings.last_error = None
            log.warning("user-visible mappings error: %s", err)
            rumps.alert(title="Mappings", message=err, ok="OK")
        if self.mappings.last_warning:
            warn = self.mappings.last_warning
            self.mappings.last_warning = None
            log.warning("user-visible mappings warning: %s", warn)
            self._notify("Mappings", warn)

    # --- helpers ---

    def _notify(self, subtitle, message):
        try:
            rumps.notification(APP_NAME, subtitle, message)
        except Exception:
            # notifications need a bundled .app — fall back to alert
            rumps.alert(title=APP_NAME, message=f"{subtitle}\n{message}", ok="OK")


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print(f"\n🎮 {APP_NAME}")
    print("   App is running in the menu bar.")
    print(f"   Mappings: {MAPPINGS_FILE}")
    print(f"   Logs:     {LOG_DIR / 'bridge.log'}\n")
    log.info("starting %s", APP_NAME)
    Switch2BridgeApp().run()
