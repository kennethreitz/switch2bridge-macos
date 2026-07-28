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

So it is high-entropy but structured (bytes 64, 65, 68, 69 are constant), and it does not track physical motion. Either encrypted, or an encoding I did not work out. If anyone has decoded this, I would be glad to hear it — I was expecting Accel Z at bytes 40–41 per the Handheld Legend motion doc and those bytes are zero in every capture I took.

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

## Still unsolved

* **Motion / gyro** — the extended report payload does not decode as motion.
  Possibly encrypted
* **Exiting pairing mode** — the controller keeps advertising, so a nearby
  console reconnects to it. Wired sidesteps it entirely
* **Rumble** — not attempted here; Switch2Connect has Pro 2 rumble working
* **Battery level** — not located in the report

---

Credit where due: the ATT table, command framing and LED payload documented by darthcloud are what made any of this possible, and TommyWabg's Switch2Connect was the reference for the verified USB init byte sequences. Thanks to @darthcloud and @german77.
