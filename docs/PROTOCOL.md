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

---

Credit where due: the ATT table, command framing and LED payload documented by darthcloud are what made any of this possible, and TommyWabg's Switch2Connect was the reference for the verified USB init byte sequences. Thanks to @darthcloud and @german77.
