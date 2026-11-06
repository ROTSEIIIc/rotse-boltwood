"""Simulated head with a pyserial-like interface (read/write/close/reset_input_buffer).

Mimics what SN 04015 did on 2026-11-06 (tests/captures/):
  - unpolled: a lone 0x00 every ~2.1 s
  - first poll after power-up: I, T, K, W, C, then a D every ~2 s while the host keeps
    ACKing/polling, each D followed by 0x00 and a few P frames ~0.2 s apart
  - no poll/ACK for `idle_after_s`: back to lone 0x00 bytes (unverified on the real head;
    it keeps the sim from talking forever to a dead host)

The K, W, C texts are copied from the capture. D values drift slowly so plots move.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable

from . import protocol as p

# From the polled capture, verbatim.
_I = " 04015 00067 0x00 000 0x93 00000"
_T = " 04015 00067 00000 -25.0 -10.0  15.0   30.0    12   100   1   132   220   5 47360 49257  0   1"
_K = " 1 0.939 12412 30.6 22.3 13215 -32768  17360"
_W = "  0.0  0.0     0     0     0 11972 32767"
_C = " 1 -4.000e-07 0.320  0.00 10.1"


class SimHead:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        seed: int = 0,
        report_interval_s: float = 2.0,
        idle_after_s: float = 10.0,
        timeout: float = 0.1,
        sky_c: float = -25.0,
    ) -> None:
        self.clock, self.sleep = clock, sleep
        self.rng = random.Random(seed)
        self.report_interval_s = report_interval_s
        self.idle_after_s = idle_after_s
        self.timeout = timeout
        self.sky_c = sky_c
        self.out = bytearray()
        t = clock()
        self.t0 = t
        self.next_zero = t + 2.1
        self.next_d: float | None = None
        self.last_host = -1e9
        self.fresh_boot = True
        self.written = bytearray()
        self.is_open = True

    # ------------------------------------------------------------------ serial API

    def write(self, data: bytes) -> int:
        self.written += data
        if b"\x01" in data or b"\x02a\n" in data or b"\x02A\n" in data:
            now = self.clock()
            if now - self.last_host > self.idle_after_s:  # waking up
                if self.fresh_boot:
                    for t, text in (("I", _I), ("T", _T), ("K", _K), ("W", _W), ("C", _C)):
                        self.out += p.build_m_frame(t, text)
                    self.fresh_boot = False
                self.next_d = now + 0.3
            self.last_host = now
        return len(data)

    def read(self, n: int = 1) -> bytes:
        deadline = self.clock() + self.timeout
        while True:
            self._advance()
            if self.out or self.clock() >= deadline:
                break
            self.sleep(min(0.02, max(0.0, deadline - self.clock())))
        chunk, self.out = bytes(self.out[:n]), self.out[n:]
        return chunk

    def reset_input_buffer(self) -> None:
        self.out.clear()

    def close(self) -> None:
        self.is_open = False

    # ------------------------------------------------------------------ behaviour

    def _advance(self) -> None:
        now = self.clock()
        polled = now - self.last_host <= self.idle_after_s
        if polled and self.next_d is not None and now >= self.next_d:
            self.out += p.build_m_frame("D", " " + self.d_text(now))
            self.out += b"\x00" + b"\x02P\n" * 5
            self.next_d = now + self.report_interval_s
        if not polled and now >= self.next_zero:
            self.out += b"\x00"
            self.next_zero = now + 2.1

    def d_text(self, now: float) -> str:
        """A D frame with the field layout of the real one, values drifting a little."""
        t = now - self.t0
        sky = self.sky_c + 2.0 * math.sin(t / 600) + self.rng.gauss(0, 0.2)
        amb = 18.0 + 3.0 * math.sin(t / 1800) + self.rng.gauss(0, 0.05)
        rh = max(5, min(99, round(45 + 10 * math.sin(t / 2400))))
        # Magnus dew point, same as the head's to ~0.1 C
        g = math.log(rh / 100) + 17.62 * amb / (243.12 + amb)
        dew = 243.12 * g / (17.62 - g)
        f = (
            f"0 1 1 1 1 0 {sky:6.1f} {amb:5.1f} {self.rng.uniform(1, 8):5.1f} N N {rh:3d} {dew:5.1f} "
            f"{amb + 5:5.1f} 0 -99.9 0 {24.3 + self.rng.gauss(0, 0.05):4.1f} {self.rng.uniform(15, 25):5.1f} "
            f"0 450 417 000 044 0000 0000 0958 1023 0186 0190 0.0 066 11972 1 0003"
        )
        return f
