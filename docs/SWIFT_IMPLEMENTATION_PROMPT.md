# Implementation prompt: Switch 2 Pro Controller support in a Swift/macOS emulator

Hand this file to an agent working in the emulator repo. Everything needed is
inline — it does not need access to any other project.

---

## Task

Add native Nintendo Switch 2 Pro Controller support to a Swift macOS emulator
that currently takes input from Apple's GameController framework
(`GCController` / `GCExtendedGamepad`).

Implement a **second input source** using **CoreBluetooth** directly, producing
the same internal input state the existing `GCExtendedGamepad` handler produces.

### Hard constraint, read this first

**You cannot make this controller appear as a `GCController`.** There is no
public API to register a virtual controller on macOS. `GCVirtualController` is
iOS/tvOS/Catalyst only and provides on-screen touch controls, which is not
applicable. Creating a real virtual HID device requires the Apple-restricted
entitlement `com.apple.developer.hid.virtual.device`; ad-hoc signing it causes
AMFI to **SIGKILL the process** (verified on macOS 27, SIP enabled).

So: do **not** attempt to synthesise a `GCController`. Add a parallel input
path that writes into the same internal state struct.

macOS also will never list this controller in System Settings → Bluetooth. It
is a BLE peripheral we talk to as a GATT client; there is no system pairing.
That is expected, not a failure.

---

## 1. Discovery

Scan with CoreBluetooth (`CBCentralManager`). Match a peripheral if **either**:

1. Its advertisement manufacturer data uses Nintendo company ID `0x0553` (the
   Bluetooth SIG assigned ID) or `0x057E` (Nintendo's USB vendor ID), **and**
   the payload contains the product ID `0x2069` little-endian, i.e. bytes
   `69 20`.
2. Fallback: the peripheral name contains `"Pro Controller"`. Some macOS BLE
   stacks surface the name without usable manufacturer data.

Related product IDs, if you want to support more later:

| PID | Device |
|-----|--------|
| `0x2069` | Switch 2 Pro Controller |
| `0x2073` | Switch 2 GameCube controller |
| `0x2067` | Right Joy-Con 2 |
| `0x2066` | Left Joy-Con 2 |

The controller only advertises while in pairing mode. The user must hold the
small pair button on the back until the LEDs sweep. Scan for at least **30
seconds** — a short window misses it routinely.

`Info.plist` needs `NSBluetoothAlwaysUsageDescription`.

---

## 2. GATT layout

Primary service: `ab7de9be-89fe-49ad-828f-118f09df7fd0`

| Characteristic UUID | Properties | Use |
|---|---|---|
| `7492866c-ec3e-4619-8258-32755ffcc0f9` | notify | **Input reports — subscribe to this** |
| `649d4ac9-8eb7-4e6c-af44-1ea54fe5f005` | write-without-response | **Command channel** (no padding) |
| `c765a961-d9d8-4d36-a20a-5315b111836a` | notify | **Command responses** — subscribe before sending |
| `3dacbc7e-6955-40b5-8eaf-6f9809e8b379` | write-without-response | Same commands, but requires 33 leading `0x00` bytes. Prefer the one above |
| `7492866c-ec3e-4619-8258-32755ffcc0f8` | notify | Second input report format (63 bytes). Not needed |
| `ab7de9be-89fe-49ad-828f-118f09df7fd2` | notify | Third input report format. Not needed |

Only the first three matter. Subscribe to the response characteristic *before*
writing any command, or you will miss the reply.

---

## 3. Input report format

Reports are **112 bytes**, arriving at roughly **30 Hz**. Ignore anything
shorter than 12 bytes. Bytes 12–111 are zero on current firmware.

| Byte | Contents |
|------|----------|
| 0 | Free-running counter |
| 1 | Constant `0x1F` |
| 2 | Buttons, group 1 |
| 3 | Buttons, group 2 |
| 4 | Buttons, group 3 |
| 5–7 | Left stick, two packed 12-bit values |
| 8–10 | Right stick, two packed 12-bit values |
| 11 | Constant `0x30` |

### Button bits

```
byte 2:  0x01 B     0x02 A      0x04 Y      0x08 X
         0x10 R     0x20 ZR     0x40 Plus   0x80 RightStickClick

byte 3:  0x01 DPadDown  0x02 DPadRight  0x04 DPadLeft  0x08 DPadUp
         0x10 L         0x20 ZL         0x40 Minus     0x80 LeftStickClick

byte 4:  0x01 Home   0x02 Capture   0x04 GripR   0x08 GripL   0x10 C (GameChat)
```

> **Pitfall:** it is easy to get `Capture` and `C` backwards. `0x02` is
> **Capture**, `0x10` is **C**. This was confirmed three independent ways: a
> timed 2,455-packet capture, a live button test, and BlueRetro's own table
> (which labels `0x10` "Chat"). An earlier implementation had them swapped.

`ZL` and `ZR` are **digital** on this controller — there is no analog trigger
travel. Report them as 0.0 or 1.0.

### Stick unpacking

Two 12-bit values packed into three bytes:

```swift
let lx = Int(d[5]) | (Int(d[6] & 0x0F) << 8)
let ly = (Int(d[6] & 0xF0) >> 4) | (Int(d[7]) << 4)
let rx = Int(d[8]) | (Int(d[9] & 0x0F) << 8)
let ry = (Int(d[9] & 0xF0) >> 4) | (Int(d[10]) << 4)
```

Raw range is 0–4095. Increasing Y reads as **up**, matching
`GCExtendedGamepad`'s convention, so no inversion is needed. Verify this on
hardware anyway.

---

## 4. Stick calibration — do not skip this

**The naive normalisation `(raw - 2048) / 2048` is wrong** and will make the
controller feel broken in two ways:

1. Real stick travel is only ~1500–1800 counts, not 2048, so a fully pushed
   stick reads about **0.80–0.86** and never reaches full deflection.
2. The resting centre is not 2048. Measured units sit 13–110 counts off, which
   reads as permanent stick drift.

The controller stores its own per-unit calibration in flash. Read it.

### SPI read command

Write this 16-byte frame to the command characteristic:

```
02 91 00 04 00 08 00 00 <len> 7E 00 00 <addr byte0> <byte1> <byte2> <byte3>
```

* `0x02` report type (SPI), `0x91` request mode, `0x04` command (SPI read)
* `<len>` = bytes to read
* address is **little-endian 32-bit**

The reply arrives on the response characteristic:

```
02 01 00 04 <r> <r> 00 00 <len> 00 00 00 <addr LE32> <data...>
```

> **Validate the reply by checking the echoed address matches what you asked
> for.** Do *not* check the result byte: upstream documentation says `0xF8`
> but real hardware returns `0x78`. The address echo is reliable.

### Calibration addresses

| Address | Contents |
|---------|----------|
| `0x000130A8` | **Factory calibration, LEFT stick** — 9 bytes |
| `0x000130E8` | **Factory calibration, RIGHT stick** — 9 bytes |
| `0x001FC040` | User calibration, left. Usually erased (`0xFF`) |
| `0x001FC060` | User calibration, right. Usually erased (`0xFF`) |
| `0x001FC000` | Motion calibration. Format unknown, usually erased |

Read the two factory blocks. If a block is all `0xFF`, it is erased — fall
back to defaults rather than using the data.

### Calibration block layout

Nine bytes, three groups of three, each group holding two 12-bit values using
the **same packing as the sticks**:

```
bytes 0-2:  centre    (x, y)
bytes 3-5:  +travel   (x, y)   distance from centre toward maximum
bytes 6-8:  -travel   (x, y)   distance from centre toward minimum
```

Worked example from a real controller, left stick:

```
raw:      b3 47 83 76 16 61 2e 66 64
centre:   (1971, 2100)
+travel:  (1654, 1553)
-travel:  (1582, 1606)
```

Sanity check: `1971 - 1582 = 389`, and 389 was exactly the minimum LX value
observed in an independent input capture. Use that style of check to confirm
your decode.

### Applying it

Travel is **asymmetric** — one direction routinely reaches ~100 counts further
than the other. Do not average them into a single half-range:

```swift
func normalize(_ raw: Int, centre: Int, travelPos: Int, travelNeg: Int) -> Float {
    let delta = raw - centre
    let span  = delta >= 0 ? travelPos : travelNeg
    guard span > 0 else { return 0 }
    return max(-1, min(1, Float(delta) / Float(span)))
}
```

**Fallback when calibration is unreadable:** centre `2048`, travel `1500` for
all axes. Use 1500, not 2048 — see the top of this section.

### Shaping

Apply a **radial** deadzone on the vector magnitude, never per axis. A per-axis
deadzone carves a cross-shaped dead region and makes diagonals snap.

```swift
func shape(_ x: Float, _ y: Float, deadzone: Float = 0.08,
           saturation: Float = 0.95) -> (Float, Float) {
    let mag = sqrt(x*x + y*y)
    guard mag > deadzone else { return (0, 0) }
    let span = max(saturation - deadzone, 0.0001)
    let scaled = min((mag - deadzone) / span, 1.0)
    return (x / mag * scaled, y / mag * scaled)
}
```

---

## 5. Player LEDs

Same 16-byte frame shape. One bit per LED, so `0x01` lights player 1 and `0x0F`
lights all four:

```
09 91 00 07 00 08 00 00 <pattern> 00 00 00 00 00 00 00
```

Set this once on connect so the user can see the controller has been claimed.
Confirmed working on hardware.

### Generic command frame

Both commands above are instances of one format:

```
<reportType> 0x91 0x00 <command> 0x00 0x08 0x00 0x00 <8-byte payload>
```

`0x91` marks a request; replies come back with `0x01` in that slot.

---

## 6. Connection lifecycle

Handle these or the experience will be poor:

* **Scan window** — at least 30s; the controller advertises only in pairing mode
* **Configure asynchronously** — read calibration and set the LED *after*
  reporting the connection as live, and bound it with a timeout (~3s). A
  controller that does not answer must not delay or block the connection, and
  must not delay noticing a disconnect
* **Reconnect** — the controller drops when it sleeps or goes out of range.
  Retry for ~60s before giving up
* **Release input on disconnect** — clear all buttons and centre the sticks, or
  the last frame's state sticks forever
* **Bluetooth permission** — CoreBluetooth prompts on first scan. If denied,
  scans silently find nothing; detect via `CBManager.authorization` and tell
  the user which Settings pane to open

---

## 7. Known limitations — do not spend time on these

| Feature | Status |
|---|---|
| **Motion / gyro** | **Not available.** Bytes 12–111 are zero in every capture. The IMU appears to need an enable command nobody has found. The motion calibration block is erased too |
| **Rumble** | Not implemented here. BlueRetro has Pro 2 rumble working — port from there if wanted |
| **Battery level** | Not located in the report |
| **Exiting pairing mode** | **Unsolved by anyone.** The controller keeps advertising, so a nearby Switch console reconnects to it over Bluetooth Classic and steals it. Requires Nintendo's LTK derivation, which is uncracked. Workaround is to power the console fully off. Do not attempt to fix this |

---

## 8. Suggested API

```swift
struct Switch2State {
    var leftStick:  SIMD2<Float> = .zero   // calibrated, shaped, -1...1
    var rightStick: SIMD2<Float> = .zero
    var a, b, x, y: Bool
    var l, r, zl, zr: Bool
    var plus, minus, home, capture: Bool
    var leftStickClick, rightStickClick: Bool
    var gripL, gripR, chatC: Bool          // Switch 2 only
    var dpadUp, dpadDown, dpadLeft, dpadRight: Bool
}

final class Switch2ProController {
    var onConnect: ((String) -> Void)?
    var onDisconnect: (() -> Void)?
    var onState: ((Switch2State) -> Void)?

    func startScanning()
    func disconnect()
    func setPlayerLight(_ pattern: UInt8)
}
```

Map `Switch2State` into whatever the `GCExtendedGamepad` path already produces,
so downstream game code is unchanged. Note the Switch face-button layout: `A` is
the **right** face button and `B` is the **bottom** one, the opposite of Xbox
naming. Map by position, not by letter, unless you want Nintendo-style glyphs.

`gripL`, `gripR` and `chatC` have no equivalent on other controllers — expose
them as extra bindable inputs.

---

## 9. Verification

1. **Buttons** — press each and confirm exactly one flag changes. Pay attention
   to Capture vs C
2. **Stick range** — push each stick to its edge; every axis must reach ±1.00.
   If you top out near 0.85, calibration is not being applied
3. **Centre** — at rest, all axes must read 0.00. Non-zero means calibration
   failed or the deadzone is too small
4. **Diagonals** — a slow circle around the stick edge should keep magnitude
   near 1.0 without snapping
5. **LED** — player 1 should light on connect
6. **Disconnect** — sleep the controller mid-input; all inputs must release

Reference implementation of the decode, calibration and command layers, in
Python: <https://github.com/mlstr0m/switch2bridge-macos>
(`controller_state.py`, `controller_commands.py`, `Switch2Bridge.py`).

Protocol reverse engineering credit: darthcloud and german77,
<https://github.com/darthcloud/BlueRetro/issues/1249>
