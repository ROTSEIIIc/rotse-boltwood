"""Session state machine, simulator, poller loop, config, HTTP API and CLI."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rotse_boltwood import cli
from rotse_boltwood import protocol as p
from rotse_boltwood.api import create_app
from rotse_boltwood.config import Settings
from rotse_boltwood.poller import Poller
from rotse_boltwood.session import Session
from rotse_boltwood.sim import SimHead

from test_protocol import POLLED, REAL_D, traffic, with_field

ACK_POLL = p.ack_and_poll()


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0  # 2027-01-15T08:00:00Z

    def __call__(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += max(dt, 0.001)


def d_frame(text: str = REAL_D) -> bytes:
    return p.build_m_frame("D", " " + text)


# ----------------------------------------------------------------- session


def test_session_replays_capture_with_same_replies():
    """Feeding the real rx stream gives the replies our script actually sent (minus timer re-polls)."""
    replies = []
    s = Session(on_event=lambda ev: replies.append(ev.reply))
    s.feed(traffic(POLLED, "rx"))
    assert s.counts["D"] == 12 and s.counts["P"] == 59 and s.counts["junk"] == 0
    assert set(replies) == {ACK_POLL}
    tx = traffic(POLLED, "tx")
    assert tx.count(ACK_POLL) == len(replies)
    assert s.reset.serial == 4015 and s.thresholds.firmware == 67
    assert s.report.power_voltage == 24.4  # last D of the capture


def test_session_repoll_timer():
    clock = Clock()
    s = Session(repoll_s=2.0, clock=clock)
    assert s.start() == p.REQUEST_POLL
    clock.t += 1.9
    assert s.tick() == b""
    clock.t += 0.2
    assert s.tick() == p.REQUEST_POLL
    assert s.tick() == b""  # just polled
    # lone zeros (unpolled head) don't reset the timer
    clock.t += 1.0
    assert s.feed(b"\x00") == b""
    clock.t += 1.1
    assert s.tick() == p.REQUEST_POLL
    # a complete message does
    assert s.feed(d_frame()) == ACK_POLL
    clock.t += 1.9
    assert s.tick() == b""


def test_session_bad_crc_nacks_and_keeps_old_report():
    s = Session()
    s.feed(d_frame())
    bad = d_frame(with_field("ambient_c", "30.0")).replace(b"30.0", b"31.0")
    assert s.feed(bad) == p.nack_and_poll()
    assert s.report.ambient_c == 25.3 and s.counts["crc_bad"] == 1


def test_session_junk_and_partial():
    s = Session()
    assert s.feed(b"garbage\n") == b""
    assert s.counts["junk"] == 1
    frame = d_frame()
    assert s.feed(frame[:30]) == b""
    assert s.feed(frame[30:]) == ACK_POLL
    assert s.report is not None


def test_session_short_d_is_parse_error_but_acked():
    s = Session()
    assert s.feed(p.build_m_frame("D", " 1 2 3")) == ACK_POLL
    assert s.counts["parse_error"] == 1 and s.report is None


# ----------------------------------------------------------------- simulator


def run_sim(seconds: float, poll: bool) -> tuple[Session, SimHead, Clock]:
    clock = Clock()
    head = SimHead(clock=clock, sleep=clock.sleep)
    s = Session(clock=clock)
    if poll:
        head.write(s.start())
    end = clock.t + seconds
    while clock.t < end:
        out = s.feed(head.read(256))
        if poll:
            out += s.tick()
            if out:
                head.write(out)
    return s, head, clock


def test_sim_unpolled_sends_lone_zeros():
    s, head, _ = run_sim(10, poll=False)
    assert s.bytes_rx == 4 and s.report is None  # one 0x00 every 2.1 s


def test_sim_polled_startup_and_reports():
    s, _, _ = run_sim(10.5, poll=True)
    assert all(s.counts[t] == 1 for t in "ITKWC")
    assert s.counts["D"] == 6  # at 0.3, 2.3, ... 10.3 s
    assert s.counts["crc_bad"] == 0 and s.counts["junk"] == 0
    assert s.report.ok and -30 < s.report.sky_minus_ambient_c < -20
    assert s.reset.serial == 4015


# ----------------------------------------------------------------- poller loop (real thread, sim head)


def test_poller_thread_with_sim_and_reconnect():
    settings = Settings.load(None, reconnect_s=0.1)
    opens = []

    def open_port():
        opens.append(1)
        if len(opens) == 1:
            raise OSError("no such device")
        return SimHead(timeout=0.05)

    poller = Poller(settings, open_port)
    t = threading.Thread(target=poller.run_blocking, daemon=True)
    t.start()
    try:
        deadline = time.time() + 5
        while poller.session.report is None and time.time() < deadline:
            time.sleep(0.05)
    finally:
        poller.stop()
        t.join(2)
    assert not t.is_alive()
    assert len(opens) == 2 and poller.opens == 1
    assert poller.session.report is not None and poller.ok()
    assert poller.port_error is None


GLITCH = "device reports readiness to read but returned no data (device disconnected or multiple access on port?)"


class GlitchyHead(SimHead):
    """SimHead whose read() raises pyserial's macOS 'returned no data' error on chosen calls."""

    def __init__(self, glitch_reads: set[int] | None = None, always: bool = False):
        super().__init__(timeout=0.05)
        self.reads, self.glitch_reads, self.always = 0, glitch_reads or set(), always

    def read(self, n: int = 1) -> bytes:
        import serial

        self.reads += 1
        if self.always or self.reads in self.glitch_reads:
            raise serial.SerialException(GLITCH)
        return super().read(n)


def run_poller(open_port, until, timeout: float = 5.0) -> Poller:
    poller = Poller(Settings.load(None, reconnect_s=0.1), open_port)
    t = threading.Thread(target=poller.run_blocking, daemon=True)
    t.start()
    deadline = time.time() + timeout
    while not until(poller) and time.time() < deadline:
        time.sleep(0.05)
    poller.stop()
    t.join(2)
    assert not t.is_alive()
    return poller


def test_poller_tolerates_isolated_read_glitches():
    poller = run_poller(lambda: GlitchyHead({2, 3, 4}), lambda pl: pl.session.report is not None)
    assert poller.opens == 1 and poller.port_error is None
    assert poller.session.counts["read_glitch"] == 3 and poller.session.report is not None


def test_poller_reconnects_on_repeated_glitches():
    heads = []

    def open_port():
        heads.append(GlitchyHead(always=len(heads) == 0))  # first port: unplugged; second: fine
        return heads[-1]

    poller = run_poller(open_port, lambda pl: pl.session.report is not None)
    assert poller.opens == 2 and poller.session.report is not None
    assert heads[0].reads == 6  # 5 tolerated glitches, the 6th gives up


# ----------------------------------------------------------------- config


def test_config_defaults_yaml_env(tmp_path: Path, monkeypatch):
    s = Settings.load(None)
    assert (s.port, s.baud, s.api.port, s.ack) == ("/dev/ttyUSB0", 4800, 8081, "a")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("port: /dev/ttyUSB3\nmax_age_s: 60\n")
    monkeypatch.setenv("BOLTWOOD_API__PORT", "9000")
    s = Settings.load(cfg)
    assert (s.port, s.max_age_s, s.api.port) == ("/dev/ttyUSB3", 60, 9000)


def test_config_rejects_typos_and_bad_fields(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("prot: /dev/ttyUSB0\n")
    with pytest.raises(ValidationError):
        Settings.load(cfg)
    cfg.write_text("api: {prot: 9000}\n")  # typo inside a section
    with pytest.raises(ValidationError):
        Settings.load(cfg)


# ----------------------------------------------------------------- API


@pytest.fixture
def rig():
    clock = Clock()
    poller = Poller(Settings.load(None), open_port=lambda: None, clock=clock)
    poller.heartbeat_ts = clock()
    return clock, poller, TestClient(create_app(poller, mode="test"))


T_TEXT = " 04015 00067 00000 -25.0 -10.0 15.0 30.0 12 100 1 132 220 5 1 2 0 1"


def test_api_index(rig):
    _, _, client = rig
    eps = client.get("/").json()["endpoints"]
    assert {"/api/v1/report", "/api/v1/health", "/api/v1/head", "/api/v1/frames", "/api/v1/codes"} <= set(eps)


def test_api_before_first_report(rig):
    _, _, client = rig
    h = client.get("/api/v1/health")
    assert h.status_code == 503 and h.json()["error"] == "no report yet"
    r = client.get("/api/v1/report").json()
    assert r["ok"] is False and r["error"] == "no report yet" and r["values"] is None
    hd = client.get("/api/v1/head").json()
    assert hd["serial"] is None and hd["thresholds"] is None


def test_api_report(rig):
    clock, poller, client = rig
    poller.session.feed(d_frame())
    assert client.get("/api/v1/health").status_code == 200
    r = client.get("/api/v1/report").json()
    assert r["ok"] is True and r["error"] is None and r["time"] == "2027-01-15T08:00:00Z" and r["age_s"] == 0
    assert r["values"]["sky_minus_ambient_c"] == -0.6 and r["values"]["wind_kmh"] is None
    assert r["values"]["power_voltage"] == 24.4 and r["values"]["rain"] is False
    assert r["conditions"] == {
        "cloud": "very cloudy",
        "wind": "unknown",
        "rain": "dry",
        "day": "very light",
        "wet_sensor": "dry",
        "rain_sensor": "no rain",
        "rain_heater": "too cold",
    }
    assert r["notes"] == ["anemometer heating up"] and len(r["raw"]) == 35

    clock.t += 31  # max_age_s = 30
    h = client.get("/api/v1/health")
    assert h.status_code == 503 and h.json()["error"].startswith("stale")
    assert client.get("/api/v1/report").json()["ok"] is False


def test_api_head_and_frames(rig):
    _, poller, client = rig
    poller.session.feed(p.build_m_frame("T", T_TEXT) + b"\x00\x02P\n" + d_frame() + b"junk\n")
    hd = client.get("/api/v1/head").json()
    assert (hd["serial"], hd["firmware"]) == (4015, 67)
    assert hd["thresholds"]["windy_kmh"] == 15.0 and set(hd["frames"]) == {"T"}
    f = client.get("/api/v1/frames").json()
    assert [x["type"] or x["kind"] for x in f["frames"]] == ["T", "D", "junk"]
    assert f["frames"][2]["raw_hex"] == b"junk\n".hex() and f["frames"][1]["reply_hex"] == "02610a01"
    assert client.get("/api/v1/frames", params={"all": 1}).json()["count"] == 4
    assert [x["type"] for x in client.get("/api/v1/frames", params={"type": "D"}).json()["frames"]] == ["D"]
    assert client.get("/api/v1/codes").json()["wind_kmh_negative"]["-1"] == "anemometer heating up"


def test_api_head_error_shows_in_report(rig):
    _, poller, client = rig
    poller.session.feed(d_frame(with_field("humidstat_temp_code", "2")))
    r = client.get("/api/v1/report").json()
    assert r["ok"] is False and "humidity measurement never finished" in r["error"]
    assert r["values"]["ambient_c"] is None


# ----------------------------------------------------------------- pyobs-weather


def test_pyobs_all_documents_ok(rig):
    _, poller, client = rig
    poller.session.feed(d_frame(with_field("wind_kmh", "3.5")))
    t = "2027-01-15T08:00:00Z"
    get = lambda n: client.get(f"/api/v1/pyobs-weather/{n}")  # noqa: E731
    assert get("rain").json() == {"time": t, "rain": 0.0}
    assert get("sky").json() == {"time": t, "skytemp": -0.6}
    assert get("ambient").json() == {"time": t, "temp": 25.3, "humid": 50.0, "dewpoint": 14.0}
    assert get("wind").json() == {"time": t, "windspeed": 3.5}
    for n in ("rain", "sky", "ambient", "wind"):
        assert all(isinstance(v, float) for k, v in get(n).json().items() if k != "time")
    idx = client.get("/api/v1/pyobs-weather").json()["documents"]
    assert {n: d["status"] for n, d in idx.items()} == {"rain": 200, "sky": 200, "ambient": 200, "wind": 200}


def test_pyobs_wet_head_keeps_rain_document(rig):
    """The reason for one document per quantity: a wet head invalidates sky and wind, not rain."""
    _, poller, client = rig
    wet = with_field("sky_minus_ambient_c", "-998.0")
    wet = " ".join(v if i != 8 else "-2.0" for i, v in enumerate(wet.split()))  # wind_kmh
    wet = " ".join(v if i != 9 else "W" for i, v in enumerate(wet.split()))  # wet_sensor
    poller.session.feed(d_frame(wet))
    assert client.get("/api/v1/pyobs-weather/rain").json()["rain"] == 1.0
    assert client.get("/api/v1/pyobs-weather/ambient").status_code == 200
    sky = client.get("/api/v1/pyobs-weather/sky")
    assert sky.status_code == 503 and sky.json()["error"].startswith("sky_minus_ambient_c not valid (")
    assert "sky sensor wet" in sky.json()["error"]
    wind = client.get("/api/v1/pyobs-weather/wind")
    assert wind.status_code == 503 and "anemometer wet" in wind.json()["error"]


def test_pyobs_rain_sensor_alone_counts(rig):
    _, poller, client = rig
    poller.session.feed(d_frame(with_field("rain_sensor", "r")))
    assert client.get("/api/v1/pyobs-weather/rain").json()["rain"] == 1.0


def test_pyobs_warming_anemometer_only_blocks_wind(rig):
    _, poller, client = rig
    poller.session.feed(d_frame())  # real frame: wind -1.0
    w = client.get("/api/v1/pyobs-weather/wind")
    assert w.status_code == 503 and "anemometer heating up" in w.json()["error"]
    assert client.get("/api/v1/pyobs-weather/sky").status_code == 200


def test_pyobs_no_report_stale_unknown(rig):
    clock, poller, client = rig
    r = client.get("/api/v1/pyobs-weather/rain")
    assert r.status_code == 503 and r.json()["error"] == "no report yet"
    poller.session.feed(d_frame())
    clock.t += 31
    r = client.get("/api/v1/pyobs-weather/rain")
    assert r.status_code == 503 and r.json()["error"].startswith("stale")
    assert client.get("/api/v1/pyobs-weather/clouds").status_code == 404


def test_pyobs_head_fault_blocks_affected_document(rig):
    _, poller, client = rig
    poller.session.feed(d_frame(with_field("humidstat_temp_code", "4")))
    a = client.get("/api/v1/pyobs-weather/ambient")
    assert a.status_code == 503 and "ambient measurement never finished" in a.json()["error"]
    assert client.get("/api/v1/pyobs-weather/rain").status_code == 200


def test_pyobs_config(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pyobs_weather:\n  documents:\n    all: {rain: rain, sky_minus_ambient_c: skytemp}\n")
    assert Settings.load(cfg).pyobs_weather.documents == {"all": {"rain": "rain", "sky_minus_ambient_c": "skytemp"}}
    for bad in ("{x: {}}", "{x: {cloudiness: clouds}}", "{x: {rain: time}}"):
        cfg.write_text(f"pyobs_weather: {{documents: {bad}}}\n")
        with pytest.raises(ValidationError):
            Settings.load(cfg)


# ----------------------------------------------------------------- CLI


def test_cli_replay(capsys):
    assert cli.main(["replay", str(POLLED)]) == 0
    out = capsys.readouterr().out
    assert "serial 04015  firmware 67" in out
    assert out.count("\n  D  sky-amb") == 12
    assert "frames: C 1, D 12, I 1, K 1, T 1, W 1" in out


def test_cli_check_sim(capsys):
    assert cli.main(["check", "sim", "--timeout", "5"]) == 0
    out = capsys.readouterr().out
    assert "serial 04015, firmware 67" in out and out.strip().endswith("OK")


def test_cli_monitor_sim_writes_capture(tmp_path: Path, capsys):
    log = tmp_path / "cap.jsonl"
    assert cli.main(["monitor", "sim", "--duration", "1.5", "--log", str(log)]) == 0
    out = capsys.readouterr().out
    assert "I  serial 04015" in out and "D  sky-amb" in out
    recs = [json.loads(line) for line in log.read_text().splitlines()]
    assert recs[0]["event"] == "start" and recs[-1]["event"] == "stop"
    assert any(r.get("dir") == "tx" for r in recs) and any(r.get("dir") == "rx" for r in recs)
    # the capture replays to the same frames
    assert cli.main(["replay", str(log)]) == 0
