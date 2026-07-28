# 🎮 Switch 2 Pro Controller — macOS BLE Bridge

**The first working Bluetooth LE client for the Nintendo Switch 2 Pro Controller on macOS.**

A Python menubar app that connects to the Switch 2 Pro Controller — **wired or over Bluetooth LE** — and exposes it to emulators as a **real analog gamepad** via the DSU (cemuhook) protocol. No driver, no kext, no permissions.

[![CI](https://github.com/mlstr0m/switch2bridge-macos/actions/workflows/ci.yml/badge.svg)](https://github.com/mlstr0m/switch2bridge-macos/actions/workflows/ci.yml)
[![macOS](https://img.shields.io/badge/macOS-Ventura%2B-blue?logo=apple)](https://www.apple.com/macos)
[![Python](https://img.shields.io/badge/Python-3.9%2B-green?logo=python)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

---

## ⚠️ What This Is (and Isn't)

**This is NOT a system driver.** The controller will not appear in System Settings, and macOS games that use Apple's GameController framework will not see it. See [Why not a real HID device?](#-why-not-a-real-hid-device) for the specific, verified reason.

**This IS:**
- ✅ A BLE client that reads controller inputs via Bluetooth Low Energy
- ✅ A **DSU/cemuhook gamepad server** — true analog sticks in Dolphin, Cemu, Ryujinx and any other DSU client
- ✅ A reference implementation of the Switch 2 Pro Controller BLE protocol
- ✅ An optional legacy keyboard bridge, for emulators that read only the keyboard

## 🚀 Features

- ✅ **Real analog gamepad output** — full 12-bit sticks over DSU, no driver needed
- ✅ **Stick calibration** — resting centre learned per connection, full deflection actually reaches 100%
- ✅ **Radial deadzone + saturation** — configurable, no cross-shaped dead region on diagonals
- ✅ **Full button mapping** — all buttons, triggers, D-pad, verified against a real capture
- ✅ **Grip buttons** — Switch 2 exclusive GL/GR, plus the new C button
- ✅ **No pairing required** — bypasses macOS Bluetooth limitations
- ✅ **Wired mode** — 250 Hz / 4 ms over USB, against 33 Hz / 30 ms on Bluetooth
- ✅ **Auto-connect** — picks up a plugged-in controller at launch, no clicking
- ✅ **Factory stick calibration** — read from the controller's own flash, per unit
- ✅ **Auto-reconnect** — if the controller sleeps or drops, the bridge retries
- ✅ **Zero permissions by default** — DSU needs neither Accessibility nor anything else
- ✅ **Legacy keyboard bridge** — still there, opt-in, for keyboard-only emulators
- ✅ **Start at Login** — one click in the menubar (bundled .app, macOS 13+)
- ✅ **Protocol tools** — capture and analyse raw BLE reports yourself

## 🤔 Why This Exists

The Nintendo Switch 2 Pro Controller (Product ID: `0x2069`) doesn't work with macOS natively:

| Method | Status | Notes |
|--------|--------|-------|
| USB | ✅ | Enumerates, but stays silent until an init sequence is sent to its **vendor-class interface** — not the HID one. This project does that: 250 Hz, 4 ms |
| Bluetooth Classic | ❌ | macOS can't discover or pair with it |
| Bluetooth LE | ✅ | Works with a custom BLE client. 33 Hz, 30 ms connection interval |

Wired is used automatically when the cable is plugged in, since it is ~7×
faster and the console cannot steal the controller back over Bluetooth
Classic while it is wired. See [docs/PROTOCOL.md](docs/PROTOCOL.md).

## 📋 Requirements

- macOS Ventura (13.0) or later
- Python 3.9+
- Nintendo Switch 2 Pro Controller
- For wired mode from source: `brew install libusb` (the bundled `.app`
  ships its own copy, so the DMG needs nothing extra)

## 🔧 Run from source

```bash
git clone https://github.com/mlstr0m/switch2bridge-macos.git
cd switch2bridge-macos

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python Switch2Bridge.py
```

macOS will ask for **Bluetooth** permission the first time you click **Connect Controller** (not at launch). That is the only permission the default configuration needs.

⚠️ **When running from source, the Bluetooth permission belongs to _Terminal_ (or your Python interpreter), not to the app.** If no prompt ever appears, add/enable Terminal manually in `System Settings → Privacy & Security → Bluetooth`, then relaunch. The app detects a denied permission and offers to open the right settings pane.

## 📦 Build a standalone .app + DMG

```bash
chmod +x build_dmg.sh
./build_dmg.sh
```

The installer lands at `dist/Switch2Bridge-Installer.dmg`. Manual build:

```bash
python setup_app.py py2app
# → dist/Switch2 Bridge.app
```

## 🎯 Usage

1. Launch the app — a 🎮 appears in the menu bar
2. **Plugged in?** It connects by itself within a second
   **Wireless?** Click **Connect Controller**, then hold the pair button on the
   back until the LEDs sweep
3. Wait for 🟢 (connected). The DSU server is already listening on `127.0.0.1:26760`
4. Point your emulator at it:

| Emulator | Where |
|----------|-------|
| **Dolphin** | Options → Controller Settings → *Alternate Input Sources* → enable *DSU Client*, add `127.0.0.1:26760` |
| **Cemu** | Input settings → add a *DSUController* with the same address |
| **Ryujinx** | Settings → Input → add controller; enable *Motion* → *Use CemuHook compatible motion* for gyro |
| **citra / others** | Any client that speaks cemuhook works |

The menubar shows how many DSU clients are connected, so you can tell at a glance whether the emulator actually attached.

### DSU button layout

Mapping is positional, matching a Switch Pro Controller against the DSU (DualShock-shaped) button set:

| Switch | DSU | | Switch | DSU |
|--------|-----|-|--------|-----|
| A | Circle | | − | Share |
| B | Cross | | + | Options |
| X | Triangle | | Home | PS |
| Y | Square | | Capture | Touch |
| L / R | L1 / R1 | | LS / RS | L3 / R3 |
| ZL / ZR | L2 / R2 | | D-Pad | D-Pad |

DSU has exactly 16 button slots and no room for the Switch 2's **GL**, **GR** and **C** buttons. Fold them onto a DSU button if you want them:

```json
"dsu": { "aliases": { "GL": "L", "GR": "R", "C": "HOME" } }
```

### Stick tuning

```json
"sticks": {
  "deadzone": 0.08,
  "saturation": 0.95,
  "calibration": { "auto_center": true, "half_range": 1500 }
}
```

- **deadzone** — radial, applied to stick magnitude (not per axis, so diagonals stay smooth)
- **saturation** — deflection treated as "fully pushed"; everything beyond clamps to 1.0
- **auto_center** — learns the resting position over the first 60 reports of each connection, and refuses to calibrate if a stick was being moved. **Recalibrate sticks** in the menubar re-runs it
- **half_range** — raw counts from centre to full deflection

## ⌨️ Legacy keyboard bridge (optional, off by default)

The original keyboard bridge is still available for emulators that read only the keyboard. It costs an **Accessibility** grant and reduces the analog sticks to 8 thresholded directions, so it is disabled unless you turn it on from the menubar (**Keyboard bridge**) or in `mappings.json`:

```json
"keyboard": { "enabled": true }
```

Default mapping:

| Button | Key | | Button | Key |
|--------|-----|-|--------|-----|
| A | Z | | L | Q |
| B | X | | R | E |
| X | C | | ZL | 1 |
| Y | V | | ZR | 3 |
| + | P | | LS (click) | F |
| − | M | | RS (click) | G |
| Home | H | | GL (grip) | 9 |
| Capture | O | | GR (grip) | 0 |

D-Pad is mapped to the arrow keys, left stick to WASD, right stick to IJKL.

Each value is either a single character (`"a"`, `"5"`, `"."`), `null` to leave a button unmapped, or a named key in angle brackets: `<up>`, `<down>`, `<left>`, `<right>`, `<space>`, `<enter>`, `<esc>`, `<tab>`, `<backspace>`, `<delete>`, `<home>`, `<end>`, `<pageup>`, `<pagedown>`, `<shift>`, `<ctrl>`, `<alt>`, `<cmd>`, and `<f1>` … `<f20>`.

Two inputs may share the same key: the key is only released once both are released.

## 🛠️ Configuration

The first launch writes a JSON config to:

```
~/Library/Application Support/Switch2Bridge/mappings.json
```

Edit it, then **Reload mappings** from the menubar. Invalid JSON falls back to defaults and the menubar surfaces the parse error; typos in button names or stick directions are reported via a notification rather than silently ignored.

Upgrading from an older version migrates the file in place, adding only the missing blocks — your existing edits are preserved. Note that **the migration leaves the keyboard bridge off**; re-enable it from the menubar if you need it.

## 🔬 How It Works

```
┌─────────────────┐    BLE     ┌──────────────────┐    DSU/UDP    ┌─────────────┐
│  Switch 2 Pro   │ ─────────▶ │  ControllerState │ ────────────▶ │  Emulator   │
│   Controller    │  (bleak)   │   (decoded once) │  (analog)     │             │
└─────────────────┘            └──────────────────┘               └─────────────┘
                                        │
                                        │  optional, opt-in
                                        └──────────────▶  keyboard (pynput)
```

The BLE report is decoded exactly once into a neutral `ControllerState`, which every output backend renders its own way. Adding a backend does not touch the parser.

### BLE characteristics

The controller exposes two services and 16 characteristics. Run
`tools/gatt_explore.py` for the full tree.

| UUID | Properties | Purpose |
|------|-----------|---------|
| `7492866c-…f9` | read, notify | Input reports — the only channel the bridge uses |
| `7492866c-…f8` | read, notify | A **second notification channel**, contents unknown |
| `ab7de9be-…fd2`, `…fde` | read, notify | Unknown notification channels |
| `c765a961-…836a` | notify | Unknown; carries a different descriptor class |
| `506d9f7d-…57e0` | notify | Unknown; same class as above |
| `d3bd69d2-…2a80` | notify | Unknown; same class as above |
| `00c5af5d-…bd282` | **write** (with response) | Only ACK'd write endpoint — likely the command channel |
| `cc483f51-…2b05`/`…2b06` | write-without-response | Unknown write endpoints |
| `3dacbc7e-…b379`/`…b380` | write-without-response | Unknown write endpoints |
| `649d4ac9-…f005`, `4147423d-…f98d`, `ab7de9be-…fdf` | write-without-response | Unknown write endpoints |

> Earlier versions of this README listed `7492866c-…f8` as the output
> characteristic for LED and rumble. **That is wrong** — it is notify-only
> and cannot be written to, which is why "output doesn't respond". The real
> write endpoints are the eight listed above, none of which the bridge
> currently uses.

### Input report layout

Reports are **112 bytes**, of which only the first 12 carry anything (verified over a 2,455-packet capture):

| Byte | Contents |
|------|----------|
| 0 | Free-running counter |
| 1 | Constant `0x1f` |
| 2 | `B` `A` `Y` `X` `R` `ZR` `+` `RS` (bits 0x01…0x80) |
| 3 | `DDOWN` `DRIGHT` `DLEFT` `DUP` `L` `ZL` `−` `LS` |
| 4 | `HOME` 0x01, `CAPT` 0x02, `GR` 0x04, `GL` 0x08, `C` 0x10 |
| 5–7 | Left stick, two 12-bit values |
| 8–10 | Right stick, two 12-bit values |
| 11 | Constant `0x30` |
| 12–111 | Always zero in every capture so far |

> Earlier versions had **C and CAPT swapped** (`0x02` was read as C, `0x10` as Capture). Fixed in v1.3.0 and confirmed live.

### Protocol tools

```bash
python3 tools/capture_packets.py     # guided capture → capture.jsonl
python3 tools/analyze_capture.py     # per-byte variance, button bits, stick extents
python3 tools/live_buttons.py        # live view of which bits are set as you press
```

Quit the menubar app first — the controller accepts one BLE connection at a time.

## 🧊 Why not a real HID device?

Making the controller appear as a genuine system-wide gamepad requires creating a virtual HID device, which on macOS is gated behind an Apple-**restricted** entitlement. This was tested directly on macOS 27 with SIP enabled:

| Attempt | Result |
|---------|--------|
| `IOHIDUserDeviceCreate` symbols present in IOKit | ✅ present |
| Create a virtual gamepad, unentitled | ❌ returns `NULL` |
| Same, ad-hoc signed with `com.apple.developer.hid.virtual.device` | ❌ process **SIGKILLed** by AMFI |

Ad-hoc signing the entitlement doesn't merely fail — the kernel kills the process. Obtaining it legitimately needs an Apple Developer Program **organization** account, a per-request approval from Apple, and a notarized build; the DriverKit route (`com.apple.developer.driverkit.family.hid.device`) has the same gate. The only other way through is disabling AMFI/SIP.

DSU is the path that works today, on an unsigned build, with no permissions.

## 🩺 Troubleshooting

- **The controller never appears in System Settings → Bluetooth** — that's **expected**, and not a failure. This bridge is a BLE client: there is no system-level pairing, so macOS will never list the controller. The only place to watch is the app's menubar icon (🔍 → 🟢).
- **"Controller not found"** — make sure the controller is **not paired with a console nearby** (unpair it or put the console to sleep far away). Click **Connect Controller** *first* — the search runs for 30 s — *then* hold the small pair button on the back until the LEDs sweep back and forth.
- **Menubar says 🟢 but the emulator sees nothing** — check the DSU client count in the menubar. If it's 0, the emulator never attached: re-check the host/port in its DSU settings.
- **Sticks drift** — click **Recalibrate sticks** with both sticks released.
- **No Bluetooth prompt ever appeared (run-from-source)** — the permission belongs to Terminal/Python, not the app. Check `System Settings → Privacy & Security → Bluetooth` and enable Terminal, then relaunch.
- **Keyboard bridge does nothing** — it needs Accessibility (`System Settings → Privacy & Security → Accessibility`), and it's off by default. DSU needs neither.
- **The Switch keeps stealing the controller back** — the bridge connects over **BLE**, while the console uses **Bluetooth Classic**; the controller happily holds both. Because the bridge never sends a claim command, the controller stays in pairing mode and answers the console. Until the command protocol is worked out, fully **power off** the console (sleep is not enough) or disable *Wake Console with Controller* in its controller settings.
- **Logs** — written to `~/Library/Logs/Switch2Bridge/bridge.log`. **Capture raw packets** in the menubar dumps the next 300 raw reports there.

## 🚧 Limitations

| Feature | Status | Notes |
|---------|--------|-------|
| Buttons | ✅ Working | All buttons verified against a real capture |
| Analog sticks | ✅ Working | Full 12-bit analog over DSU, calibrated |
| C / GL / GR | ✅ Decoded | No native DSU slot — alias them onto a DSU button |
| Keyboard bridge | ✅ Optional | Off by default; 8-direction sticks only |
| Motion / Gyro | ❌ Not available | Bytes 12–111 of the input report are zero in every capture. Either the IMU needs enabling via a write endpoint, or it streams on one of the six unsubscribed notify channels. Plumbing and configurable offsets exist — see `motion` in `mappings.json` |
| LED control | ❌ Not working | No command has been sent yet — the bridge writes nothing at all |
| Rumble | ❌ Not working | Same cause |
| Battery level | ❌ Not decoded | Not located in the report yet |
| **Exiting pairing mode** | ❌ Not working | The bridge never claims the controller, so it keeps advertising and a nearby Switch will reconnect to it over Bluetooth Classic. See below |
| Native HID | ❌ Not possible | Requires an Apple-restricted entitlement — see above |

## 🤝 Contributing

Contributions welcome! Areas that need work:

The big open problem is the **command protocol**. The bridge has never written a
single byte to the controller, and that one gap plausibly explains the missing
motion data, the dead LEDs, and the fact that the controller never leaves
pairing mode. There are eight unused write endpoints (see the characteristic
table) and six unsubscribed notify channels where a reply would land.

1. **Map the command channel** — `00c5af5d-…bd282` is the only write-with-response
   endpoint, which makes it the best candidate. `tools/listen_all.py` subscribes
   to every notify channel so a response can be spotted
2. **Enable the IMU** — likely the same protocol; may also already be streaming
   on an unsubscribed channel
3. **LED / rumble / exiting pairing mode** — same protocol
4. **Battery level** — locate it in the report
5. **Cross-platform** — Linux (`uinput` gives a real HID device for free) / Windows (ViGEm)

⚠️ When probing write endpoints, be aware that one of them may be a firmware or
configuration endpoint. Prefer short, structured probes over random payloads.

## 📁 Project Structure

```
switch2bridge-macos/
├── Switch2Bridge.py       # Menubar app, BLE client, report decoding
├── controller_state.py    # Neutral ControllerState + stick calibration
├── outputs.py             # KeyboardOutput (legacy backend)
├── dsu_server.py          # DSU (cemuhook) server — the primary backend
├── tools/
│   ├── capture_packets.py # Guided raw-report capture
│   ├── analyze_capture.py # Offline layout analysis
│   └── live_buttons.py    # Live button-bit viewer
├── tests/
│   ├── test_state.py      # Calibration, migration, motion decode
│   ├── test_bridge.py     # Mappings, key dispatch, BLE lifecycle
│   └── test_dsu.py        # DSU protocol over real UDP
├── setup_app.py           # py2app configuration
├── build_dmg.sh           # Automated build script (.app + DMG)
└── requirements.txt
```

## 📜 Credits

- **Aurélien Desert** — reverse engineering & implementation

## 📚 Protocol notes

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for hardware-verified findings on the
BLE and USB protocols, stick calibration, and what is still unsolved.
