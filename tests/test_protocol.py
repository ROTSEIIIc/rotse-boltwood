"""Protocol tests against real captures from head SN 04015 (tests/captures/, 2026-11-06).

Sentinel/error-code tests use a real D frame with single fields changed: those values have
not been seen on our head yet, so they only test that we follow the sources.
"""

import json
from collections import Counter
from pathlib import Path

import pytest

from rotse_boltwood import protocol as p

CAPTURES = Path(__file__).parent / "captures"
POLLED = CAPTURES / "2026-11-06-sn04015-polled.jsonl"
UNPOLLED = CAPTURES / "2026-11-06-sn04015-unpolled.jsonl"

# First D frame of the polled capture, verbatim (type char and CRC stripped).
REAL_D = (
    "0 3 0 1 3 1   -0.6  25.3  -1.0 N N  50  14.0  30.1  70 -99.9 4 24.4  19.3     1   464   417 "
    "074 044 0000 0000 0958 1023 0186 0190   0.0 066 11972 3 0609"
)


def traffic(path: Path, direction: str) -> bytes:
    out = b""
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("dir") == direction:
            out += bytes.fromhex(rec["hex"])
    return out


@pytest.fixture(scope="module")
def frames() -> list[p.Frame]:
    msgs, rest = p.split_frames(traffic(POLLED, "rx"))
    assert rest == b""  # capture ended right after a P frame
    return [p.parse_frame(m) for m in msgs]


def with_field(name: str, value: str) -> str:
    vals = REAL_D.split()
    vals[[n for n, _ in p.D_FIELDS].index(name)] = value
    return " ".join(vals)


# ----------------------------------------------------------------- real capture


def test_capture_frame_counts(frames):
    counts = Counter(f.type if f.kind == "M" else f.kind for f in frames)
    assert counts == {"I": 1, "T": 1, "K": 1, "W": 1, "C": 1, "D": 12, "P": 59}


def test_capture_all_crcs_ok(frames):
    m = [f for f in frames if f.kind == "M"]
    assert len(m) == 17
    assert all(f.crc_ok is True for f in m)


def test_capture_startup_order(frames):
    assert [f.type for f in frames if f.kind == "M"][:6] == ["I", "T", "K", "W", "C", "D"]


def test_capture_host_only_sent_poll_and_ack():
    tx, _ = p.split_frames(traffic(POLLED, "tx").replace(p.REQUEST_POLL, p.REQUEST_POLL + b"\n"))
    assert set(tx) <= {p.REQUEST_POLL + b"\n", p.FRAME_START + p.HOST_ACK + b"\n"}
    assert p.ack_and_poll() == bytes.fromhex("02610a01")


def test_reset_frame(frames):
    i = p.parse_reset(next(f for f in frames if f.type == "I").text)
    assert (i.serial, i.firmware) == (4015, 67)


def test_threshold_frame(frames):
    t = p.parse_thresholds(next(f for f in frames if f.type == "T").text)
    assert (t.serial, t.firmware) == (4015, 67)
    assert (t.cloudy_c, t.very_cloudy_c, t.windy_kmh, t.very_windy_kmh) == (-25.0, -10.0, 15.0, 30.0)
    assert (t.rain, t.wet, t.daylight_code, t.day_threshold, t.very_day_threshold) == (12, 100, 1, 132, 220)
    assert len(t.unknown) == 5


def test_first_sensor_report(frames):
    d = next(f for f in frames if f.type == "D")
    assert d.text == REAL_D
    r = p.parse_sensor_report(d.text)
    assert r.ok
    assert r.sky_minus_ambient_c == -0.6
    assert (r.ambient_c, r.humidity_pct, r.dew_point_c) == (25.3, 50, 14.0)
    assert r.wind_kmh is None and r.notes == ["anemometer heating up"]
    assert (r.wet, r.rain, r.roof_close_requested) == (False, False, True)
    assert r.power_voltage == 24.4
    assert (r.cloud, r.day) == ("very cloudy", "very light")
    # digital thermopile (fw >= 67): analog thermopile ADCs read zero
    assert (r.raw["thermopile_adc"], r.raw["thermistor_adc"]) == (0, 0)
    assert r.raw["raw_wetness_osc"] == 11972 and r.raw["daylight_adc"] == 609


def test_all_sensor_reports(frames):
    reports = [p.parse_sensor_report(f.text) for f in frames if f.type == "D"]
    assert len(reports) == 12
    assert all(r.ok for r in reports)
    assert all(24.2 <= r.power_voltage <= 24.5 for r in reports)  # bench PSU at 24 V
    assert all(-3 < r.sky_minus_ambient_c < 0 for r in reports)  # indoors, ceiling


def test_unpolled_capture_is_lone_zero_bytes_every_2s():
    recs = [json.loads(line) for line in UNPOLLED.read_text().splitlines()]
    rx = [r for r in recs if r.get("dir") == "rx"]
    assert len(rx) > 20 and {r["hex"] for r in rx} == {"00"}
    t = [r["mono"] for r in rx]
    gaps = sorted(b - a for a, b in zip(t, t[1:]))
    assert 2.0 < gaps[len(gaps) // 2] < 2.2
    msgs, rest = p.split_frames(traffic(UNPOLLED, "rx"))
    assert msgs == []


# ----------------------------------------------------------------- framing


def test_split_across_chunks():
    data = b"\x00\x02P\n" + p.build_m_frame("D", " " + REAL_D)
    msgs, rest = p.split_frames(data[:20])
    assert len(msgs) == 1 and rest == data[4:20]
    msgs, rest = p.split_frames(rest + data[20:])
    assert len(msgs) == 1 and rest == b""
    assert p.parse_frame(msgs[0]).type == "D"


def test_leading_zero_and_empty():
    assert p.parse_frame(b"\x00\x02P\n").kind == "P"
    assert p.parse_frame(b"\n").kind == "empty"
    assert p.parse_frame(b"\x00\n").kind == "empty"


def test_junk_raises():
    with pytest.raises(p.ProtocolError):
        p.parse_frame(b"garbage\n")
    with pytest.raises(p.ProtocolError):
        p.parse_frame(b"\x02Zxx\n")


def test_bad_crc_detected():
    good = p.build_m_frame("D", " " + REAL_D)
    bad = good.replace(b"25.3", b"25.4")
    assert p.parse_frame(good).crc_ok is True
    assert p.parse_frame(bad).crc_ok is False


# ----------------------------------------------------------------- sentinels (from the sources)


@pytest.mark.parametrize(
    "value, where, msg",
    [
        ("999.9", "errors", "sky temperature saturated hot"),
        ("-999.9", "errors", "sky temperature saturated cold"),
        ("-998.0", "notes", "sky sensor wet"),
        ("-998.9", "notes", "sky sensor wet"),
    ],
)
def test_sky_sentinels(value, where, msg):
    r = p.parse_sensor_report(with_field("sky_minus_ambient_c", value))
    assert r.sky_minus_ambient_c is None
    assert msg in getattr(r, where)


def test_ambient_comms_failure():
    r = p.parse_sensor_report(with_field("humidity_pct", "0").replace("25.3", "-40.0"))
    assert not r.ok
    assert (r.ambient_c, r.humidity_pct, r.dew_point_c) == (None, None, None)


def test_humidstat_code():
    r = p.parse_sensor_report(with_field("humidstat_temp_code", "3"))
    assert r.errors == ["humidity/ambient sensor: ambient write failure"]
    assert r.ambient_c is None


@pytest.mark.parametrize("code, ok", [("-1.0", True), ("-2.0", True), ("-4.0", False), ("-6.0", False)])
def test_wind_codes(code, ok):
    r = p.parse_sensor_report(with_field("wind_kmh", code))
    assert r.wind_kmh is None and r.ok is ok


def test_real_wind_speed():
    assert p.parse_sensor_report(with_field("wind_kmh", "3.5")).wind_kmh == 3.5


def test_wet_and_rain():
    r = p.parse_sensor_report(with_field("rain_sensor", "r").replace(" N r ", " w r "))
    assert (r.wet, r.rain) == (True, True)


def test_short_d_frame_raises():
    with pytest.raises(p.ProtocolError):
        p.parse_sensor_report("0 3 0 1")


def test_capture_110s_periodic_t_and_warm_anemometer():
    """110 s polled run: T/K/W/C repeat as a group (~100 s), anemometer warmed up (0.0 km/h)."""
    msgs, rest = p.split_frames(traffic(CAPTURES / "2026-11-06-sn04015-polled-110s.jsonl", "rx"))
    frames = [p.parse_frame(m) for m in msgs]
    m = [f for f in frames if f.kind == "M"]
    assert [f.type for f in m if f.type != "D"] == ["T", "K", "W", "C"]
    assert sum(f.type == "D" for f in m) == 52 and all(f.crc_ok for f in m)
    reports = [p.parse_sensor_report(f.text) for f in m if f.type == "D"]
    assert all(r.ok and r.wind_kmh == 0.0 and r.wind == "calm" and not r.notes for r in reports)
    t = p.parse_thresholds(next(f.text for f in m if f.type == "T"))
    assert (t.serial, t.firmware, t.windy_kmh, t.very_windy_kmh) == (4015, 67, 15.0, 30.0)
    assert rest.startswith(b"\x02MD")  # run stopped mid-frame
