"""
Input-report rate meter
=======================

The controller emits one report per BLE connection event (or per USB poll)
and puts a free-running counter in byte 0. This records arrival times and
that counter so a log line can show the rate, the gap, and whether reports
were skipped before they reached us.

It does not pace, drop, or rewrite reports. Callers feed it after they have
already forwarded the report.
"""

import statistics
import threading
from collections import deque


class ReportRateMeter:
    """A short rolling window of report arrivals.

    `forwarded` is whether that report was handed to at least one DSU
    client. The window is a few seconds, which is long enough for a stable
    gap and short enough that plugging in the cable shows up quickly.
    """

    def __init__(self, window_s=5.0):
        self.window_s = window_s
        self._lock = threading.Lock()
        self._samples = deque()  # (timestamp, counter, forwarded)

    def reset(self):
        with self._lock:
            self._samples.clear()

    def observe(self, counter, timestamp, forwarded):
        with self._lock:
            self._samples.append((float(timestamp), int(counter) & 0xFF, bool(forwarded)))
            cutoff = float(timestamp) - self.window_s
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def summary(self):
        """Stats for the current window, or None until two reports have arrived."""
        with self._lock:
            samples = list(self._samples)
        if len(samples) < 2:
            return None

        gaps = []
        skipped = 0
        duplicates = 0
        forwarded = 0
        prev_t, prev_c, prev_f = samples[0]
        if prev_f:
            forwarded += 1
        for timestamp, counter, was_forwarded in samples[1:]:
            gap = timestamp - prev_t
            if gap >= 0:
                gaps.append(gap * 1000.0)
            delta = (counter - prev_c) & 0xFF
            if delta == 0:
                duplicates += 1
            elif delta > 1:
                skipped += delta - 1
            if was_forwarded:
                forwarded += 1
            prev_t, prev_c = timestamp, counter

        span = samples[-1][0] - samples[0][0]
        count = len(samples)
        hz = (count - 1) / span if span > 0 else 0.0
        return {
            "count": count,
            "span_s": span,
            "hz": hz,
            "gap_p50_ms": statistics.median(gaps) if gaps else 0.0,
            "gap_p95_ms": _quantile(gaps, 0.95),
            "gap_max_ms": max(gaps) if gaps else 0.0,
            "skipped": skipped,
            "duplicates": duplicates,
            "forwarded": forwarded,
        }

    def format_line(self, transport):
        """One log line, or None until the window has about a second of data."""
        stats = self.summary()
        if stats is None or stats["count"] < 20 or stats["span_s"] < 1.0:
            return None
        note = describe_interval(stats["gap_p50_ms"], stats["skipped"], stats["count"])
        dsu = f"DSU forwarded {stats['forwarded']}/{stats['count']}"
        if stats["forwarded"] == 0:
            dsu += " (no subscribed client)"
        text = (
            f"report rate [{transport}]: {stats['hz']:.1f} Hz over "
            f"{stats['span_s']:.1f}s, {stats['count']} reports, "
            f"gap p50 {stats['gap_p50_ms']:.1f} ms "
            f"p95 {stats['gap_p95_ms']:.1f} ms "
            f"max {stats['gap_max_ms']:.1f} ms, "
            f"counter skips {stats['skipped']}, "
            f"duplicates {stats['duplicates']}, {dsu}"
        )
        if note:
            text += " — " + note
        return text


def describe_interval(gap_p50_ms, skipped, count):
    """A short reading of the gap and the counter, for the log line."""
    if count < 2:
        return ""
    if skipped == 0:
        if 27.0 <= gap_p50_ms <= 36.0:
            return "one report per ~30 ms connection event; the link is the limit"
        if 13.0 <= gap_p50_ms <= 18.0:
            return "one report per ~15 ms connection event"
        if 6.5 <= gap_p50_ms <= 8.5:
            return "one report per ~7.5 ms connection event (BLE minimum)"
        if 4.5 <= gap_p50_ms <= 5.5:
            return "one report per ~5 ms event (console-class interval)"
        if 3.5 <= gap_p50_ms <= 4.5:
            return "one report per ~4 ms (USB-class rate)"
        return "counter stepped by 1; nothing was skipped in this window"
    if gap_p50_ms >= 20.0:
        return (
            "counter jumped while arrivals stayed slow; reports are produced "
            "faster than they are delivered"
        )
    return "counter jumped; some reports never arrived"


def _quantile(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round(q * (len(ordered) - 1)))
    index = min(len(ordered) - 1, max(0, index))
    return ordered[index]
