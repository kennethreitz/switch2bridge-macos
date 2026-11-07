"""Headless tests for the input-report rate meter."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from report_rate import ReportRateMeter, describe_interval

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def feed(meter, start, step, count, counter0=0, forwarded=False, counter_step=1):
    for i in range(count):
        meter.observe(counter0 + i * counter_step, start + i * step, forwarded)


print("== empty ==")
meter = ReportRateMeter()
check("no summary until two reports", meter.summary() is None)
check("no log line until two reports", meter.format_line("ble") is None)

print("== 30 ms ble ==")
meter = ReportRateMeter()
feed(meter, 0.0, 0.030, 40, forwarded=True)
stats = meter.summary()
check("33 Hz class", 32.0 <= stats["hz"] <= 34.5, stats["hz"])
check("p50 gap is 30 ms", abs(stats["gap_p50_ms"] - 30.0) < 0.01, stats["gap_p50_ms"])
check("no skips", stats["skipped"] == 0, stats["skipped"])
check("no duplicates", stats["duplicates"] == 0)
check("every report forwarded", stats["forwarded"] == stats["count"])
line = meter.format_line("ble")
check("log names the link", line and "link is the limit" in line, line)
check("log names transport", line and "[ble]" in line, line)
check("describe 30 ms", "30 ms" in describe_interval(30.0, 0, 40))

print("== 4 ms usb ==")
meter = ReportRateMeter()
feed(meter, 10.0, 0.004, 300, forwarded=True)
stats = meter.summary()
check("250 Hz class", 245.0 <= stats["hz"] <= 255.0, stats["hz"])
check("p50 gap is 4 ms", abs(stats["gap_p50_ms"] - 4.0) < 0.01, stats["gap_p50_ms"])
check("usb note", "4 ms" in (meter.format_line("usb") or ""), meter.format_line("usb"))

print("== 5 ms and 7.5 ms ==")
check("describe 5 ms", "5 ms" in describe_interval(5.0, 0, 50))
check("describe 7.5 ms", "7.5 ms" in describe_interval(7.5, 0, 50))
check("describe 15 ms", "15 ms" in describe_interval(15.0, 0, 50))

print("== counter ==")
meter = ReportRateMeter()
# 0,1,2 then a jump to 5 (skipped 3 and 4), then a duplicate 5
for i, counter in enumerate((0, 1, 2, 5, 5, 6)):
    meter.observe(counter, i * 0.030, False)
stats = meter.summary()
check("two skipped counters", stats["skipped"] == 2, stats["skipped"])
check("one duplicate", stats["duplicates"] == 1, stats["duplicates"])
check("none forwarded", stats["forwarded"] == 0)
check("jump note",
      "faster than they are delivered" in describe_interval(30.0, stats["skipped"], stats["count"]))

meter = ReportRateMeter()
for i in range(40):
    meter.observe(i * 2, i * 0.030, False)  # one skipped counter per step
line = meter.format_line("ble")
check("slow arrivals with jumps are called out",
      line and "faster than they are delivered" in line, line)
check("no-client note", line and "no subscribed client" in line, line)

print("== wrap ==")
meter = ReportRateMeter()
meter.observe(254, 0.0, True)
meter.observe(255, 0.030, True)
meter.observe(0, 0.060, True)
meter.observe(1, 0.090, False)
stats = meter.summary()
check("wrap is not a skip", stats["skipped"] == 0, stats["skipped"])
check("wrap is not a duplicate", stats["duplicates"] == 0)
check("partial forward count", stats["forwarded"] == 3, stats["forwarded"])

meter = ReportRateMeter()
meter.observe(255, 0.0, False)
meter.observe(1, 0.030, False)  # missed 0
check("wrap can still skip", meter.summary()["skipped"] == 1)

print("== window ==")
meter = ReportRateMeter(window_s=0.2)
feed(meter, 0.0, 0.030, 5)
feed(meter, 5.0, 0.004, 30, counter0=0)
stats = meter.summary()
check("old samples leave the window", stats["span_s"] < 0.2, stats["span_s"])
check("window keeps the fast run", 200.0 <= stats["hz"] <= 300.0, stats["hz"])

print("== reset ==")
meter.reset()
check("reset clears", meter.summary() is None)

print("== short line suppressed ==")
meter = ReportRateMeter()
feed(meter, 0.0, 0.030, 5)
check("five reports is not a log line yet", meter.format_line("ble") is None)

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
    sys.exit(1)
print("ALL RATE METER TESTS PASSED")
