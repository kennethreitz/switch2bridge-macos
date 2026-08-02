"""Headless tests for Switch2Bridge logic (no BLE, no real key presses)."""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import Switch2Bridge as S2B
from outputs import KeyboardOutput

# --- record key presses instead of sending them ---
events = []


class FakeController:
    def press(self, key):
        events.append(("press", key))

    def release(self, key):
        events.append(("release", key))


def make_keyboard(mappings):
    """A KeyboardOutput that records instead of typing."""
    keyboard = KeyboardOutput(mappings)
    keyboard._controller = FakeController()
    return keyboard

FAILURES = []

def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)

# ============ Mappings._parse_key ============
print("== _parse_key ==")
M = S2B.Mappings
check("null unmapped", M._parse_key(None) is None)
check("<none> unmapped", M._parse_key("<none>") is None)
check("single char", M._parse_key("a") == "a")
check("uppercase normalized", M._parse_key("Z") == "z")
check("f-key", M._parse_key("<f12>") == "<f12>")
check("arrow", M._parse_key("<up>") == "<up>")
try:
    M._parse_key("ctrl")
    check("multi-char rejected", False)
except ValueError:
    check("multi-char rejected", True)
try:
    M._parse_key(3)
    check("int rejected", False)
except ValueError:
    check("int rejected", True)

# ============ Mappings._apply validation ============
print("== _apply ==")
m = M()
cfg = json.loads(json.dumps(M.DEFAULT))
cfg["buttons"]["TYPO_BTN"] = "y"
cfg["sticks"]["threshold"] = 5.0
cfg["sticks"]["left"]["upp"] = "t"
m._apply(cfg)
check("unknown button warned", "TYPO_BTN" in (m.last_warning or ""), m.last_warning)
check("threshold clamped", m.stick_threshold == 0.9, m.stick_threshold)
check("unknown stick dir warned", "upp" in (m.last_warning or ""))
check("typo button not applied", "TYPO_BTN" not in m.buttons)
check("C default unmapped", m.buttons.get("C") is None)

# load() with corrupt file
print("== load ==")
tmpdir = Path(tempfile.mkdtemp())
S2B.CONFIG_DIR = tmpdir
S2B.MAPPINGS_FILE = tmpdir / "mappings.json"
(tmpdir / "mappings.json").write_text("{not json")
m2 = M()
ok = m2.load()
check("corrupt file -> ok False", ok is False)
check("corrupt file -> error set", m2.last_error is not None)
check("corrupt file -> defaults applied", m2.buttons.get("A") == "z")

S2B.MAPPINGS_FILE.unlink()
m3 = M()
ok = m3.load()
check("first launch writes default + ok True", ok is True and S2B.MAPPINGS_FILE.exists())

# ============ key ref-counting ============
print("== _set_key ref counting ==")
mm = M()
mm._apply(json.loads(json.dumps(M.DEFAULT)))
kb = make_keyboard(mm)
br = S2B.ControllerBridge(mm, keyboard_output=kb)
events.clear()

# two sources sharing key "z"
kb._set_key("A", "z", True)
kb._set_key("B", "z", True)
kb._set_key("A", "z", False)
check("shared key still held", ("release", "z") not in events, events)
kb._set_key("B", "z", False)
check("shared key released once both up", events == [("press", "z"), ("release", "z")], events)

# mapping change while held
events.clear()
kb._set_key("A", "z", True)
kb._set_key("A", "q", True)  # remapped mid-hold
check("remap mid-hold swaps keys", events == [("press", "z"), ("release", "z"), ("press", "q")], events)
kb._set_key("A", None, True)  # unmapped mid-hold
check("unmap mid-hold releases", events[-1] == ("release", "q"), events)

# release_all, driven through the bridge to prove the delegation works
events.clear()
kb._set_key("A", "z", True)
kb._set_key("ls_up", "w", True)
br.release_all_keys()
check("release_all releases everything",
      sorted(e for e in events if e[0] == "release") == [("release", "w"), ("release", "z")], events)
check("release_all clears state", not kb._key_refs and not kb._source_keys)

# ============ hysteresis ============
print("== stick hysteresis ==")
events.clear()
kb._set_stick_key("ls_up", "w", 0.45)   # below 0.5 -> no press
check("below threshold no press", events == [])
kb._set_stick_key("ls_up", "w", 0.55)   # above -> press
check("above threshold press", events == [("press", "w")])
kb._set_stick_key("ls_up", "w", 0.45)   # above 0.4 (=0.8*0.5) -> stays held
check("hysteresis holds", events == [("press", "w")])
kb._set_stick_key("ls_up", "w", 0.35)   # below 0.4 -> release
check("below release threshold releases", events[-1] == ("release", "w"))

# ============ _on_data packet parsing ============
print("== _on_data ==")
def packet(b2=0, b3=0, b4=0, lx=2048, ly=2048, rx=2048, ry=2048):
    d = bytearray(11)
    d[2], d[3], d[4] = b2, b3, b4
    d[5] = lx & 0xFF
    d[6] = ((lx >> 8) & 0x0F) | ((ly & 0x0F) << 4)
    d[7] = (ly >> 4) & 0xFF
    d[8] = rx & 0xFF
    d[9] = ((rx >> 8) & 0x0F) | ((ry & 0x0F) << 4)
    d[10] = (ry >> 4) & 0xFF
    return bytes(d)

kb2 = make_keyboard(mm)
br2 = S2B.ControllerBridge(mm, keyboard_output=kb2)
events.clear()
br2._on_data(None, packet(b2=0x02))          # A pressed
check("A press -> z", ("press", "z") in events, events)
br2._on_data(None, packet(b2=0x00))          # A released
check("A release -> z up", ("release", "z") in events, events)

events.clear()
br2._on_data(None, packet(b4=0x10))          # C pressed, unmapped by default
check("unmapped C -> nothing", events == [], events)
check("C decoded on state", br2.last_state.pressed("C"))
check("C is not CAPT", not br2.last_state.pressed("CAPT"))

events.clear()
br2._on_data(None, packet(ly=4000))          # left stick up
check("stick up -> w", ("press", "w") in events, events)
br2._on_data(None, packet(ly=100))           # left stick down
check("stick flip: w released", ("release", "w") in events, events)
check("stick flip: s pressed", ("press", "s") in events, events)

events.clear()
br2._on_data(None, b"\x00\x01\x02")          # short packet
check("short packet ignored", events == [] and br2.packet_count == 5)

# dpad + stick sharing same key
mm.left_stick["up"] = "<up>"  # same as DUP
up_key = kb2._resolve("<up>")
events.clear()
br2._on_data(None, packet(b3=0x08, ly=4000))  # DUP + stick up together
br2._on_data(None, packet(b3=0x00, ly=4000))  # DUP released, stick still up
check("dpad/stick shared key not stolen", ("release", up_key) not in events, events)
br2._on_data(None, packet())
check("shared key released at rest", ("release", up_key) in events)

# ============ connect/cancel lifecycle (mock BLE) ============
print("== lifecycle ==")
# These drive a mocked BLE stack; without this the bridge would find a real
# controller on USB and none of the Bluetooth paths would be exercised.
mm.usb_enabled = False

class MockScanner:
    delay = 0.3
    result = {}
    queue = []  # optional per-round results, popped first
    @staticmethod
    async def discover(timeout=None, return_adv=False):
        await asyncio.sleep(MockScanner.delay)
        if MockScanner.queue:
            return MockScanner.queue.pop(0)
        return MockScanner.result

S2B.BleakScanner = MockScanner

# not found path (shrink the 30 s initial window for the test)
S2B.INITIAL_SCAN_WINDOW = 0.0
br3 = S2B.ControllerBridge(mm)
br3.connect()
br3._thread.join(3)
check("not found -> error", br3.last_error and "not found" in br3.last_error, br3.last_error)
check("not found -> thread exits", not br3._thread.is_alive())

# cancel during scan
MockScanner.delay = 5.0
br4 = S2B.ControllerBridge(mm)
br4.connect()
time.sleep(0.5)
t0 = time.time()
br4.disconnect(wait=True, timeout=4.0)
dt = time.time() - t0
check("cancel interrupts scan quickly", dt < 1.5 and not br4._thread.is_alive(), f"dt={dt:.2f}")
check("cancel -> no error surfaced", br4.last_error is None, br4.last_error)

# connect while thread alive is a no-op; reconnect after cancel works
MockScanner.delay = 0.1
br4.connect()
br4._thread.join(3)
check("reconnect after cancel runs", br4.last_error is not None)

# mock full session: device found, client streams then "drops"
class MockClient:
    instances = []
    def __init__(self, address, timeout=None):
        self.address = address
        self._connected = False
        # Real clients dispatch per characteristic; the bridge now subscribes
        # to the command-response channel as well as the input report.
        self.notify_cbs = {}
        self.writes = []
        MockClient.instances.append(self)

    @property
    def notify_cb(self):
        """Callback for the input report characteristic."""
        return self.notify_cbs.get(S2B.INPUT_CHAR_UUID)

    async def write_gatt_char(self, uuid, data, response=True):
        self.writes.append((str(uuid), bytes(data)))
    @property
    def is_connected(self):
        return self._connected
    async def connect(self):
        self._connected = True
    async def disconnect(self):
        self._connected = False
    async def start_notify(self, uuid, cb):
        self.notify_cbs[str(uuid)] = cb
    async def stop_notify(self, uuid):
        self.notify_cbs.pop(str(uuid), None)

S2B.BleakClient = MockClient

class Adv:
    manufacturer_data = {0x057E: b"\x01\x69\x20\xff"}
class Dev:
    name = "Pro Controller (S2)"

MockScanner.result = {"AA:BB": (Dev(), Adv())}
MockScanner.delay = 0.05

br5 = S2B.ControllerBridge(mm)
br5.connect()
time.sleep(0.6)
check("session connected", br5.is_connected and br5.controller_name == "Pro Controller (S2)")
client = MockClient.instances[-1]
client.notify_cb(None, packet(b2=0x02))
check("notify parsed while connected", br5.packet_count == 1)

# unexpected drop -> reconnect
client._connected = False
time.sleep(0.4)
check("drop -> reconnecting notice", br5.last_notice and "reconnect" in br5.last_notice.lower(), br5.last_notice)
time.sleep(0.6)
check("auto-reconnected", br5.is_connected)
check("reconnected notice", br5.last_notice and "Reconnected" in br5.last_notice, br5.last_notice)

# user disconnect -> clean stop, no reconnect
br5.disconnect(wait=True, timeout=3.0)
check("user disconnect stops thread", not br5._thread.is_alive())
check("no error after user disconnect", br5.last_error is None, br5.last_error)
check("client disconnected", not MockClient.instances[-1].is_connected)

# initial search keeps scanning: nothing on round 1, found on round 2
print("== initial scan retry ==")
S2B.INITIAL_SCAN_WINDOW = 10.0
MockScanner.queue = [{}]
br6 = S2B.ControllerBridge(mm)
br6.connect()
time.sleep(2.0)  # scan (0.05) + 1 s pause + scan + connect
check("found on second scan round", br6.is_connected)
check("no error during retry", br6.last_error is None, br6.last_error)
br6.disconnect(wait=True, timeout=3.0)

# scan error -> actionable messages
print("== scan error messages ==")
msg = S2B.ControllerBridge._scan_error_message(Exception("CBCentralManager is not authorized"))
check("unauthorized -> permission hint", "Privacy & Security" in msg and "Terminal" in msg, msg)
msg = S2B.ControllerBridge._scan_error_message(Exception("Bluetooth device is turned off"))
check("powered off -> enable hint", "turned off" in msg, msg)
msg = S2B.ControllerBridge._scan_error_message(Exception("boom"))
check("generic passthrough", "boom" in msg, msg)

# discovery filters: both Nintendo company IDs + name fallback
print("== discovery filters ==")
class AdvSIG:
    manufacturer_data = {0x0553: b"\x01\x69\x20\x00"}  # BT SIG assigned ID
class AdvVID:
    manufacturer_data = {0x057E: b"\x01\x69\x20\x00"}  # Nintendo USB VID
class AdvOther:
    manufacturer_data = {0x004C: b"\x10\x05"}          # random Apple device
class NoName:
    name = None
class Named:
    name = "Pro Controller (S2)"

MockScanner.queue = []
MockScanner.delay = 0.01
br7 = S2B.ControllerBridge(mm)

MockScanner.result = {"S1": (NoName(), AdvSIG())}
a, n = asyncio.run(br7._find_controller())
check("SIG id 0x0553 matched (no name)", a == "S1" and n == "Switch 2 Pro Controller", (a, n))

MockScanner.result = {"S2": (NoName(), AdvVID())}
a, n = asyncio.run(br7._find_controller())
check("USB VID 0x057e matched", a == "S2")

MockScanner.result = {"S3": (Named(), AdvOther())}
a, n = asyncio.run(br7._find_controller())
check("name fallback", a == "S3" and "Pro Controller" in n)

MockScanner.result = {"S4": (NoName(), AdvOther())}
a, n = asyncio.run(br7._find_controller())
check("unrelated device ignored", a is None)

# failing reconnect attempts must not spam last_error; only the final
# give-up message surfaces
print("== reconnect error spam ==")
S2B.INITIAL_SCAN_WINDOW = 10.0
S2B.RECONNECT_WINDOW = 2.0
MockScanner.delay = 0.05
MockScanner.queue = []
MockScanner.result = {"AA:BB": (Dev(), Adv())}
br8 = S2B.ControllerBridge(mm)
br8.connect()
time.sleep(0.6)
check("spam test: connected", br8.is_connected)

orig_connect = MockClient.connect
async def _failing_connect(self):
    raise RuntimeError("nope")
MockClient.connect = _failing_connect
MockClient.instances[-1]._connected = False  # unexpected drop
time.sleep(1.2)  # several failing reconnect attempts happen here
check("no error surfaced while retrying", br8.last_error is None, br8.last_error)
time.sleep(2.5)  # past the (shrunk) reconnect window
check("final give-up error surfaced",
      br8.last_error and "reconnect" in br8.last_error.lower(), br8.last_error)
check("spam test: thread ended", not br8._thread.is_alive())
MockClient.connect = orig_connect

# ============ wired transport preference ============
print("== usb preference ==")
import usb_transport

_real_is_connected = usb_transport.is_connected
mm_usb = M()
mm_usb._apply(json.loads(json.dumps(M.DEFAULT)))
check("usb enabled by default", mm_usb.usb_enabled is True)

br_usb = S2B.ControllerBridge(mm_usb)
usb_transport.is_connected = lambda: False
check("no cable -> falls through to bluetooth", br_usb._try_usb() is False)

mm_usb.usb_enabled = False
usb_transport.is_connected = lambda: True
check("usb disabled in config -> not tried", br_usb._try_usb() is False)
usb_transport.is_connected = _real_is_connected

cfg_usb = json.loads(json.dumps(M.DEFAULT))
cfg_usb["controller"]["usb"] = False
m_off = M(); m_off._apply(cfg_usb)
check("usb:false honoured", m_off.usb_enabled is False)

# the wired report is the bluetooth report with a HID report id in front
body = bytes.fromhex("7e23000000b8378472688638") + bytes(51)
check("usb body decodes with the same parser",
      S2B.ControllerBridge(mm_usb)._build_state(body) is not None)

# a corrupt mappings.json must never be clobbered by the DSU toggle
print("== dsu toggle vs corrupt file ==")
S2B.MAPPINGS_FILE.write_text("{broken json")
m4 = S2B.Mappings()
m4.set_dsu_enabled(False)
check("corrupt file untouched", S2B.MAPPINGS_FILE.read_text() == "{broken json")
check("in-memory toggle still applied", m4.dsu_enabled is False)


# ============ auto-connect / watching ============
print("== auto connect ==")
import usb_transport as _ut

class FakeApp:
    """Exercises _maybe_auto_connect without building a real menubar app."""
    _maybe_auto_connect = S2B.Switch2BridgeApp._maybe_auto_connect
    def __init__(self, mappings, bluetooth_ready=True):
        self.mappings = mappings
        self.calls = []
        self._auto_retry_after = 0.0
        self._auto_attempt = False
        self._idle_note = "stale"
        self._bt_ready = bluetooth_ready
        self.bridge = self
    def connect(self, watch=False):
        self.calls.append(watch)
    def _bluetooth_ready(self):
        return self._bt_ready

_orig = _ut.is_connected
m_auto = M(); m_auto._apply(json.loads(json.dumps(M.DEFAULT)))
check("auto_connect on by default", m_auto.auto_connect is True)

# the point of the feature: one watcher covering both transports
_ut.is_connected = lambda: False
app = FakeApp(m_auto)
app._maybe_auto_connect()
check("starts a watcher", app.calls == [True], app.calls)
check("idle note cleared", app._idle_note is None)

# and it does not stack watchers on top of each other
app._maybe_auto_connect()
check("backs off rather than restarting", app.calls == [True], app.calls)

# wired needs no Bluetooth permission at all
_ut.is_connected = lambda: True
app2 = FakeApp(m_auto, bluetooth_ready=False)
app2._maybe_auto_connect()
check("wired watched even with bluetooth denied", app2.calls == [True], app2.calls)

# wireless with permission denied should back off, not spin
_ut.is_connected = lambda: False
app3 = FakeApp(m_auto, bluetooth_ready=False)
app3._maybe_auto_connect()
check("bluetooth denied -> no watcher", app3.calls == [], app3.calls)
check("bluetooth denied -> explains why", "permission" in (app3._idle_note or "").lower(),
      app3._idle_note)
check("bluetooth denied -> long backoff", app3._auto_retry_after > 0)

# disabled in config
cfg_off = json.loads(json.dumps(M.DEFAULT))
cfg_off["controller"]["auto_connect"] = False
m_off = M(); m_off._apply(cfg_off)
check("auto_connect:false honoured", m_off.auto_connect is False)
app4 = FakeApp(m_off)
app4._maybe_auto_connect()
check("disabled -> never watches", app4.calls == [], app4.calls)

_ut.is_connected = _orig

# ============ watch mode keeps looking instead of erroring ============
print("== watch mode ==")
S2B.INITIAL_SCAN_WINDOW = 0.0        # would normally give up immediately
MockScanner.queue = []
MockScanner.result = {}              # nothing to find
MockScanner.delay = 0.05
br_watch = S2B.ControllerBridge(mm)
br_watch.connect(watch=True)
time.sleep(1.2)
check("watching -> still running after the window", br_watch._thread.is_alive())
check("watching -> no 'not found' error", br_watch.last_error is None, br_watch.last_error)
check("watching flag set", br_watch.watching is True)

# controller enters pairing mode: it should be picked up without a click
MockScanner.result = {"AA:BB": (Dev(), Adv())}
time.sleep(1.5)
check("connects once it starts advertising", br_watch.is_connected, br_watch.last_error)

# it sleeps again -> back to watching, not an error
MockClient.instances[-1]._connected = False
time.sleep(1.0)
check("drop -> keeps watching", br_watch._thread.is_alive() and not br_watch.last_error,
      br_watch.last_error)
br_watch.disconnect(wait=True, timeout=3.0)
check("stop ends the watcher", not br_watch._thread.is_alive())
check("watching cleared on stop", br_watch.watching is False)
S2B.INITIAL_SCAN_WINDOW = 10.0

# ============ USB hot-plug during a Bluetooth session ============
print("== usb hot-plug ==")
_orig_is_conn = _ut.is_connected
_orig_transport = _ut.USBTransport

class FakeUSB:
    """Stands in for a real wired controller."""
    def __init__(self, on_report, on_error=None):
        self.connected = False
    def connect(self):
        self.connected = True
        return True
    def read_stick_calibration(self):
        return None, None
    def read_gyro_bias(self):
        return (0.0, 0.0, 0.0)
    def set_player_light(self, pattern):
        pass
    def set_rumble(self, low, high):
        pass
    def disconnect(self):
        self.connected = False

mm_hot = M(); mm_hot._apply(json.loads(json.dumps(M.DEFAULT)))
MockScanner.queue = []
MockScanner.result = {"AA:BB": (Dev(), Adv())}
MockScanner.delay = 0.05
S2B.INITIAL_SCAN_WINDOW = 10.0

_ut.is_connected = lambda: False          # no cable yet
_ut.USBTransport = FakeUSB
br_hot = S2B.ControllerBridge(mm_hot)
br_hot.connect(watch=True)
time.sleep(1.2)
check("bluetooth session established", br_hot.is_connected, br_hot.last_error)
check("transport is ble", br_hot.transport == "ble", br_hot.transport)

_ut.is_connected = lambda: True           # cable goes in mid-session
time.sleep(2.5)
check("upgraded to wired", br_hot.transport == "usb", br_hot.transport)
check("still connected across the switch", br_hot.is_connected)
check("explained as an upgrade, not a failure",
      "wired" in (br_hot.last_notice or "").lower(), br_hot.last_notice)
check("no error raised for the switch", br_hot.last_error is None, br_hot.last_error)
br_hot.disconnect(wait=True, timeout=3.0)

# a cable that cannot actually be claimed must not strand the user
class DeadUSB(FakeUSB):
    def connect(self):
        self.last_error = "nope"
        return False
_ut.USBTransport = DeadUSB
br_fall = S2B.ControllerBridge(mm_hot)
br_fall.connect(watch=True)
time.sleep(2.5)
check("falls back to bluetooth when the cable cannot be claimed",
      br_fall.is_connected and br_fall.transport == "ble", br_fall.transport)
br_fall.disconnect(wait=True, timeout=3.0)

# with usb disabled the bluetooth session is left alone
_ut.USBTransport = FakeUSB
mm_no_usb = M()
cfg_nu = json.loads(json.dumps(M.DEFAULT)); cfg_nu["controller"]["usb"] = False
mm_no_usb._apply(cfg_nu)
br_stay = S2B.ControllerBridge(mm_no_usb)
br_stay.connect(watch=True)
time.sleep(1.2)
check("session up with usb disabled", br_stay.is_connected, br_stay.last_error)
time.sleep(1.6)
check("cable ignored when usb is off",
      br_stay.is_connected and br_stay.transport == "ble", br_stay.transport)
br_stay.disconnect(wait=True, timeout=3.0)

# ============ cable arriving mid-scan ============
# The regression this guards: the cable used to be looked at only between
# Bluetooth steps, so plugging in while a scan or handshake was in flight did
# nothing until that step ran itself out — up to ~20 s of a dead cable.
print("== usb during a bluetooth scan ==")
_ut.USBTransport = FakeUSB
MockScanner.queue = []
MockScanner.result = {}          # nothing to find; the scan just runs long
MockScanner.delay = 8.0          # stands in for a slow scan or handshake
_ut.is_connected = lambda: False

br_scan = S2B.ControllerBridge(mm_hot)
br_scan.connect(watch=True)
time.sleep(1.0)                  # let it get well inside the scan
check("scanning, not yet wired", br_scan.transport != "usb", br_scan.transport)
_ut.is_connected = lambda: True  # cable goes in mid-scan
time.sleep(3.0)                  # far short of the 8 s the scan would take
check("cable seen without waiting for the scan to finish",
      br_scan.transport == "usb", br_scan.transport)
check("wired session is live", br_scan.is_connected)
br_scan.disconnect(wait=True, timeout=3.0)

# an unclaimable cable must not make the loop abandon Bluetooth every second
print("== unclaimable cable does not spin ==")
_ut.USBTransport = DeadUSB
MockScanner.delay = 0.3
br_spin = S2B.ControllerBridge(mm_hot)
br_spin.connect(watch=True)
time.sleep(1.0)
attempts_start = br_spin._usb_retry_after
time.sleep(2.0)
check("backing off a cable it cannot claim",
      br_spin._usb_retry_after == attempts_start
      and br_spin._usb_retry_after > time.monotonic(),
      br_spin._usb_retry_after)
br_spin.disconnect(wait=True, timeout=3.0)

_ut.is_connected = _orig_is_conn
_ut.USBTransport = _orig_transport
MockScanner.delay = 0.3

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
    sys.exit(1)
print("ALL TESTS PASSED")
