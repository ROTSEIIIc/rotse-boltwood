"""HTTP API (FastAPI). Read-only; everything comes from the poller's in-memory state.

GET /                   endpoint list
GET /api/v1/report      latest D report: values, conditions, errors/notes, all raw fields, age_s, ok
GET /api/v1/health      serial link + loop status, counters; 503 if the loop is dead or the report stale
GET /api/v1/head        serial, firmware, thresholds, latest I/T/C/K/W frames
GET /api/v1/frames      recent messages from the head (?limit=50&type=D&all=1)
GET /api/v1/codes       the status code tables used to decode reports
GET /api/v1/pyobs-weather          configured pyobs-weather documents and their status
GET /api/v1/pyobs-weather/{name}   one flat document for a pyobs-weather JSON station

pyobs-weather 2.2.5's JSON station (weather/stations/json.py) does ``weather[field]`` for every
configured field and ``isoparse(weather["time"])``, and skips the update on any status other
than 200. So each document is flat, always has every configured key, ``time`` = time of the
report (not of the request), values are floats (InfluxDB rejects a field whose type changes;
booleans become 0.0/1.0), and it's 503 instead of stale or invalid data. Same rules as
rotse-weather-mast.

The README documents every field and message.
"""

from __future__ import annotations

import platform
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from . import __version__
from . import protocol as p
from .poller import Poller
from .session import Event


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _round(v: float | None, n: int = 1) -> float | None:
    return None if v is None else round(v, n)


def _event(ev: Event) -> dict[str, Any]:
    f = ev.frame
    return {
        "time": _iso(ev.ts),
        "kind": f.kind,
        "type": f.type,
        "name": p.FRAME_TYPES.get(f.type or "") if f.kind == "M" else None,
        "text": f.text if f.kind == "M" else None,
        "crc_ok": f.crc_ok,
        "error": ev.error,
        "raw_hex": f.raw.hex() if f.kind in ("junk",) else None,
        "reply_hex": ev.reply.hex() or None,
    }


def pyobs_values(r: p.SensorReport) -> dict[str, float | None]:
    """Values a pyobs-weather document can carry (config.PyobsField). None = invalid."""
    return {
        "sky_minus_ambient_c": r.sky_minus_ambient_c,
        "ambient_c": r.ambient_c,
        "humidity_pct": None if r.humidity_pct is None else float(r.humidity_pct),
        "dew_point_c": r.dew_point_c,
        "wind_kmh": r.wind_kmh,
        "rain": float(r.rain),
        "wet": float(r.wet),
        "rain_or_wet": float(r.rain or r.wet),
    }


def pyobs_document(poller: Poller, name: str) -> tuple[int, dict[str, Any]]:
    """(status, body) for /api/v1/pyobs-weather/{name}."""
    fields = poller.settings.pyobs_weather.documents.get(name)
    if fields is None:
        return 404, {"ok": False, "error": f"unknown document '{name}'"}
    r = poller.session.report
    if r is None or not poller.fresh():
        return 503, {"ok": False, "error": poller.error() or "no fresh report"}
    values = pyobs_values(r)
    bad = [f for f in fields if values[f] is None]
    if bad:
        why = "; ".join(r.errors + r.notes) or "flagged invalid by the head"
        return 503, {"ok": False, "error": f"{', '.join(bad)} not valid ({why})"}
    return 200, {"time": _iso(poller.session.report_ts), **{code: values[f] for f, code in fields.items()}}


def report_doc(poller: Poller) -> dict[str, Any]:
    s, r = poller.session, poller.session.report
    return {
        "time": _iso(s.report_ts),
        "age_s": _round(poller.report_age_s()),
        "ok": poller.ok(),
        "error": poller.error(),
        "values": (
            None
            if r is None
            else {
                "sky_minus_ambient_c": r.sky_minus_ambient_c,
                "ambient_c": r.ambient_c,
                "humidity_pct": r.humidity_pct,
                "dew_point_c": r.dew_point_c,
                "wind_kmh": r.wind_kmh,
                "wet": r.wet,
                "rain": r.rain,
                "roof_close_requested": r.roof_close_requested,
                "case_c": r.case_c,
                "power_voltage": r.power_voltage,
            }
        ),
        "conditions": (
            None
            if r is None
            else {
                "cloud": r.cloud,
                "wind": r.wind,
                "rain": r.rain_cond,
                "day": r.day,
                "wet_sensor": p.WET_SENSOR.get(r.raw["wet_sensor"], r.raw["wet_sensor"]),
                "rain_sensor": p.RAIN_SENSOR.get(r.raw["rain_sensor"], r.raw["rain_sensor"]),
                "rain_heater": r.rain_heater,
            }
        ),
        "errors": [] if r is None else r.errors,
        "notes": [] if r is None else r.notes,
        "raw": None if r is None else r.raw,
    }


def create_app(poller: Poller, mode: str = "hardware", lifespan: Any = None) -> FastAPI:
    app = FastAPI(title="rotse-boltwood", version=__version__, lifespan=lifespan)
    st = poller.settings

    @app.get("/")
    def index() -> dict[str, Any]:
        return {
            "service": "rotse-boltwood",
            "version": __version__,
            "endpoints": {
                "/api/v1/report": "latest sensor report",
                "/api/v1/health": "serial link and loop status (503 if not ok)",
                "/api/v1/head": "serial number, firmware, thresholds, calibration frames",
                "/api/v1/frames": "recent messages (?limit=50&type=D&all=1)",
                "/api/v1/codes": "status code tables",
                "/api/v1/pyobs-weather": "pyobs-weather documents and their status",
                "/api/v1/pyobs-weather/{name}": "flat document for a pyobs-weather JSON station (503 if not valid)",
                "/docs": "interactive API docs",
            },
        }

    @app.get("/api/v1/report")
    def report() -> dict[str, Any]:
        return {"station": st.station, **report_doc(poller)}

    @app.get("/api/v1/health")
    def health() -> JSONResponse:
        sess = poller.session
        info = sess.reset or sess.thresholds  # both carry serial + firmware
        healthy = poller.loop_healthy() and poller.fresh()
        body = {
            "station": st.station,
            "version": __version__,
            "mode": mode,
            "ok": healthy,
            "error": None if healthy else (poller.error() or "serial loop not running"),
            "host": platform.node(),
            "uptime_s": round(poller.clock() - poller.started_ts),
            "port": {
                "device": "sim" if mode == "sim" else st.port,
                "baud": st.baud,
                "connected": poller.connected,
                "error": poller.port_error,
                "opens": poller.opens,
            },
            "head": {"serial": info.serial if info else None, "firmware": info.firmware if info else None},
            "last_message_age_s": _round(sess.message_age_s()),
            "report_age_s": _round(poller.report_age_s()),
            "last_error": sess.last_error,
            "bytes_rx": sess.bytes_rx,
            "counts": dict(sess.counts),
        }
        return JSONResponse(status_code=200 if healthy else 503, content=body)

    @app.get("/api/v1/head")
    def head() -> dict[str, Any]:
        sess = poller.session
        info = sess.reset or sess.thresholds
        return {
            "serial": info.serial if info else None,
            "firmware": info.firmware if info else None,
            "thresholds": None if sess.thresholds is None else asdict(sess.thresholds),
            "reset": None if sess.reset is None else asdict(sess.reset),
            "frames": {
                t: {"time": _iso(ev.ts), "name": p.FRAME_TYPES.get(t), "text": ev.frame.text}
                for t, ev in sorted(sess.latest.items())
                if t != "D"
            },
            "note": "T (serial, firmware, thresholds), K, W, C arrive at startup and about every 100 s",
        }

    @app.get("/api/v1/frames")
    def frames(
        limit: int = Query(50, ge=1, le=10000),
        type: str | None = Query(None, max_length=1, description="only M frames of this type, e.g. D"),
        all: bool = Query(False, description="include P/A/N/empty messages"),
    ) -> dict[str, Any]:
        evs = list(poller.session.recent)
        if type:
            evs = [e for e in evs if e.frame.kind == "M" and e.frame.type == type]
        elif not all:
            evs = [e for e in evs if e.frame.kind in ("M", "junk")]
        return {"count": len(evs[-limit:]), "frames": [_event(e) for e in evs[-limit:]]}

    @app.get("/api/v1/codes")
    def codes() -> dict[str, Any]:
        return {
            "cloud_cond": p.CLOUD_CODES,
            "wind_cond": p.WIND_CODES,
            "rain_cond": p.RAIN_CODES,
            "day_cond": p.DAY_CODES,
            "sky_cond": p.SKY_CODES,
            "humidstat_temp_code": p.HUMIDSTAT_CODES,
            "rain_heater_state": p.RAIN_HEATER_STATES,
            "wet_sensor": p.WET_SENSOR,
            "rain_sensor": p.RAIN_SENSOR,
            "wind_kmh_negative": {-k: v for k, v in p.WIND_ERRORS.items()},
            "frame_types": p.FRAME_TYPES,
        }

    @app.get("/api/v1/pyobs-weather")
    def pyobs_index() -> dict[str, Any]:
        out = {}
        for name, fields in st.pyobs_weather.documents.items():
            code, body = pyobs_document(poller, name)
            out[name] = {
                "url": f"/api/v1/pyobs-weather/{name}",
                "fields": fields,
                "status": code,
                "error": body.get("error") if code != 200 else None,
            }
        return {"documents": out}

    @app.get("/api/v1/pyobs-weather/{name}")
    def pyobs_weather(name: str) -> JSONResponse:
        code, body = pyobs_document(poller, name)
        return JSONResponse(status_code=code, content=body)

    return app
