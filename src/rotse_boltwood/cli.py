"""``rotse-boltwood-cli``: check and debug the head without the service.

  rotse-boltwood-cli ports                      list serial ports
  rotse-boltwood-cli check PORT                 poll until one report, print it, exit 0 if ok
  rotse-boltwood-cli monitor PORT               live decoded frames (+ JSONL capture)
  rotse-boltwood-cli monitor PORT --listen      passive: never transmit, show raw bytes
  rotse-boltwood-cli replay FILE.jsonl          decode a capture offline
  rotse-boltwood-cli status [URL]               query a running service

PORT is a device (/dev/ttyUSB0, /dev/cu.usbserial-XXXX), a pyserial URL, or ``sim``.
Don't run monitor/check while the service has the port open.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from . import protocol as p
from .session import Event, Session

# ---------------------------------------------------------------- helpers


def show(data: bytes) -> str:
    """Printable ASCII, control bytes as [0xNN]."""
    return "".join(chr(c) if 32 <= c < 127 else f"[0x{c:02x}]" for c in data)


def fmt(v: Any, spec: str = "") -> str:
    return "--" if v is None else format(v, spec)


def describe_report(r: p.SensorReport) -> str:
    parts = [
        f"sky-amb {fmt(r.sky_minus_ambient_c, '.1f')} C ({r.cloud})",
        f"amb {fmt(r.ambient_c, '.1f')} C",
        f"rh {fmt(r.humidity_pct)} %",
        f"dew {fmt(r.dew_point_c, '.1f')} C",
        f"wind {fmt(r.wind_kmh, '.1f')} km/h ({r.wind})",
        f"wet {r.raw['wet_sensor']} rain {r.raw['rain_sensor']} ({r.rain_cond})",
        f"roof-close {int(r.roof_close_requested)}",
        f"{r.power_voltage:.1f} V",
        r.day,
    ]
    line = "  ".join(parts)
    if r.errors:
        line += "  ERRORS: " + "; ".join(r.errors)
    if r.notes:
        line += "  (" + "; ".join(r.notes) + ")"
    return line


def describe_event(ev: Event, verbose: bool) -> str | None:
    f = ev.frame
    if f.kind == "junk":
        return f"JUNK {show(f.raw)}  ({ev.error}; wrong baud, A/B swapped or no PE?)"
    if f.kind in ("P", "empty", "A", "N") and not verbose:
        return None
    if f.kind != "M":
        return {"P": "P  poll request", "empty": "empty message", "A": "A  ack", "N": "N  NACK"}[f.kind]
    head = f"{f.type}  "
    if f.crc_ok is False:
        return head + f"CRC BAD  {f.text}"
    if ev.error:
        return head + f"PARSE ERROR {ev.error}  {f.text}"
    if f.type == "D":
        return head + describe_report(p.parse_sensor_report(f.text))
    if f.type in ("I", "T"):
        info = p.parse_reset(f.text) if f.type == "I" else p.parse_thresholds(f.text)
        extra = (
            "  head reset"
            if f.type == "I"
            else f"  thresholds: cloudy {info.cloudy_c:g}/{info.very_cloudy_c:g} C,"
            f" windy {info.windy_kmh:g}/{info.very_windy_kmh:g} km/h"
        )
        return head + f"serial {info.serial:05d}  firmware {info.firmware}{extra}"
    return head + f"{p.FRAME_TYPES.get(f.type or '', 'unknown type')}: {f.text}"


class Capture:
    """Raw traffic as JSONL, same format as tests/captures/."""

    def __init__(self, path: Path | None, meta: dict[str, Any]):
        self.f: TextIO | None = None
        self.path = path
        self.t0 = time.monotonic()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.f = path.open("a", buffering=1)
            self.write({"event": "start", **meta})

    def write(self, rec: dict[str, Any]) -> None:
        if self.f is not None:
            t = datetime.now(UTC).isoformat(timespec="milliseconds")
            self.f.write(json.dumps({"t": t, "mono": round(time.monotonic() - self.t0, 4), **rec}) + "\n")

    def bytes(self, direction: str, data: bytes) -> None:
        if data:
            self.write({"dir": direction, "hex": data.hex()})

    def close(self) -> None:
        if self.f is not None:
            self.write({"event": "stop"})
            self.f.close()


def open_port(port: str, baud: int, timeout: float = 0.1):
    if port == "sim":
        from .sim import SimHead

        return SimHead(timeout=timeout)
    import serial

    ser = serial.serial_for_url(port, baudrate=baud, bytesize=8, parity="N", stopbits=1, timeout=timeout)
    ser.reset_input_buffer()
    return ser


def print_counts(session: Session) -> None:
    c = session.counts
    frames = ", ".join(f"{k} {c[k]}" for k in sorted(c) if len(k) == 1 and k not in "PAN")
    other = ", ".join(f"{k} {c[k]}" for k in ("P", "repoll", "junk", "crc_bad", "parse_error", "empty") if c[k])
    print(f"  frames: {frames or 'none'}")
    if other:
        print(f"  other:  {other}")


# ---------------------------------------------------------------- commands


def cmd_ports(_: argparse.Namespace) -> int:
    from serial.tools import list_ports

    ports = list_ports.comports()
    for port in ports:
        print(f"{port.device:34} {port.description}  {port.hwid}")
    if not ports:
        print("no serial ports found")
    return 0


def cmd_monitor(a: argparse.Namespace) -> int:
    ser = open_port(a.port, a.baud)
    log_path = None if a.no_log else (a.log or Path("captures") / f"boltwood-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl")
    cap = Capture(log_path, {"port": a.port, "baud": a.baud, "mode": "listen" if a.listen else "poll", "ack": a.ack})
    t0 = time.monotonic()

    def on_event(ev: Event) -> None:
        line = describe_event(ev, a.verbose)
        if line:
            print(f"{time.monotonic() - t0:8.2f}s  {line}")

    s = Session(ack=a.ack.encode(), repoll_s=a.repoll, on_event=on_event)
    mode = "LISTEN (never transmits): power-cycle the head to see startup" if a.listen else "POLL"
    print(f"{a.port} @ {a.baud} 8N1, {mode}. Ctrl-C to stop." + (f" Capture: {log_path}" if log_path else ""))

    def send(data: bytes) -> None:
        if data and not a.listen:
            ser.write(data)
            cap.bytes("tx", data)
            if a.raw:
                print(f"{time.monotonic() - t0:8.2f}s  -> {show(data)}")

    end = None if a.duration is None else t0 + a.duration
    try:
        send(s.start())
        while end is None or time.monotonic() < end:
            try:
                data = ser.read(256)
            except Exception as e:  # noqa: BLE001 - macOS FTDI reports a break as a read error
                print(f"  read error: {e}")
                cap.write({"event": "read_error", "error": str(e)})
                time.sleep(0.1)
                continue
            if data:
                cap.bytes("rx", data)
                if a.raw or a.listen:
                    print(f"{time.monotonic() - t0:8.2f}s  <- {show(data)}")
            out = s.feed(data)
            send(out + s.tick())
    except KeyboardInterrupt:
        pass
    finally:
        cap.close()
        ser.close()
    print("\n--- summary ---")
    print_counts(s)
    if s.bytes_rx == 0:
        print("  nothing received: PSU on? PE wired to head GND? right port? (listen: power-cycle the head)")
    elif not s.counts.get("D") and not a.listen:
        print("  bytes but no reports: only 0x00s means not polled / A-B swapped; junk means wrong baud")
    if log_path:
        print(f"  capture: {log_path}")
    return 0


def cmd_check(a: argparse.Namespace) -> int:
    ser = open_port(a.port, a.baud)
    s = Session(ack=a.ack.encode(), repoll_s=2.0)
    t_end = time.monotonic() + a.timeout
    try:
        ser.write(s.start())
        while time.monotonic() < t_end and s.report is None:
            out = s.feed(ser.read(256)) + s.tick()
            if out:
                ser.write(out)
    finally:
        ser.close()
    info = s.reset or s.thresholds
    if info:
        print(f"head:   serial {info.serial:05d}, firmware {info.firmware}")
    if s.report is None:
        print(f"FAIL: no report within {a.timeout:g} s ({s.bytes_rx} bytes received)")
        print_counts(s)
        return 1
    r = s.report
    print(f"report: {describe_report(r)}")
    if a.json:
        print(json.dumps(r.raw, indent=2))
    if not r.ok:
        print("FAIL: head reports errors")
        return 1
    print("OK")
    return 0


def cmd_replay(a: argparse.Namespace) -> int:
    rx = b""
    meta: dict[str, Any] = {}
    for line in a.file.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("event") == "start":
            meta = rec
        elif rec.get("dir") == "rx":
            rx += bytes.fromhex(rec["hex"])
    print(
        f"{a.file}: {meta.get('mode', '?')} capture on {meta.get('port', '?')} at {meta.get('t', '?')}, {len(rx)} bytes rx"
    )

    def on_event(ev: Event) -> None:
        line = describe_event(ev, a.verbose)
        if line:
            print("  " + line)

    s = Session(on_event=on_event)
    s.feed(rx)
    if s.buf:
        print(f"  trailing, no newline: {show(s.buf[:80])}{'...' if len(s.buf) > 80 else ''}")
    print_counts(s)
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    base = a.url.rstrip("/")
    docs = {}
    for name in ("health", "report", "head", "pyobs-weather"):
        try:
            with urllib.request.urlopen(f"{base}/api/v1/{name}", timeout=5) as resp:
                docs[name] = (resp.status, json.load(resp))
        except urllib.error.HTTPError as e:
            docs[name] = (e.code, json.load(e))
        except (urllib.error.URLError, OSError) as e:
            print(f"cannot reach {base}: {e}")
            return 2
    code, h = docs["health"]
    port = h["port"]
    print(f"service: {h['station']} v{h['version']} ({h['mode']}) on {h['host']}, up {h['uptime_s']} s -> {code}")
    if h["error"]:
        print(f"         error: {h['error']}")
    print(
        f"port:    {port['device']} @ {port['baud']}  connected={port['connected']}  opens={port['opens']}"
        + (f"  error: {port['error']}" if port["error"] else "")
    )
    _, hd = docs["head"]
    t = hd["thresholds"]
    print(
        f"head:    serial {hd['serial']}  firmware {hd['firmware']}"
        + (
            f"  thresholds cloudy {t['cloudy_c']}/{t['very_cloudy_c']} C windy {t['windy_kmh']}/{t['very_windy_kmh']} km/h"
            if t
            else "  (no T frame yet: arrives every ~100 s)"
        )
    )
    print(f"link:    last message {h['last_message_age_s']} s ago, {h['bytes_rx']} bytes, counts {h['counts']}")
    if h["last_error"]:
        print(f"         last error: {h['last_error']}")
    _, r = docs["report"]
    if r["values"]:
        print(
            f"report:  {r['time']}  age {r['age_s']} s  ok={r['ok']}" + (f"  error: {r['error']}" if r["error"] else "")
        )
        for label, d in (("values", r["values"]), ("conds", r["conditions"])):
            print(f"{label + ':':9}" + "  ".join(f"{k}={v}" for k, v in d.items()))
        if r["notes"]:
            print(f"notes:   {'; '.join(r['notes'])}")
    else:
        print(f"report:  none ({r['error']})")
    _, pw = docs["pyobs-weather"]
    for name, d in pw.get("documents", {}).items():
        line = f"{d['status']}" + (f"  {d['error']}" if d["error"] else f"  {', '.join(d['fields'].values())}")
        print(f"{'pyobs:' if name == next(iter(pw['documents'])) else '':9}{name:8} {line}")
    return 0 if h["ok"] else 1


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rotse-boltwood-cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ports", help="list serial ports").set_defaults(fn=cmd_ports)

    def serial_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("port", help="serial device, pyserial URL, or 'sim'")
        sp.add_argument("--baud", type=int, default=4800)
        sp.add_argument("--ack", choices=["a", "A"], default="a", help="ACK char sent to the head")

    sp = sub.add_parser("check", help="poll until one report; exit 0 if the head is ok")
    serial_args(sp)
    sp.add_argument("--timeout", type=float, default=20.0)
    sp.add_argument("--json", action="store_true", help="also print all raw D fields")
    sp.set_defaults(fn=cmd_check)

    sp = sub.add_parser("monitor", help="live decoded frames, capture to JSONL")
    serial_args(sp)
    sp.add_argument("--listen", action="store_true", help="passive: never transmit")
    sp.add_argument("--raw", action="store_true", help="also print raw bytes in both directions")
    sp.add_argument("-v", "--verbose", action="store_true", help="also show P/A/N frames")
    sp.add_argument("--repoll", type=float, default=2.0, help="re-poll after this long without a message [s]")
    sp.add_argument("--duration", type=float, help="stop after N seconds")
    sp.add_argument("--log", type=Path, help="capture file (default captures/boltwood-<UTC>.jsonl)")
    sp.add_argument("--no-log", action="store_true", help="don't write a capture")
    sp.set_defaults(fn=cmd_monitor)

    sp = sub.add_parser("replay", help="decode a JSONL capture")
    sp.add_argument("file", type=Path)
    sp.add_argument("-v", "--verbose", action="store_true", help="also show P/A/N frames")
    sp.set_defaults(fn=cmd_replay)

    sp = sub.add_parser("status", help="query a running rotse-boltwood service")
    sp.add_argument("url", nargs="?", default="http://localhost:8081")
    sp.set_defaults(fn=cmd_status)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
