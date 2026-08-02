# Switch 2 Pro Controller protocol notes

Findings from building a macOS bridge for the Pro Controller 2, over both
Bluetooth LE (CoreBluetooth) and USB. Everything here is verified against
real hardware — a single Pro 2 unit, firmware version unknown — and the
decode steps were cross-checked against independent input captures rather
than assumed.

These were originally written up for
[BlueRetro issue #1249](https://github.com/darthcloud/BlueRetro/issues/1249),
which is where most of this protocol was reverse engineered. That repository
has since been archived and no longer accepts comments, so they live here
instead.

Prior work this builds on:

* [darthcloud/BlueRetro #1249](https://github.com/darthcloud/BlueRetro/issues/1249)
  — the ATT table, command framing, and player LED payload
* [@german77](https://github.com/german77) — SPI read command and the stick
  calibration block layout
* [TommyWabg/Switch2Connect](https://github.com/TommyWabg/Switch2Connect)
  (GPL-3.0) — verified USB init byte sequences, used as a protocol reference
* [Handheld Legend](https://docs.handheldlegend.com/s/link-zone) — USB
  initialisation and report format documentation
* [ndeadly/switch2_controller_research](https://github.com/ndeadly/switch2_controller_research)
  — `commands.md` and `hid_reports.md`, the root documentation for the command
  set, the feature-flag bits and the report `0x05` layout
* [SDL](https://github.com/libsdl-org/SDL) `src/joystick/hidapi/SDL_hidapi_switch2.c`
  — an upstream, maintained USB implementation: init order, flash addresses,
  IMU offsets and scaling. USB only; its Bluetooth path is a stub

---

## Right stick factory calibration lives at `0x000130E8` on the Pro 2

german77 documented factory stick calibration is at `0x000130A8` "only, both left and right", with `0x000130E8` empty on Joy-Cons. On the Pro Controller 2 **both blocks are populated** — left at `0x000130A8`, right at `0x000130E8`, nine bytes each:

```
0x000130A8: b347837616612e6664   -> centre (1971, 2100)  +travel (1654, 1553)  -travel (1582, 1606)
0x000130E8: 60e884f1f56551f665   -> centre (2144, 2126)  +travel (1521, 1631)  -travel (1617, 1631)
```

Layout is `centre(3) + max(3) + min(3)`, each three bytes packing two 12-bit values exactly like the stick reports, and max/min are travel distances from centre rather than absolute positions.

I validated this against a separate 2455-packet input capture rather than trusting the decode. Predicted vs observed extremes:

| | predicted | observed | delta |
|---|---|---|---|
| LX min | 389 | 389 | 0 |
| LY max | 3653 | 3656 | 3 |
| LY min | 494 | 481 | 13 |
| RX max | 3665 | 3666 | 1 |

The user calibration blocks (`0x1FC040` / `0x1FC060`) and motion calibration (`0x1FC000`) were all `0xFF` (erased) on this unit, so factory data is the only source unless the user has recalibrated.

Worth noting for anyone implementing: the nominal `(raw - 2048) / 2048` normalisation is wrong twice over. Real travel is 1514–1767 counts, so a fully deflected stick reads ~0.80–0.86 and never reaches full scale, and resting centres sit up to 110 counts off 2048, which reads as permanent drift. Travel is also asymmetric — up to ~130 counts difference between directions on the same axis — so a single half-range loses accuracy.

## The SPI reply result byte is not always `0xF8`

The documented ACK is `0xF8`, but this unit replies `0x78` over BLE for every command, including SPI reads and player LEDs:

```
request  02 91 00 04 00 08 00 00 0b 7e 00 00 40 c0 1f 00
reply    02 01 00 04 10 78 00 00 0b 00 00 00 40 c0 1f 00 <data>
                        ^^ ^^
```

Interestingly the *same unit* returns `0xF8` over USB, so it appears to be transport-dependent rather than a firmware revision difference. Validating replies by checking the echoed address matches the request is more robust than matching the result byte.

## Extended input report over BLE, and what is in it

Sending `03 91 01 0A 00 04 00 00 09 00 00 00` (report type `0x03`, command `0x0A`, select input report `0x09`, transport byte `0x01`) on the command characteristic switches the BLE input report from all-zero past byte 12 to carrying a ~42 byte payload at bytes 64–105. Byte 1 flips from `0x1f` to `0x23` when this is active.

The `0x0C` feature commands ACK but do not appear to do anything on their own over BLE — the report selection is what changes the stream.

I could not decode that payload as motion. Testing against a scripted capture (controller flat and untouched vs deliberate pitch / yaw / roll):

- 25% of the payload's bits change between consecutive packets **while the controller is stationary** (stick bytes, as a control, change 6%)
- Shannon entropy 7.30 bits/byte, all 256 values present
- Across rest/pitch/yaw/roll, only byte 105 correlated with rotation

So it is high-entropy but structured (bytes 64, 65, 68, 69 are constant), and it does not track physical motion. Either encrypted, or an encoding I did not work out. I was expecting Accel Z at bytes 40–41 per the Handheld Legend motion doc and those bytes are zero in every capture I took.

**This turned out to be the wrong report.** Motion is carried plainly in input
report `0x05`, not in the `0x09` tail — see [Motion is solved](#motion-is-solved-it-is-in-input-report-0x05-not-0x09)
below. What the `0x09` payload actually encodes is still unknown, but nothing
here depends on decoding it any more.

## Wired: commands go to interface 1, not the HID interface

This may save someone time. Over USB the controller enumerates as:

```
interface 0    class 0x03 HID      ep 0x81 IN, 0x01 OUT
interface 1    class 0xff vendor   ep 0x02 OUT, 0x82 IN
interface 2-4  class 0x01 audio    isochronous, 192 byte packets
```

Sending the init sequence as HID output reports on interface 0 (report ID `0x02`, the only output report in the descriptor) **succeeds and does nothing** — hidapi returns a byte count and the controller stays silent. The commands have to go to the vendor-class interface 1, bulk endpoint `0x02`, with replies on `0x82`. On macOS, IOKit claims interface 0 for HID but leaves interface 1 free, so libusb can claim it while input is read with hidapi.

With that, this four-command sequence is enough to get it streaming — no LTK, no pairing, no `0x15` exchange:

```
03 91 00 0D 00 08 00 00 01 00 FF FF FF FF FF FF   init (host MAC all-FF is accepted)
0C 91 00 02 00 04 00 00 27 00 00 00               feature mask
0C 91 00 04 00 04 00 00 27 00 00 00               enable features
03 91 00 0A 00 04 00 00 09 00 00 00               select input report 0x09
```

All four ACK, and the controller then streams report `0x09` at **250 Hz** (4 ms), against ~33 Hz over BLE where CoreBluetooth negotiates a 30 ms connection interval that an app cannot change.

The report body is byte-identical to the BLE one — USB just prepends the HID report ID — so one parser handles both.

## HID report descriptor

For reference, since it confirms the field layout:

```
Report ID 0x05  Input   63 bytes
Report ID 0x09  Input   63 bytes   <- 2 vendor bytes, 21 buttons (21 bits + 3 pad),
                                       4 axes x 12 bits, then 52 vendor bytes
Report ID 0x02  Output  63 bytes
```

The 21 buttons match the byte 2/3/4 bitfield documented by darthcloud exactly, and the 4x12-bit axes confirm the stick packing.

## There is no Bluetooth Classic mode

The controller advertises `Flags = 0x06`:

```
bit 1  LE General Discoverable
bit 2  BR/EDR NOT supported          <- set
bit 3  Simultaneous LE + BR/EDR      <- clear
bit 4  Simultaneous LE + BR/EDR      <- clear
```

Read from the advertiser carrying Nintendo company ID `0x0553` with
`VID 057e / PID 2069` in its manufacturer data, so it is definitely the
Pro 2 and not a neighbour in the open-air capture.

**It is a BLE-only device.** No command can put it into a standard Bluetooth
HID mode, because the radio does not have one.

Two consequences:

* macOS's built-in "Switch Pro Controller" support is for the *Switch 1*
  Pro, a Bluetooth Classic HID device. That path does not apply here.
* Even over BLE the controller uses proprietary GATT services
  (`ab7de9be-...`, `00c5af5d-...`) rather than the standard HID-over-GATT
  profile (`0x1812`), so there is no generic path either. This is why it
  never appears in System Settings.

The Switch 2 console also connects over **BLE**, not Bluetooth Classic — the
sniffer captures show `CONNECT_IND` and ATT traffic. A console reclaiming
the controller is ordinary BLE reconnection to an advertising peripheral,
not a second parallel radio link.

## Connection interval: 15 ms on the console, 30 ms on macOS

From an nRF sniffer capture of a real Switch 2 pairing with a Pro 2, the
`CONNECT_IND` specifies:

```
interval = 15.00 ms   latency = 0   timeout = 2000 ms
```

Confirmed independently by packet timing in the same capture — the dominant
inter-event gap is 14.8 ms. No `LL_CONNECTION_UPDATE_IND` appears anywhere,
so it never changes after connecting.

macOS negotiates **30 ms** instead: ~33 Hz against the console's ~66 Hz. The
controller sends exactly one report per connection event on both transports
— the byte-0 counter increments by 1 every time, over 2454 BLE and 499 USB
transitions — so the report rate *is* the connection interval. The
controller adapts to the transport rather than dropping packets.

**This cannot be changed from the controller side.** The full console init
sequence below was replayed over BLE, every command acknowledged, and the
rate stayed at exactly 33 Hz / 30 ms. The interval is chosen by the central
before any command is sent, and CoreBluetooth exposes no API to influence
it. 15 ms is Apple's documented minimum for peripheral-requested
parameters, so the controller would likely be granted it — but it publishes
no Peripheral Preferred Connection Parameters characteristic and never sends
a connection parameter update request.

Use USB if latency matters: 250 Hz / 4 ms.

## The console's own BLE init sequence

Extracted from darthcloud's decrypted OTA capture
(`sw2_pro2_reconn_sc2_rumble_crackle.pcap`). This is what a real Switch 2
sends, in order, to ATT handle `0x0016`. The transport byte is `01`
throughout, and the console reads input from handle `0x000e`
(`7492866c-...f8`) rather than `...f9`.

```
07 91 01 01 00 00 00 00                            status
02 91 01 04 00 08 00 00 40 7e 00 00 00 30 01 00    SPI read 0x00013000, 0x40 bytes
10 91 01 01 00 00 00 00                            unknown; replies 02 01 04 02
16 91 01 01 00 00 00 00                            unknown
0a 91 01 02 00 04 00 00 03 00 00 00                report config
09 91 01 07 00 08 00 00 01 ...                     player LED
0c 91 01 02 00 04 00 00 27 00 00 00                feature mask
02 91 01 04 ...  SPI 0x00013080 / 0x000130c0 / 0x001fc040 / 0x00013040 / 0x00013100
11 91 01 03 00 00 00 00                            unknown; replies 01 20 03 00
02 91 01 04 ...  SPI 0x00013060
0a 91 01 08 00 14 00 00 01 ff ff ff ff ff ff ff    vibration config
11 91 01 01 00 00 00 00                            unknown; replies 01 00 00 00
0c 91 01 04 00 04 00 00 27 00 00 00                enable features
```

Replaying this verbatim did **not** make `...f8` or `...fd2` start streaming
for us, and did not enable motion. `tools/replay_console_init.py` reproduces
the experiment.

## There is no USB compatibility mode, and the wired command space is small

The Pro 2 does **not** have a mode that makes it enumerate as a Switch 1 Pro
Controller (`057e:2009`). If you see `057e:2009` alongside a Pro 2, it is either
a genuine Switch 1 pad or Steam's own virtual gamepad — Steam creates one via
`IOHIDUserDevice` and records it in
`~/Library/Application Support/Steam/config/virtualgamepadinfo.txt` as
`name=Nintendo Switch Pro Controller, VID=0x057e, PID=0x2009, type=switchpro`.
It will sit on the same USB `LocationID` if it replaced the Pro 2 in the same
port, which makes it easy to mistake for a mode change.

Evidence:

* one USB configuration only (`bNumConfigurations = 1`), `bcdDevice = 0x0201`,
  so there is no alternate config to select
* sweeping command ids `0x01`–`0x1F` on interface 1 never changed the pid

### Command sweep results

Sent as `03 91 00 <cmd> 00 00 00 00` — report type `0x03`, **zero-length
payload** — to interface 1 endpoint `0x02`, reading `0x82`, re-checking the pid
after every command. Empty payloads were deliberate: an SPI write needs an
address and data, so a malformed empty frame should be rejected rather than
executed.

| Command | Behaviour |
|---|---|
| `0x01` | **Device reset.** Drops off the bus and re-enumerates, same pid. Note the console sends `0x01` as report type `0x11`, not `0x03` |
| `0x08`, `0x09` | Reply `03 01 00 <cmd> 00 f8 00 00` — accepted |
| `0x0F` | Reply `03 01 00 0f 00 f8 00 00 05 00` — accepted, returns a 2-byte payload `05 00` |
| `0x03`, `0x05`, `0x06`, `0x0B`, `0x0C`, `0x0E` | No reply at all |
| `0x10`–`0x1F` | Uniform `03 04 00 <cmd> 00 f8 00 00` — almost certainly "unknown command" |

### Reply framing

Replies mirror the command header, with two fields repurposed:

```
byte 0   report type, echoed
byte 1   0x01 on a real reply  (= MODE_REPLY in controller_commands)
         0x04 on the 0x10-0x1F block, i.e. a different class — likely unknown-command
byte 3   command id, echoed
byte 5   result byte
```

Worth noting against the BLE observation above: **over USB this unit replies
`0xF8`**, the documented ACK, not the `0x78` it gives over BLE. So the `0x78`
is a BLE-transport quirk rather than a property of the unit.

Unexplored: `0x08`, `0x09` and `0x0F` are accepted but their payload formats are
unknown, and only report type `0x03` was swept — `0x02`, `0x0A`, `0x0C` and
`0x11` may expose different commands at the same ids.

**The `0x10`–`0x1F` "unknown command" block was an artefact of that last
limitation.** Those ids are not commands under report type `0x03`; they are
*report types* in their own right. `10 91 00 01 00 00 00 00` is a firmware
version query and `11 91 00 01 00 00 00 00` is part of the console's own init,
and both are accepted. Sweeping the command axis while holding the report type
fixed at `0x03` could only ever find the `0x03` commands.

## Motion is solved: it is in input report `0x05`, not `0x09`

The extended `0x09` payload at bytes 64–105 was the wrong place to look. It is
not encrypted — it is simply not the motion report. The controller can stream a
**different input report, `0x05`**, which carries plaintext accelerometer and
gyroscope data at fixed offsets.

Select it with the same command that selects `0x09`, with payload `0x05`:

```
03 91 00 0A 00 04 00 00 05 00 00 00     select input report 0x05
```

Verified here over USB: 64-byte reports at 250 Hz, decoding to |a| = 9.97 m/s²
resting on a desk, and a peak of 3.45 rad/s (198 °/s) while the controller was
rotated by hand against 0.098 rad/s once it was put back down.

### Report `0x05` field map

Offsets include the leading HID report-id byte, so subtract one for the
Bluetooth body. Verified field by field on hardware:

```
[0]      report id 0x05
[1:5]    counter, u32 LE (increments every packet)
[5:9]    buttons, u32 LE
[11:14]  left stick,  two 12-bit values, same packing as report 0x09
[14:17]  right stick
[32:34]  battery, millivolts u16 LE      <- read 3744 mV on a part-charged unit
[0x2b]   sensor timestamp, u32 LE
[0x31]   accel X, i16 LE   [0x33] accel Z (negated)   [0x35] accel Y
[0x37]   gyro X,  i16 LE   [0x39] gyro Y (negated)    [0x3b] gyro Z
[60][61] triggers (analog on the GameCube pad; digital bits on the Pro)
```

Scaling, from SDL: accel is ±8 g, so `raw * 9.80665 * 8 / 32767` gives m/s².
Gyro is `raw * 34.8 / 32767` rad/s minus the stored bias.

**Liveness is the sensor timestamp, not the values.** A disabled IMU keeps its
last sample latched in the report, so accel still reads like plausible gravity
while nothing is updating. `[0x2b]` advancing is the only reliable signal — this
cost some confusion before it was noticed.

### Turning the IMU on

Feature bits, per ndeadly's `commands.md`, carried by report type `0x0C`:

```
0x01 buttons   0x02 analog   0x04 IMU   0x08 unknown (set by SDL and BlueRetro)
0x10 mouse (Joy-Con)         0x20 rumble             0x80 magnetometer
```

So the `0x27` this repo already sends is `buttons | analog | IMU | rumble` — the
IMU bit was being set all along. `0x0C/0x02` declares the allowed mask,
`0x0C/0x04` enables, `0x0C/0x05` disables.

On a cold plug-in the init sequence brings the IMU straight up, confirmed on a
real unplug/replug. Once on it **stays on across re-inits** — the controller
remembers, so "it worked after my change" needs a power cycle to mean anything.
One session did leave the IMU dark until an explicit `0x0C/0x05` → `0x0C/0x04`
toggle; that state could not be recreated afterwards across cold boots, warm
re-inits, `0x09`→`0x05` format switches, or with and without the rejected
commands below, so the toggle is worth keeping as a recovery but is not
required.

### IMU and serial calibration blocks

```
0x00013000   serial number, ASCII at offset 2
0x00013040   gyro bias,  three float32 LE at offsets 4, 8, 12
0x00013100   accel bias, three float32 LE at offsets 12, 16, 20
```

On this unit the accel bias block reads `(0.0645, -0.1511, 9.9194)` — that third
value being gravity in m/s² is what confirms the layout. These are real
float32s, unlike the packed 12-bit stick blocks. Note this is *not* the
`0x001FC000` motion block, which was erased here.

## A second flash-read command, and a cross-check of the calibration

SDL reads flash with `0x02`/**`0x01`**, which takes no length and always returns
`0x40` bytes, data at offset `0x10` of the reply:

```
02 91 00 01 00 08 00 00 00 00 00 00 <addr u32 LE>
```

This repo uses `0x02`/`0x04` with an explicit length instead. Both work. Running
them against each other is a useful check on the calibration decode, and they
agree byte for byte:

```
left   SDL 0x13080+0x28 : b347837616612e6664
       ours 0x000130A8  : b347837616612e6664
right  SDL 0x130C0+0x28 : 60e884f1f56551f665
       ours 0x000130E8  : 60e884f1f56551f665
```

Which also explains the address discrepancy in the first section: SDL reads a
`0x40`-byte block at `0x13080` / `0x130C0` and takes the 9-byte record from
offset `0x28`, landing on exactly the `0x130A8` / `0x130E8` documented above.

User calibration lives at `0x001FC040` and `0x001FC080` — note `0x80`, not the
`0x60` guessed here earlier — and is only valid when the block starts `B2 A1`,
with the record at offset 2. Both were erased on this unit.

## The full USB init, and two commands this unit rejects

SDL's sequence, which is longer than the four commands above and ends with
"start output" rather than beginning with it:

```
07 91 00 01 00 00 00 00                            unknown
0C 91 00 02 00 04 00 00 27 00 00 00                feature mask
11 91 00 01 00 00 00 00                            unknown
0A 91 00 08 00 14 00 00 01 ff ff ff ff ff ff ff
                           ff 35 00 46 00 00 ...   vibration config
0C 91 00 04 00 04 00 00 27 00 00 00                enable features
01 91 00 0C 00 00 00 00                            unknown
01 91 00 01 00 00 00 00                            enable rumble      <- rejected
08 91 00 02 00 04 00 00 01 00 00 00                grip buttons       <- rejected
03 91 00 0A 00 04 00 00 05 00 00 00                select report 0x05
03 91 00 0D 00 08 00 00 01 00 ff ff ff ff ff ff    start output
```

Every command replies with `0x01` in byte 1 (accepted) except `01/01` and
`08/02`, which reply with `0x04` — the same class the `0x10`–`0x1F` sweep
returned. `08/02` is documented as enabling the grip buttons on the *charging
grip*, so a Pro 2 refusing it is unsurprising. Dropping both makes no difference
to input, motion or the report rate.

## Still unsolved

* **Exiting pairing mode** — the controller keeps advertising, so a nearby
  console reconnects to it. Wired sidesteps it entirely
* **Rumble** — not attempted here, though the encoding is now documented:
  five-byte HD frames of 10-bit frequency/amplitude pairs, three frames per
  16-byte block, left at offset 1 and right at 17 of output report `0x02`
* **Motion over Bluetooth** — report `0x05` is verified over USB here. ndeadly's
  captures name handle `0x000A` for it, against `0x000E` for `0x09`, so the BLE
  path likely needs a different characteristic as well as the format command

---

Credit where due: the ATT table, command framing and LED payload documented by darthcloud are what made any of this possible, and TommyWabg's Switch2Connect was the reference for the verified USB init byte sequences. Thanks to @darthcloud and @german77.
