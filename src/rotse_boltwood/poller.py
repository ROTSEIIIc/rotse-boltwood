"""Serial link to the head: a blocking loop in a worker thread, latest report in memory.

The head pushes a D frame every ~2 s once polled, so this isn't a periodic read like the
weather mast: it's one long conversation driven by Session. The loop:

  open port -> write(session.start()) -> repeat { read up to 256 bytes (0.1 s timeout);
  write(session.feed(data)); write(session.tick()) }

Any serial error closes the port, records the error, waits `reconnect_s` and reopens.
The API reads `session.report` (replaced whole, never mutated) without locking.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from .config import Settings
from .session import Session

log = logging.getLogger(__name__)


MAX_READ_GLITCHES = 5  # consecutive "returned no data" errors before we treat it as a disconnect


def is_read_glitch(e: Exception) -> bool:
    """pyserial's SerialException for select() saying readable but read() returning nothing."""
    return type(e).__name__ == "SerialException" and "returned no data" in str(e)


class Port(Protocol):
    def read(self, n: int) -> bytes: ...
    def write(self, data: bytes) -> Any: ...
    def close(self) -> None: ...


def open_serial(settings: Settings) -> Port:
    import serial  # imported here so tests and --sim don't need a port

    port = serial.serial_for_url(
        settings.port,
        baudrate=settings.baud,
        bytesize=8,
        parity="N",
        stopbits=1,
        rtscts=False,
        xonxoff=False,
        timeout=0.1,
    )
    port.reset_input_buffer()
    return port


class Poller:
    def __init__(
        self,
        settings: Settings,
        open_port: Callable[[], Port],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.open_port = open_port
        self.clock = clock
        self.session = Session(
            ack=settings.ack.encode(), repoll_s=settings.repoll_s, clock=clock, history=settings.history
        )
        self.connected = False
        self.port_error: str | None = None
        self.opens = 0
        self.started_ts = clock()
        self.heartbeat_ts: float | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ state for the API

    def report_age_s(self) -> float | None:
        return self.session.report_age_s()

    def fresh(self) -> bool:
        age = self.report_age_s()
        return age is not None and age <= self.settings.max_age_s

    def ok(self) -> bool:
        """Fresh report and the head flags nothing as broken."""
        r = self.session.report
        return self.fresh() and r is not None and r.ok

    def error(self) -> str | None:
        if not self.connected and self.port_error:
            return f"port: {self.port_error}"
        r = self.session.report
        if r is None:
            return "no report yet"
        if not self.fresh():
            return f"stale: last report {self.report_age_s():.0f} s ago (max_age_s {self.settings.max_age_s:g})"
        if r.errors:
            return "; ".join(r.errors)
        return None

    def loop_healthy(self) -> bool:
        hb = self.heartbeat_ts
        return hb is not None and self.clock() - hb < self.settings.reconnect_s + 30

    # ------------------------------------------------------------------ loop

    def run_blocking(self) -> None:
        while not self._stop.is_set():
            self.heartbeat_ts = self.clock()
            try:
                port = self.open_port()
            except Exception as e:  # noqa: BLE001
                self._set_error(e)
                self._stop.wait(self.settings.reconnect_s)
                continue
            self.opens += 1
            self.connected, self.port_error = True, None
            log.info("port %s open", self.settings.port)
            try:
                port.write(self.session.start())
                glitches_in_a_row = 0
                while not self._stop.is_set():
                    self.heartbeat_ts = self.clock()
                    try:
                        data = port.read(256)
                        glitches_in_a_row = 0
                    except Exception as e:
                        # macOS FTDI driver: a line glitch/break shows up as pyserial's
                        # "device reports readiness to read but returned no data". Isolated
                        # ones are harmless; a real unplug repeats immediately.
                        if not is_read_glitch(e) or glitches_in_a_row >= MAX_READ_GLITCHES:
                            raise
                        glitches_in_a_row += 1
                        self.session.counts["read_glitch"] += 1
                        log.debug("port %s: read glitch: %s", self.settings.port, e)
                        time.sleep(0.05)
                        continue
                    out = self.session.feed(data)
                    out += self.session.tick()
                    if out:
                        port.write(out)
            except Exception as e:  # noqa: BLE001 - serial errors, device unplugged, ...
                self._set_error(e)
            finally:
                self.connected = False
                try:
                    port.close()
                except Exception:  # noqa: BLE001
                    pass
            self._stop.wait(self.settings.reconnect_s)

    def _set_error(self, e: Exception) -> None:
        msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        if msg != self.port_error:
            log.warning("port %s: %s", self.settings.port, msg)
        self.connected, self.port_error = False, msg

    async def run(self) -> None:
        try:
            await asyncio.to_thread(self.run_blocking)
        finally:
            self._stop.set()

    def stop(self) -> None:
        self._stop.set()
