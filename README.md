# Switch 2 Pro Controller for macOS

A Python menu bar app that connects a Nintendo Switch 2 Pro Controller, **wired or over Bluetooth LE**, and exposes it to emulators as a **real analog gamepad** through the DSU (Cemuhook) protocol. No driver, no kext, and no extra permissions for the default setup.

[![CI](https://github.com/kennethreitz/switch2bridge-macos/actions/workflows/ci.yml/badge.svg)](https://github.com/kennethreitz/switch2bridge-macos/actions/workflows/ci.yml)
[![macOS](https://img.shields.io/badge/macOS-Ventura%2B-blue?logo=apple)](https://www.apple.com/macos)
[![Python](https://img.shields.io/badge/Python-3.9%2B-green?logo=python)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

This is a fork of [mlstr0m/switch2bridge-macos](https://github.com/mlstr0m/switch2bridge-macos). Upstream built the Bluetooth LE client, the menu bar app, and the original DSU server. This fork adds wired USB mode (a hardware-verified init sequence sent to the vendor-class interface, 250 Hz versus about 33 Hz over BLE, and an automatic upgrade to wired when the cable is plugged in), real analog sticks with factory calibration, auto-connect, a bundled libusb, and [protocol notes](docs/PROTOCOL.md).

**No gyro support yet.**

---

## What is DSU?

DSU (Cemuhook) is a small UDP protocol. This bridge serves controller state on `127.0.0.1:26760`. Emulators and frontends connect as clients: Dolphin, Cemu, Ryujinx, and [RetroVault](https://github.com/kennethreitz/RetroVault).

It is not a system driver. The controller does not appear in System Settings, and games that use Apple's GameController framework will not see it. See [Why not a real HID device?](#why-not-a-real-hid-device).

## What this is

- A BLE client that reads controller input over Bluetooth Low Energy
- A wired USB client: 250 Hz once the init sequence is sent
- A DSU server with full 12-bit analog sticks for Dolphin, Cemu, Ryujinx, RetroVault, and any other DSU client
- A reference for the Switch 2 Pro Controller BLE and USB protocols ([docs/PROTOCOL.md](docs/PROTOCOL.md))
- An optional legacy keyboard bridge, for emulators that read only the keyboard

## Features

- **Real analog sticks** over DSU, full 12-bit, with no driver
- **Factory stick calibration** read from the controller's own flash, per unit, including asymmetric travel
- **Radial deadzone and saturation**, configurable, so diagonals stay smooth
- **Full button mapping**: buttons, triggers, D-pad, grip buttons (GL/GR), and the C button
- **Wired mode**: 250 Hz / 4 ms over USB, against about 33 Hz / 30 ms on Bluetooth
- **Auto-upgrade to wired** when the cable is plugged in during a Bluetooth session
- **Auto-connect**: the app watches for a cable or for pairing mode, so there is nothing to click
- **Bundled libusb** in the `.app`, so the DMG does not need Homebrew
- **Auto-reconnect** if the controller sleeps or drops
- **No pairing in System Settings.** macOS does not list the controller
- **DSU needs no Accessibility permission.** Bluetooth permission is only for a wireless connection
- **Legacy keyboard bridge**, opt-in, for keyboard-only emulators
- **Start at Login** in the menu bar (bundled `.app`, macOS 13+)
- **Protocol tools** for capturing and inspecting raw reports

## Why both transports

The Nintendo Switch 2 Pro Controller (vendor `0x057E`, product `0x2069`) does not work with macOS's built-in gamepad support:

| Method | Status | Notes |
|--------|--------|-------|
| USB | Works | Enumerates, then stays silent until an init sequence is sent to its **vendor-class interface** (interface 1), not the HID interface. This app does that: 250 Hz, 4 ms |
| Bluetooth Classic | Not available | The controller advertises BR/EDR as unsupported. It is BLE only |
| Bluetooth LE | Works | Custom BLE client. About 33 Hz, because macOS negotiates a 30 ms connection interval and CoreBluetooth does not let an app change it |

Wired is preferred when the cable is plugged in. It is about 7x the Bluetooth rate, and a nearby console cannot take the controller back while it is wired. See [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Requirements

- macOS Ventura (13.0) or later
- Python 3.9+
- Nintendo Switch 2 Pro Controller
- For wired mode from source: `brew install libusb` (the bundled `.app` ships its own copy, so the DMG needs nothing extra)

## Run from source

```bash
git clone https://github.com/kennethreitz/switch2bridge-macos.git
cd switch2bridge-macos

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python Switch2Bridge.py
```

macOS asks for **Bluetooth** permission the first time a wireless connection needs it, not at launch. A plugged-in controller does not need that permission. That is the only permission the default configuration needs.

When running from source, the Bluetooth permission belongs to **Terminal** (or your Python interpreter), not to a separate app. If no prompt ever appears, enable Terminal in `System Settings → Privacy & Security → Bluetooth`, then relaunch. The app detects a denied permission and offers to open that pane.

## Build a standalone .app and DMG

```bash
chmod +x build_dmg.sh
./build_dmg.sh
```

The installer lands at `dist/Switch2Bridge-Installer.dmg`. Manual build:

```bash
python setup_app.py py2app
# dist/Switch2 Bridge.app
```

`setup_app.py` copies libusb into the bundle so wired mode works on machines without Homebrew.

## Usage

1. Launch the app. A gamepad icon appears in the menu bar and the app waits for a controller.
2. Plug in the USB cable, or hold the pair button on the back until the LEDs sweep. It connects on its own.
3. Wait for the green icon. The DSU server is already listening on `127.0.0.1:26760`.
4. Point your emulator or frontend at it:

| Client | Where |
|--------|-------|
| **Dolphin** | Options → Controller Settings → *Alternate Input Sources* → enable *DSU Client*, add `127.0.0.1:26760` |
| **Cemu** | Input settings → add a *DSUController* with the same address |
| **Ryujinx** | Settings → Input → add a controller pointed at the DSU server. No gyro support yet. |
| **RetroVault** | Connect as a DSU client to `127.0.0.1:26760` ([RetroVault](https://github.com/kennethreitz/RetroVault)) |
| **citra / others** | Any client that speaks Cemuhook |

The menu bar shows how many DSU clients are connected, so you can tell whether the emulator actually attached.

**Disconnect** stops watching for that session. Relaunch the app, or click **Connect Controller**, to start again.

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

DSU has exactly 16 button slots and no room for the Switch 2's **GL**, **GR**, and **C** buttons. Fold them onto a DSU button if you want them:

```json
"dsu": { "aliases": { "GL": "L", "GR": "R", "C": "HOME" } }
```

### Stick tuning

On connect the app reads factory calibration from the controller (left block `0x000130A8`, right block `0x000130E8`). That supplies the real centre and the travel in each direction. If the read fails, it falls back to learning the resting centre from the first quiet reports.

```json
"sticks": {
  "deadzone": 0.08,
  "saturation": 0.95,
  "calibration": { "auto_center": true, "half_range": 1500, "use_factory": true }
}
```

- **deadzone**: radial, applied to stick magnitude, so diagonals stay smooth
- **saturation**: deflection treated as fully pushed; everything beyond clamps to 1.0
- **auto_center**: learns the resting position over the first 60 reports of a connection when factory data is not in use, and refuses to calibrate if a stick was moving
- **half_range**: raw counts from centre to full deflection, used until factory travel is applied
- **use_factory**: read per-unit calibration from the controller. **Recalibrate sticks** re-learns the centre and keeps factory travel

## Legacy keyboard bridge (optional, off by default)

The original keyboard bridge is still available for emulators that read only the keyboard. It needs an **Accessibility** grant and reduces the analog sticks to 8 thresholded directions, so it stays off unless you enable **Keyboard bridge** in the menu bar or set this in `mappings.json`:

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

Each value is a single character (`"a"`, `"5"`, `"."`), `null` to leave a button unmapped, or a named key in angle brackets: `<up>`, `<down>`, `<left>`, `<right>`, `<space>`, `<enter>`, `<esc>`, `<tab>`, `<backspace>`, `<delete>`, `<home>`, `<end>`, `<pageup>`, `<pagedown>`, `<shift>`, `<ctrl>`, `<alt>`, `<cmd>`, and `<f1>` through `<f20>`.

Two inputs may share the same key. The key is released only once both are released.

## Configuration

The first launch writes a JSON config to:

```
~/Library/Application Support/Switch2Bridge/mappings.json
```

Edit it, then choose **Reload mappings** in the menu bar. Invalid JSON falls back to defaults and the menu bar shows the parse error. Typos in button names or stick directions are reported in a notification.

Upgrading from an older version migrates the file in place and adds only the missing blocks. Existing edits are preserved. The migration leaves the keyboard bridge off. Re-enable it from the menu bar if you need it.

## How it works

```
┌─────────────────┐  USB or BLE  ┌──────────────────┐   DSU/UDP    ┌─────────────┐
│  Switch 2 Pro   │ ───────────▶ │  ControllerState │ ───────────▶ │  Emulator   │
│   Controller    │              │  (decoded once)  │   (analog)   │  or frontend│
└─────────────────┘              └──────────────────┘              └─────────────┘
                                          │
                                          │  optional, opt-in
                                          └──────────────▶  keyboard (pynput)
```

USB is read with hidapi after libusb sends the init sequence to vendor interface 1. Bluetooth uses bleak. Either way the report is decoded once into a neutral `ControllerState`, and each output renders it. Adding a backend does not touch the parser.

Wired init, in order, on bulk endpoint `0x02`:

```
03 91 00 0D ...   init (an all-FF host address is accepted)
0C 91 00 02 ...   feature mask
0C 91 00 04 ...   enable features
03 91 00 0A ...   select input report 0x09
```

The byte sequences follow public notes (see Credits). What this project established on hardware is that those commands have to go to the vendor-class interface. The same bytes sent as HID output reports are accepted and do nothing. Details, including the console's own BLE init sequence, are in [docs/PROTOCOL.md](docs/PROTOCOL.md).

### BLE characteristics

The controller exposes two services and 16 characteristics. Run `tools/gatt_explore.py` for the full tree. The bridge uses three of them:

| UUID | Properties | Purpose |
|------|-----------|---------|
| `7492866c-…f9` | read, notify | Input reports |
| `649d4ac9-…f005` | write without response | Command channel: factory calibration reads and player LEDs |
| `c765a961-…836a` | notify | Replies to those commands |
| `7492866c-…f8` | read, notify | Second input channel. The console reads this one. This bridge does not |
| `ab7de9be-…fd2`, `…fde` | read, notify | Unknown notification channels |
| `ab7de9be-…fdf` | write without response | Unknown |
| `506d9f7d-…57e0` | notify | Unknown |
| `d3bd69d2-…2a80` | notify | Unknown |
| `00c5af5d-…bd282` | write with response | Unused by the bridge |
| `cc483f51-…2b05` / `…2b06` | write without response | Unknown |
| `3dacbc7e-…b379` | write without response | Same commands as `649d4ac9-…f005`, with 33 leading zero bytes. Unused here |
| `3dacbc7e-…b380` | write without response | Unknown |
| `4147423d-…f98d` | write without response | Unknown |

`7492866c-…f8` is notify-only. It is not an LED or rumble output.

### Input report layout

The bridge decodes the first 12 bytes below. USB prepends a HID report ID, and that byte is stripped before this layout. Report `0x09` (what wired mode selects) continues past byte 11. Those extra bytes are not used. No gyro support yet.

| Byte | Contents |
|------|----------|
| 0 | Free-running counter |
| 1 | Constant `0x1f` on the basic report, `0x23` when report `0x09` is selected |
| 2 | `B` `A` `Y` `X` `R` `ZR` `+` `RS` (bits 0x01 through 0x80) |
| 3 | `DDOWN` `DRIGHT` `DLEFT` `DUP` `L` `ZL` `−` `LS` |
| 4 | `HOME` 0x01, `CAPT` 0x02, `GR` 0x04, `GL` 0x08, `C` 0x10 |
| 5-7 | Left stick, two 12-bit values |
| 8-10 | Right stick, two 12-bit values |
| 11 | Constant `0x30` |

An earlier layout had **C and CAPT swapped** (`0x02` read as C, `0x10` as Capture). The table above matches a live capture and the BlueRetro button map.

### Protocol tools

```bash
python3 tools/capture_packets.py     # guided capture, writes capture.jsonl
python3 tools/analyze_capture.py     # per-byte variance, button bits, stick extents
python3 tools/live_buttons.py        # which bits are set as you press
python3 tools/read_calibration.py    # factory stick blocks over BLE
```

Quit the menu bar app first. The controller accepts one BLE connection at a time. `tools/` also contains USB and GATT probes and a replay of the console init sequence.

## Why not a real HID device?

Making the controller appear as a system-wide gamepad means creating a virtual HID device. On macOS that needs an Apple-restricted entitlement. This was tested on macOS 27 with SIP enabled:

| Attempt | Result |
|---------|--------|
| `IOHIDUserDeviceCreate` symbols present in IOKit | Present |
| Create a virtual gamepad, unentitled | Returns `NULL` |
| Same, ad-hoc signed with `com.apple.developer.hid.virtual.device` | Process is killed by AMFI |

Ad-hoc signing that entitlement gets the process killed. Obtaining it needs an Apple Developer Program organization account, a per-request approval from Apple, and a notarized build. The DriverKit route (`com.apple.developer.driverkit.family.hid.device`) has the same gate. The other route is disabling AMFI or SIP.

DSU works on an unsigned build, with no permissions.

## Troubleshooting

- **The controller never appears in System Settings → Bluetooth.** That is expected. There is no system-level pairing, so macOS will not list it. Watch the menu bar icon.
- **It stays on "Waiting for controller..."** Plug the cable in, or hold the small pair button on the back until the LEDs sweep. A nearby console that is already using the controller will keep it. Power the console off (sleep is not enough) or move it away. **Connect Controller** is the manual search (30 seconds) for when auto-connect is off.
- **Green icon, but the emulator sees nothing.** Check the DSU client count in the menu bar. Zero means the emulator never attached. Recheck host and port.
- **Sticks drift.** Click **Recalibrate sticks** with both sticks released.
- **No Bluetooth prompt (run from source).** The permission belongs to Terminal or Python. Check `System Settings → Privacy & Security → Bluetooth`, enable Terminal, and relaunch. Wired mode does not need it.
- **Keyboard bridge does nothing.** It needs Accessibility (`System Settings → Privacy & Security → Accessibility`), and it is off by default. DSU needs neither.
- **The Switch keeps taking the controller back.** Over Bluetooth the controller keeps advertising, and the console reconnects over BLE (the same radio, not Bluetooth Classic). Power the console fully off, or disable *Wake Console with Controller*. USB avoids this for as long as the cable is plugged in.
- **Logs.** `~/Library/Logs/Switch2Bridge/bridge.log`. **Capture raw packets** in the menu bar dumps the next 300 raw reports there.

## Limitations

| Feature | Status | Notes |
|---------|--------|-------|
| Buttons | Working | Verified against a real capture |
| Analog sticks | Working | Full 12-bit over DSU, factory calibration when the controller answers |
| C / GL / GR | Decoded | No native DSU slot. Alias them onto a DSU button |
| Wired USB | Working | 250 Hz after the vendor-interface init. Falls back to BLE if the cable cannot be claimed |
| Keyboard bridge | Optional | Off by default. 8-direction sticks only |
| Motion / gyro | No gyro support yet | Report `0x09` carries extra bytes. They do not decode as motion. Replaying the console's BLE init sequence did not enable it |
| Player LEDs | Working | A pattern is sent on connect (player 1 by default, `controller.player_light`) |
| Rumble | Not implemented | |
| Battery level | Not decoded | Not located in the report |
| Leaving pairing mode on BLE | Not solved | The controller keeps advertising, so a nearby Switch can reconnect. USB sidesteps this |
| Native HID | Not possible | Needs an Apple-restricted entitlement. See above |

## Contributing

The command channel used here is `649d4ac9-…f005`, with replies on `c765a961-…836a`. Factory stick reads and player LEDs already go out on it.

Still open, in roughly this order:

1. **Gyro.** No gyro support yet. The extra bytes in report `0x09` do not track motion in the captures so far, and replaying the console init sequence did not turn it on.
2. **Rumble.**
3. **Battery level** in the input report.
4. **Stop advertising after connect**, so a console cannot reclaim the controller over BLE. USB already avoids that.
5. **Other platforms.** Linux `uinput` can be a real HID device. Windows has ViGEm.

When probing write endpoints, one of them may be a firmware or configuration endpoint. Prefer short, structured probes over random payloads.

## Project structure

```
switch2bridge-macos/
├── Switch2Bridge.py        # Menu bar app, connect loop, report decoding
├── usb_transport.py        # Wired mode: libusb init, 250 Hz input
├── controller_commands.py  # SPI reads and player LEDs
├── controller_state.py     # ControllerState and stick calibration
├── dsu_server.py           # DSU (Cemuhook) server
├── outputs.py              # Legacy keyboard backend
├── docs/PROTOCOL.md        # Hardware-verified protocol notes
├── tools/                  # Capture, analysis, and probe scripts
├── tests/                  # Headless tests for state, the bridge, and DSU
├── setup_app.py            # py2app configuration, including bundled libusb
├── build_dmg.sh            # .app and DMG
├── requirements.txt
└── AppIcon.icns
```

## Credits

- **[mlstr0m/switch2bridge-macos](https://github.com/mlstr0m/switch2bridge-macos)** (Aurélien Desert): the Bluetooth LE client, the menu bar app, and the original DSU server
- **[darthcloud/BlueRetro #1249](https://github.com/darthcloud/BlueRetro/issues/1249)** and **[@german77](https://github.com/german77)**: ATT table, command framing, SPI reads, and the stick calibration layout
- **[TommyWabg/Switch2Connect](https://github.com/TommyWabg/Switch2Connect)** (GPL-3.0): the USB init byte sequences, used here as a protocol reference and checked on hardware

## Protocol notes

[docs/PROTOCOL.md](docs/PROTOCOL.md) records what was verified on a real Pro Controller 2: factory calibration addresses, the USB interface map, why Bluetooth stays near 33 Hz, the console's BLE init sequence, and what is still unsolved.
