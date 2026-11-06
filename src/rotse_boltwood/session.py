"""Host side of the head conversation, as a pure state machine (no I/O, injectable clock).

    s = Session()
    port.write(s.start())
    loop:
        port.write(s.feed(port.read(256)))   # answers frames, keeps the latest report
        port.write(s.tick())                 # re-polls after `repoll_s` without a message

Behaviour (confirmed on SN 04015, see protocol.py): every M frame gets ACK + poll (NACK +
poll on a bad CRC), every P frame gets ACK + poll, and if no complete message arrives for
`repoll_s` we send REQUEST_POLL again. An unpolled head only sends lone 0x00 bytes, which
never complete a message, so the re-poll timer covers both startup and recovery.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass

from . import protocol as p


@dataclass(frozen=True)
class Event:
    """One message seen by the session, for monitors/loggers. kind 'junk' = unparseable."""

    ts: float
    frame: p.Frame
    reply: bytes
    error: str | None = None


class Session:
    def __init__(
        self,
        ack: bytes = p.HOST_ACK,
        repoll_s: float = 2.0,
        clock: Callable[[], float] = time.time,
        on_event: Callable[[Event], None] | None = None,
        history: int = 200,
    ) -> None:
        self.ack = ack
        self.repoll_s = repoll_s
        self.clock = clock
        self.on_event = on_event
        self.buf = b""
        self.counts: Counter[str] = Counter()
        self.report: p.SensorReport | None = None
        self.report_ts: float | None = None
        self.thresholds: p.Thresholds | None = None
        self.reset: p.ResetInfo | None = None
        self.last_message_ts: float | None = None
        self.last_poll_ts: float | None = None
        self.last_error: str | None = None
        self.bytes_rx = 0
        # for inspection: last `history` messages, and the latest M frame of each type
        self.recent: deque[Event] = deque(maxlen=history)
        self.latest: dict[str, Event] = {}

    # ------------------------------------------------------------------ public

    def start(self) -> bytes:
        """Call after opening the port: drops partial input and asks for data."""
        self.buf = b""
        return self._poll()

    def feed(self, data: bytes) -> bytes:
        """Process received bytes; returns what to send back (possibly b'')."""
        if not data:
            return b""
        self.bytes_rx += len(data)
        msgs, self.buf = p.split_frames(self.buf + data)
        if len(self.buf) > 4096:  # no newline for a long time: garbage, don't grow forever
            self.counts["overflow"] += 1
            self.buf = b""
        return b"".join(self._handle(m) for m in msgs)

    def tick(self) -> bytes:
        """Re-poll if nothing complete arrived (and we haven't polled) for repoll_s."""
        now = self.clock()
        last = max(self.last_message_ts or 0.0, self.last_poll_ts or 0.0)
        if now - last >= self.repoll_s:
            self.counts["repoll"] += 1
            return self._poll()
        return b""

    def message_age_s(self) -> float | None:
        return None if self.last_message_ts is None else max(0.0, self.clock() - self.last_message_ts)

    def report_age_s(self) -> float | None:
        return None if self.report_ts is None else max(0.0, self.clock() - self.report_ts)

    # ------------------------------------------------------------------ internals

    def _poll(self) -> bytes:
        self.last_poll_ts = self.clock()
        return p.poll_request()

    def _emit(self, frame: p.Frame, reply: bytes, error: str | None = None) -> bytes:
        ev = Event(self.clock(), frame, reply, error)
        self.recent.append(ev)
        if frame.kind == "M" and frame.type:
            self.latest[frame.type] = ev
        if self.on_event is not None:
            self.on_event(ev)
        return reply

    def _handle(self, raw: bytes) -> bytes:
        now = self.clock()
        try:
            f = p.parse_frame(raw)
        except p.ProtocolError as e:
            self.counts["junk"] += 1
            self.last_error = str(e)
            return self._emit(p.Frame(kind="junk", raw=raw), b"", str(e))
        self.last_message_ts = now

        if f.kind == "empty":
            self.counts["empty"] += 1
            return self._emit(f, self._poll())
        if f.kind == "P":
            self.counts["P"] += 1
            self.last_poll_ts = now
            return self._emit(f, p.ack_and_poll(self.ack))
        if f.kind in ("A", "N"):
            self.counts[f.kind] += 1
            return self._emit(f, b"")

        # M frame
        self.counts[f.type or "?"] += 1
        self.last_poll_ts = now
        if f.crc_ok is False:
            self.counts["crc_bad"] += 1
            self.last_error = f"bad CRC on {f.type} frame"
            return self._emit(f, p.nack_and_poll(), self.last_error)
        error = None
        try:
            if f.type == "D":
                self.report, self.report_ts = p.parse_sensor_report(f.text), now
            elif f.type == "T":
                self.thresholds = p.parse_thresholds(f.text)
            elif f.type == "I":
                self.reset = p.parse_reset(f.text)
                self.counts["head_resets"] += 1
        except p.ProtocolError as e:
            self.counts["parse_error"] += 1
            self.last_error = error = str(e)
        return self._emit(f, p.ack_and_poll(self.ack), error)
