"""Boltwood Cloud Sensor II head protocol (RS-485, 4800 8N1). Pure functions, no I/O.

Sources, in order of authority:
  [M]  Cloud Sensor II User's Manual V0029 (Diffraction Limited): Appendix A "D" record,
       section 17 data file, section 20 COM enums. Units, codes, sentinels.
  [E]  eddyem/boltwood Protocol.md + term.c: wire framing, CRC, host commands, T/I/K/W/C layouts.
  [T]  thusser/boltwood: poll/ACK loop.
  [04015] seen on our head, SN 04015 firmware 67, 2026-11-06 (tests/captures/).

Confirmed on our head [04015]:
  - Host sends REQUEST_POLL (0x01); head answers with M frames; host ACKs each with
    0x02 'a' '\\n' 0x01 (ACK + poll again). After each D the head sends 0x00 and a burst of
    P frames (0x02 'P' '\\n') about 0.2 s apart; we answer each with the same ACK + poll.
  - Unpolled, the head sends a lone 0x00 every ~2.1 s and nothing else.
  - M frame = 0x02 'M' <type><text><CRC as 4 hex digits> '\\n'; CRC16/XMODEM
    (binascii.crc_hqx, init 0) over <type><text>, 'M' excluded. All frames passed.
  - After the first poll: I, T, K, W, C, then D about every 2 s. T, K, W, C repeat as a
    group: seen 79 s into a 110 s run ([M]: T every ~100 s).
  - D has 35 fields, the same layout as [M] Appendix A; T has 17, I has 6 ([E] order).
    powerVoltage matched the bench supply (24.3-24.4 V). T thresholds 15.0/30.0 are [M]'s
    default windy/very windy in km/h, which confirms speeds are km/h on the wire.
  - thermopileADC and thermistorADC read 0 (digital thermopile, firmware >= 67).

Not yet seen on our head: every sentinel and error code except wind -1 (warming up).
They're handled as [M] describes.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass, field
from typing import Any

REQUEST_POLL = b"\x01"
FRAME_START = b"\x02"
FRAME_END = b"\n"

# Host -> head single-character frames ([E] term.h: CMD_ACK 'a', CMD_NACK 'n').
# [T] sends 'A' instead; 'a' is what we use and what the head accepted.
HOST_ACK = b"a"
HOST_NACK = b"n"

FRAME_TYPES = {
    "C": "thermopile calibration",
    "D": "sensor report",
    "I": "reset",
    "K": "wetness calibration",
    "Q": "echo of host command",
    "R": "roof close",
    "T": "thresholds",
    "W": "wetness data",
}


class ProtocolError(ValueError):
    """A message that doesn't fit the frame format."""


# --------------------------------------------------------------------------- framing


def crc16(data: bytes) -> int:
    """CRC16/XMODEM (poly 0x1021, init 0), same table as [E] term.c calcCRC16."""
    return binascii.crc_hqx(data, 0)


def poll_request() -> bytes:
    return REQUEST_POLL


def ack_and_poll(ack: bytes = HOST_ACK) -> bytes:
    """ACK the last frame and ask for the next one, in one write ([E] send_symbol)."""
    return FRAME_START + ack + FRAME_END + REQUEST_POLL


def nack_and_poll() -> bytes:
    return ack_and_poll(HOST_NACK)


def split_frames(buf: bytes) -> tuple[list[bytes], bytes]:
    """Split a receive buffer at '\\n'. Returns (complete messages incl. '\\n', remainder)."""
    out = []
    while True:
        i = buf.find(FRAME_END)
        if i < 0:
            return out, buf
        out.append(buf[: i + 1])
        buf = buf[i + 1 :]


@dataclass(frozen=True)
class Frame:
    """One message from the head.

    kind: 'P' poll request, 'A' ack, 'N' nack, 'M' data, 'empty' (bare '\\n').
    For 'M': type is the report type ('D', 'T', ...), text the payload without type or CRC,
    crc_ok whether the CRC matched (None if there was no hex CRC to check).
    """

    kind: str
    type: str | None = None
    text: str = ""
    crc_ok: bool | None = None
    raw: bytes = b""


def parse_frame(raw: bytes) -> Frame:
    """Parse one message as returned by split_frames.

    Leading 0x00 bytes are dropped (the head sends one before each P burst, and alone every
    ~2.1 s when unpolled). Anything else before 0x02 is a ProtocolError.
    """
    msg = raw.lstrip(b"\x00")
    if msg in (b"", FRAME_END):
        return Frame(kind="empty", raw=raw)
    if not msg.startswith(FRAME_START):
        raise ProtocolError(f"no frame start: {raw!r}")
    body = msg[1:].rstrip(b"\n").rstrip(b"\r")
    if not body:
        return Frame(kind="empty", raw=raw)
    kind = chr(body[0])
    if kind in ("P", "A", "N"):
        return Frame(kind=kind, raw=raw)
    if kind != "M":
        raise ProtocolError(f"unknown frame kind {kind!r}: {raw!r}")

    payload, crc_ok = body[1:], None
    if len(payload) >= 5:
        try:
            got = int(payload[-4:].decode("ascii"), 16)
        except ValueError:
            got = None
        if got is not None:
            payload, crc_ok = payload[:-4], crc16(payload[:-4]) == got
    if not payload:
        raise ProtocolError(f"empty M frame: {raw!r}")
    return Frame(
        kind="M",
        type=chr(payload[0]),
        text=payload[1:].decode("ascii", "replace").strip(),
        crc_ok=crc_ok,
        raw=raw,
    )


def build_m_frame(type_: str, text: str) -> bytes:
    """Encode an M frame the way the head does. For tests and simulators."""
    payload = (type_ + text).encode("ascii")
    return FRAME_START + b"M" + payload + b"%04X" % crc16(payload) + FRAME_END


# --------------------------------------------------------------------------- D report

# (name, type) in [M] Appendix A / [E] order. Headings in [M]:
# D E C W R 1 c SKY AMB WIND w r HUM DEW CASE HEA BLKT H PWR WNDTD WDROP
# WAVG WDRY RHT AHT ASKY ACSE APSV ABLK AWND AVNE DKMPH VNE RWOSC D ADAY
D_FIELDS: list[tuple[str, type]] = [
    ("humidstat_temp_code", int),  # E: humidity/ambient sensor error code
    ("cloud_cond", int),  # C
    ("wind_cond", int),  # W
    ("rain_cond", int),  # R
    ("sky_cond", int),  # 1: SkyCond for Cloud Sensor I programs
    ("roof_close_requested", int),  # c
    ("sky_minus_ambient_c", float),  # SKY
    ("ambient_c", float),  # AMB
    ("wind_kmh", float),  # WIND, km/h [M]; negatives are status codes
    ("wet_sensor", str),  # w
    ("rain_sensor", str),  # r
    ("humidity_pct", int),  # HUM
    ("dew_point_c", float),  # DEW
    ("case_c", float),  # CASE: thermopile case temperature
    ("rain_heater_pct", int),  # HEA
    ("black_body_c", float),  # BLKT: factory calibration only
    ("rain_heater_state", int),  # H
    ("power_voltage", float),  # PWR: +24 V line measured at the head
    ("anemometer_temp_diff_c", float),  # WNDTD: tip minus ambient, capped at 25 C
    ("wetness_drop", int),  # WDROP
    ("wetness_avg", int),  # WAVG
    ("wetness_dry", int),  # WDRY
    ("rain_heater_pwm", int),  # RHT
    ("anemometer_heater_pwm", int),  # AHT
    ("thermopile_adc", int),  # ASKY (0 on digital thermopile heads)
    ("thermistor_adc", int),  # ACSE (0 on digital thermopile heads)
    ("power_adc", int),  # APSV
    ("block_adc", int),  # ABLK
    ("anemometer_thermistor_adc", int),  # AWND
    ("davis_vane_adc", int),  # AVNE: factory only
    ("dk_mph", float),  # DKMPH: external anemometer, factory only
    ("ext_anemometer_direction", int),  # VNE: factory only
    ("raw_wetness_osc", int),  # RWOSC
    ("day_cond", int),  # D
    ("daylight_adc", int),  # ADAY: 0 dark .. 1023 brightest [E]
]

# Status code tables. [M] section 20 enums unless marked.
HUMIDSTAT_CODES = {  # [M] Appendix A, firmware >= 54
    0: "ok",
    1: "humidity write failure",
    2: "humidity measurement never finished",
    3: "ambient write failure",
    4: "ambient measurement never finished",
    5: "humidity data line not high",
    6: "ambient data line not high",
}
CLOUD_CODES = {0: "unknown", 1: "clear", 2: "cloudy", 3: "very cloudy"}
WIND_CODES = {0: "unknown", 1: "calm", 2: "windy", 3: "very windy"}
RAIN_CODES = {0: "unknown", 1: "dry", 2: "wet", 3: "rain"}  # 2 = water on sensor, 3 = drops falling
DAY_CODES = {0: "unknown", 1: "dark", 2: "light", 3: "very light"}
SKY_CODES = {0: "unknown", 1: "clear", 2: "cloudy", 3: "very cloudy", 4: "wet"}  # [E]; Cloud Sensor I
WET_SENSOR = {"N": "dry", "W": "wet now", "w": "wet in last minute"}
RAIN_SENSOR = {"N": "no rain", "R": "drops this cycle", "r": "drops in last minute"}
RAIN_HEATER_STATES = {  # [E] only, unverified ([T] disagrees on 0/1)
    0: "too hot",
    1: "at target",
    2: "too cold",
    3: "too cold",
    4: "too cold",
    5: "too cold",
    6: "cannot control: case temperature saturated (shutdown)",
    7: "normal control",
}
# Negative wind_kmh = anemometer status, [M] Appendix A. Firmware >= 56 (ours is 67).
WIND_ERRORS = {
    1: "anemometer heating up",
    2: "anemometer wet",
    3: "anemometer A/D bad (firmware < 56 only)",
    4: "anemometer not heating",
    5: "anemometer shorted (A/D low)",
    6: "no anemometer probe (A/D high)",
}
WIND_NOTES = {1, 2}  # expected states, not faults


def _code(table: dict[int, str], v: int) -> str:
    return table.get(v, f"code {v}")


@dataclass
class SensorReport:
    """A parsed D frame.

    `raw` has every field as sent. The top-level values are None when the head flags them
    as invalid; the reason is in `errors` (faults) or `notes` (expected states, such as the
    anemometer warming up or the sky sensor being wet).
    """

    raw: dict[str, Any]
    sky_minus_ambient_c: float | None
    ambient_c: float | None
    humidity_pct: int | None
    dew_point_c: float | None
    wind_kmh: float | None
    case_c: float | None
    power_voltage: float
    wet: bool
    rain: bool
    roof_close_requested: bool
    cloud: str
    wind: str
    rain_cond: str
    day: str
    rain_heater: str
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_sensor_report(text: str) -> SensorReport:
    """Parse the text of a D frame (after the type character, CRC removed)."""
    vals = text.split()
    if len(vals) < len(D_FIELDS):
        raise ProtocolError(f"D frame has {len(vals)} fields, expected {len(D_FIELDS)}: {text!r}")
    raw: dict[str, Any] = {}
    for (name, typ), v in zip(D_FIELDS, vals):
        try:
            raw[name] = typ(v)
        except ValueError as e:
            raise ProtocolError(f"D field {name}={v!r}: {e}") from None
    if len(vals) > len(D_FIELDS):
        raw["extra"] = vals[len(D_FIELDS) :]

    errors: list[str] = []
    notes: list[str] = []

    # SKY: 999.9 saturated hot, -999.9 saturated cold, -998.0 wet [M]. [T] lists -998.9 for
    # wet; both fall in the band below.
    sky: float | None = raw["sky_minus_ambient_c"]
    if sky >= 999.0:
        errors.append("sky temperature saturated hot")
        sky = None
    elif sky <= -999.0:
        errors.append("sky temperature saturated cold")
        sky = None
    elif sky <= -997.5:
        notes.append("sky sensor wet")
        sky = None

    # Ambient -40 together with 0 % humidity = no comms with those sensors [E].
    ambient: float | None = raw["ambient_c"]
    humidity: int | None = raw["humidity_pct"]
    dew: float | None = raw["dew_point_c"]
    if raw["ambient_c"] == -40.0 and raw["humidity_pct"] == 0:
        errors.append("no comms with ambient/humidity sensor (water ingress?)")
        ambient = humidity = dew = None
    hcode = raw["humidstat_temp_code"]
    if hcode != 0:
        errors.append(f"humidity/ambient sensor: {_code(HUMIDSTAT_CODES, hcode)}")
        ambient = humidity = dew = None

    wind: float | None = raw["wind_kmh"]
    if wind < 0:
        code = round(-wind)
        msg = WIND_ERRORS.get(code, f"anemometer code {wind:g}")
        (notes if code in WIND_NOTES else errors).append(msg)
        wind = None

    # CASE: 999.9 saturated hot, -99.9 saturated cold [M] ("should never occur").
    case: float | None = raw["case_c"]
    if case >= 999.0 or case <= -99.9:
        errors.append("thermopile case temperature saturated")
        case = None

    if raw["rain_heater_state"] == 6:
        errors.append("rain heater: " + RAIN_HEATER_STATES[6])
    for key, table in (("wet_sensor", WET_SENSOR), ("rain_sensor", RAIN_SENSOR)):
        if raw[key] not in table:
            errors.append(f"unknown {key} value {raw[key]!r}")

    return SensorReport(
        raw=raw,
        sky_minus_ambient_c=sky,
        ambient_c=ambient,
        humidity_pct=humidity,
        dew_point_c=dew,
        wind_kmh=wind,
        case_c=case,
        power_voltage=raw["power_voltage"],
        wet=raw["wet_sensor"] in ("W", "w"),
        rain=raw["rain_sensor"] in ("R", "r"),
        roof_close_requested=raw["roof_close_requested"] == 1,
        cloud=_code(CLOUD_CODES, raw["cloud_cond"]),
        wind=_code(WIND_CODES, raw["wind_cond"]),
        rain_cond=_code(RAIN_CODES, raw["rain_cond"]),
        day=_code(DAY_CODES, raw["day_cond"]),
        rain_heater=_code(RAIN_HEATER_STATES, raw["rain_heater_state"]),
        errors=errors,
        notes=notes,
    )


# --------------------------------------------------------------------------- T and I


@dataclass
class Thresholds:
    """T frame, sent at startup and every ~100 s [M]. [E] order; last five fields unknown.

    Thresholds are what the head uses for its own cloud/wind/rain/day conditions and the
    roof-close request. Speeds in km/h.
    """

    serial: int
    firmware: int
    send_errors: int
    cloudy_c: float
    very_cloudy_c: float
    windy_kmh: float
    very_windy_kmh: float
    rain: int
    wet: int
    daylight_code: int
    day_threshold: int
    very_day_threshold: int
    unknown: list[str]


def parse_thresholds(text: str) -> Thresholds:
    v = text.split()
    if len(v) < 12:
        raise ProtocolError(f"T frame has {len(v)} fields, expected >= 12: {text!r}")
    try:
        return Thresholds(
            serial=int(v[0]),
            firmware=int(v[1]),
            send_errors=int(v[2]),
            cloudy_c=float(v[3]),
            very_cloudy_c=float(v[4]),
            windy_kmh=float(v[5]),
            very_windy_kmh=float(v[6]),
            rain=int(v[7]),
            wet=int(v[8]),
            daylight_code=int(v[9]),
            day_threshold=int(v[10]),
            very_day_threshold=int(v[11]),
            unknown=v[12:],
        )
    except ValueError as e:
        raise ProtocolError(f"T frame: {e}: {text!r}") from None


@dataclass
class ResetInfo:
    """I frame, sent once after power-up / first poll: "I serial firmware 0x.. .. 0x.. .."."""

    serial: int
    firmware: int
    rest: list[str]


def parse_reset(text: str) -> ResetInfo:
    v = text.split()
    if len(v) < 2:
        raise ProtocolError(f"I frame too short: {text!r}")
    try:
        return ResetInfo(serial=int(v[0]), firmware=int(v[1]), rest=v[2:])
    except ValueError as e:
        raise ProtocolError(f"I frame: {e}: {text!r}") from None
