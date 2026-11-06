# rotse-boltwood

Boltwood Cloud Sensor II **head** (no adaptor box) over RS-485 for ROTSE-IIIc: a small
service that keeps talking to the head and serves the latest report as JSON, plus a CLI for
checking and debugging. Same pattern as rotse-weather-mast (asyncio + FastAPI, latest value
in memory, `--sim`, pydantic-settings, systemd unit).

**Status 2026-11-06:** head SN 04015 (firmware 67) running on the bench, 24 V bench supply,
Waveshare isolated USB-RS485 adapter on a Mac. Protocol, service and CLI verified against the
real head, including the pyobs-weather documents. Not deployed; no pyobs-weather station set up yet.
Hardware notes and the bring-up story: [`docs/hardware-handoff.md`](docs/hardware-handoff.md).

Contents: [Wiring](#wiring-bench) · [Quick start](#quick-start) · [CLI](#debug-cli-rotse-boltwood-cli) ·
[HTTP API](#http-api-8081) · [pyobs-weather](#pyobs-weather) · [Protocol](#protocol) · [Report fields](#d-sensor-report-fields) ·
[Status codes](#status-codes) · [Errors and messages](#errors-notes-and-messages) ·
[Configuration](#configuration) · [Layout](#layout) · [Sources](#sources)

---

## Wiring (bench)

| Head pin (identify by continuity, see docs) | Goes to |
|---|---|
| +24 V | PSU + (24.0 V, current limit ~0.7 A) |
| Power return (GND) | PSU − **and** adapter `PE` (the adapter is isolated: `PE` is its only ground reference) |
| Data + / Data − | adapter `TA` / `TB`. Only one orientation works; swapped = nothing received at all. TODO: note which wire is on `TA` |
| Shield | open on the bench |

Power: 24 V regulated, ~0.05–0.12 A normally, ~0.4 A with the heaters on, 0.63 A worst case.
Never plug/unplug the head while powered. The wetness disc and the anemometer reach 70 °C.

## Quick start

```sh
uv sync
uv run pytest                                   # 54 tests, ~4 s, no hardware needed

uv run rotse-boltwood-cli ports                 # find the adapter (FTDI FT232R)
uv run rotse-boltwood-cli check /dev/cu.usbserial-XXXX     # one report; exit 0 if the head is ok
uv run rotse-boltwood --port /dev/cu.usbserial-XXXX        # service on http://localhost:8081
uv run rotse-boltwood --sim                     # same with a simulated head
uv run rotse-boltwood-cli status                # summary of the running service
open http://localhost:8081/docs                 # interactive API docs
```

On Linux use `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_<serial>-if00-port0`; the user needs
the `dialout` group. Only one program can have the port open: stop the service before `check`
or `monitor`.

---

## Debug CLI: `rotse-boltwood-cli`

| Command | What |
|---|---|
| `ports` | list serial ports |
| `check PORT [--timeout 20] [--json]` | poll until one D report, print it (and serial/firmware if an I/T frame came). Exit 0 = report with no errors, 1 = no report or head errors |
| `monitor PORT` | poll and print every decoded frame live; raw traffic saved to `captures/boltwood-<UTC>.jsonl` |
| `monitor PORT --listen` | passive, never transmits. An unpolled head shows a lone `0x00` every ~2.1 s |
| `monitor PORT --raw -v` | also raw bytes in both directions (`->` sent, `<-` received) and P/A/N frames |
| `monitor ... --duration 60 --log FILE --no-log --repoll 2 --ack A` | stop after N s / capture file / no capture / re-poll interval / ACK char |
| `replay FILE.jsonl [-v]` | decode a capture offline |
| `status [URL]` | health, head info and latest report from a running service (default `http://localhost:8081`). Exit 0 if healthy |

`PORT` is a serial device, a pyserial URL (`socket://host:port`, `loop://`) or `sim`.

`monitor` output, one line per frame (`D` lines shortened here):

```
    5.71s  I  serial 04015  firmware 67  head reset
    6.13s  T  serial 04015  firmware 67  thresholds: cloudy -25/-10 C, windy 15/30 km/h
    6.43s  K  wetness calibration: 1 0.939 12412 30.6 22.3 13215 -32768  17360
    6.74s  W  wetness data: 0.0  0.0     0     0     0 11972 32767
    7.05s  C  thermopile calibration: 1 -4.000e-07 0.320  0.00 10.1
    7.46s  D  sky-amb -0.6 C (very cloudy)  amb 25.3 C  rh 50 %  dew 14.0 C  wind -- km/h (unknown)
              wet N rain N (dry)  roof-close 1  24.4 V  very light  (anemometer heating up)
```

Captures use one JSON object per line: `{"t": ISO time, "mono": seconds since start,
"dir": "rx"|"tx", "hex": bytes}` plus `"event"` lines (`start` with port/baud/mode, `stop`,
`read_error`). Keep useful ones in `tests/captures/` (committed); everything else under
`captures/` is git-ignored.

---

## HTTP API (:8081)

Read-only. `GET /docs` is the interactive (Swagger) version.

| Endpoint | What | Status |
|---|---|---|
| `GET /` | endpoint list | 200 |
| `GET /api/v1/report` | latest D report: values, conditions, errors, notes, all 35 raw fields | always 200; check `ok` |
| `GET /api/v1/health` | serial link, loop, head serial/firmware, counters | 200 healthy, **503** otherwise (`error` says why) |
| `GET /api/v1/head` | serial, firmware, thresholds, reset info, latest I/T/C/K/W frame texts | 200 |
| `GET /api/v1/frames` | recent messages, newest last. `?limit=50` (default), `?type=D` (only that frame type), `?all=1` (include P/A/N/empty) | 200 |
| `GET /api/v1/codes` | the status code tables below, as JSON | 200 |
| `GET /api/v1/pyobs-weather` | the configured pyobs-weather documents, each with its current status and error | 200 |
| `GET /api/v1/pyobs-weather/{name}` | one flat document for a pyobs-weather `JSON` station, see [pyobs-weather](#pyobs-weather) | 200, **503** when not valid, 404 unknown name |

### `/api/v1/report`

```json
{
  "station": "boltwood",
  "time": "2026-11-06T14:16:14Z",
  "age_s": 0.6,
  "ok": true,
  "error": null,
  "values": {
    "sky_minus_ambient_c": -0.7, "ambient_c": 25.6, "humidity_pct": 48, "dew_point_c": 13.7,
    "wind_kmh": 0.0, "wet": false, "rain": false, "roof_close_requested": true,
    "case_c": 31.3, "power_voltage": 24.4
  },
  "conditions": {
    "cloud": "very cloudy", "wind": "calm", "rain": "dry", "day": "very light",
    "wet_sensor": "dry", "rain_sensor": "no rain", "rain_heater": "too cold"
  },
  "errors": [],
  "notes": [],
  "raw": { "humidstat_temp_code": 0, "cloud_cond": 3, "...": "all 35 fields as sent" }
}
```

- `time` is when the report arrived (UTC), `age_s` how old it is.
- `ok` = a report younger than `max_age_s` (30 s) **and** no `errors`.
- `error` = the first reason `ok` is false (see [messages](#service-and-link-messages)), else null.
- A value in `values` is **null** when the head flags it invalid (sky sensor wet, anemometer
  warming up, sensor fault, ...); the reason is in `errors` or `notes`. `raw` always has
  the number exactly as sent.
- `wet` = wet sensor `W` or `w`; `rain` = rain sensor `R` or `r`. Kept separate on purpose.
- `conditions` are the head's own judgements against its thresholds (see `/api/v1/head`).
  `roof_close_requested` is the head's decision too.

### `/api/v1/health`

```json
{
  "station": "boltwood", "version": "0.1.0", "mode": "hardware", "ok": true, "error": null,
  "host": "epa-mbp.local", "uptime_s": 10,
  "port": {"device": "/dev/cu.usbserial-BH00RNEW", "baud": 4800, "connected": true, "error": null, "opens": 1},
  "head": {"serial": 4015, "firmware": 67},
  "last_message_age_s": 0.6, "report_age_s": 0.6, "last_error": null, "bytes_rx": 774,
  "counts": {"D": 4, "P": 16, "repoll": 1}
}
```

`ok` = the serial loop is alive and the report is fresh. `head` is null until an I or T frame
arrives (T comes at startup and about every 100 s). `counts` keys: one per frame type (`D`, `T`, `I`, `K`,
`W`, `C`, ...), `P`/`A`/`N` (poll/ack/nack from the head), `repoll` (timer re-polls we sent),
`empty`, `junk`, `crc_bad`, `parse_error`, `overflow`, `head_resets`, `read_glitch` (macOS FTDI
read glitches that were tolerated, see below). In normal operation only frame types, `P` and
the odd `repoll` grow.

### `/api/v1/head`, `/api/v1/frames`

`/api/v1/head` gives `serial`, `firmware`, `thresholds` (fields of the [T frame](#t-thresholds)),
`reset` (the [I frame](#i-reset)) and `frames`: the latest text of each non-D frame type with
its time. `/api/v1/frames` entries:

```json
{"time": "2026-11-06T14:05:47Z", "kind": "M", "type": "D", "name": "sensor report",
 "text": "0 3 0 1 3 1 -0.6 25.3 ...", "crc_ok": true, "error": null,
 "raw_hex": null, "reply_hex": "02610a01"}
```

`kind` is `M` (data frame), `P`, `A`, `N`, `empty`, or `junk` (unparseable; `raw_hex` has the
bytes). `reply_hex` is what we sent back. `error` is set on a bad CRC or a frame that didn't parse.

---

## pyobs-weather

Same approach as rotse-weather-mast: each document is a **separate pyobs-weather station** using
pyobs-weather's built-in `JSON` station class, pointed at `/api/v1/pyobs-weather/{name}`.

### Documents (defaults)

| Document | URL | JSON | From |
|---|---|---|---|
| `rain` | `/api/v1/pyobs-weather/rain` | `{"time": ..., "rain": 0.0}` | 1.0 if the rain sensor **or** the wet sensor fires (`rain_or_wet`) |
| `sky` | `/api/v1/pyobs-weather/sky` | `{"time": ..., "skytemp": -24.8}` | sky − ambient, °C |
| `ambient` | `/api/v1/pyobs-weather/ambient` | `{"time": ..., "temp": 18.0, "humid": 45.0, "dewpoint": 5.9}` | ambient °C, RH %, dew point °C |
| `wind` | `/api/v1/pyobs-weather/wind` | `{"time": ..., "windspeed": 7.7}` | km/h (the head's unit, and pyobs-weather's) |

The JSON keys are pyobs-weather's built-in sensor type codes (`SENSOR_TYPES` in
`pyobs_weather/weather/stations/station.py`, v2.2.5).

**Why four documents and not one:** pyobs-weather's `JSON` station reads every configured key
(a missing one crashes the update), so a document has to be either complete or 503. A wet head
reports sky −998 and wind −2, and wind is −1 for the first minute after power-on. In a single
document that would take `rain` down exactly when it's raining. Split up, only the affected
document goes 503:

| Situation | rain | sky | ambient | wind |
|---|---|---|---|---|
| normal | 200 | 200 | 200 | 200 |
| head wet (rain, dew) | **200, rain 1.0** | 503 `sky sensor wet` | 200 | 503 `anemometer wet` |
| first minute after power-on | 200 | 200 | 200 | 503 `anemometer heating up` |
| humidity sensor fault | 200 | 200 | 503 | 200 |
| report stale / port down | 503 | 503 | 503 | 503 |

### Rules (pyobs-weather 2.2.5 `JSON` station, read from its source)

- Flat document, every configured key always present when 200.
- `time` = when the head's report arrived (UTC ISO 8601), not the time of the request.
- Values are always floats (InfluxDB rejects a field whose type changes); booleans are 0.0 / 1.0.
- 503 instead of stale or invalid data: pyobs-weather skips the update on any non-200.
  Body: `{"ok": false, "error": "..."}`. Messages:

| 503 error | Meaning |
|---|---|
| `no report yet` / `stale: last report <n> s ago (max_age_s 30)` / `port: ...` | same as [service messages](#service-and-link-messages) |
| `<field>, <field> not valid (<reasons>)` | the head flagged a value in this document invalid; reasons are the report's errors and notes, e.g. `sky_minus_ambient_c not valid (sky sensor wet)` |

`GET /api/v1/pyobs-weather` lists every document with its status, and `rotse-boltwood-cli status`
prints them.

### Setting up the stations in pyobs-weather

One station per document (Admin → Stations, or in a script like rotse-weather-mast's
`deploy/pyobs-weather-test/init.sh`). Class `pyobs_weather.weather.stations.JSON`, kwargs as
below. Give `name` and `unit` exactly as in `SENSOR_TYPES`: with only `code`, v2.2.5's
`JSON.create_sensors()` crashes. Station `code` and plot `color` are max 20 characters
(`rgba(0,120,200,0.6)`, no spaces). Replace `<host>` with the machine running this service.

```json
// station "bw_rain"
{"url": "http://<host>:8081/api/v1/pyobs-weather/rain",
 "fields": {"rain": {"code": "rain", "name": "Raining", "unit": ""}}}

// station "bw_sky"
{"url": "http://<host>:8081/api/v1/pyobs-weather/sky",
 "fields": {"skytemp": {"code": "skytemp", "name": "Rel sky temperature", "unit": "°C"}}}

// station "bw_ambient"
{"url": "http://<host>:8081/api/v1/pyobs-weather/ambient",
 "fields": {"temp":     {"code": "temp",     "name": "Temperature",       "unit": "°C"},
            "humid":    {"code": "humid",    "name": "Relative humidity", "unit": "%"},
            "dewpoint": {"code": "dewpoint", "name": "Dew point",         "unit": "°"}}}

// station "bw_wind"
{"url": "http://<host>:8081/api/v1/pyobs-weather/wind",
 "fields": {"windspeed": {"code": "windspeed", "name": "Wind speed", "unit": "km/h"}}}
```

**Attach evaluators.** The weather mast showed that when a station stops updating (503),
pyobs-weather keeps showing its last values as GOOD unless an evaluator checks the age. Give
every station a `valid` evaluator, and the rain station a "rain must be 0" check, before trusting
these for the roof. The limits (cloudy sky threshold, wind limit) are ROTSE decisions, still TBD.

To change the split, set `pyobs_weather.documents` in the config (document name → {report value →
JSON key}). Report values: `sky_minus_ambient_c`, `ambient_c`, `humidity_pct`, `dew_point_c`,
`wind_kmh`, `rain`, `wet`, `rain_or_wet`.

---

## Protocol

### Physical layer

2-wire half-duplex RS-485, **4800 baud, 8 data bits, no parity, 1 stop bit**, no flow control.
The head is the only other node; the host side (our adapter) should have the termination.

### Messages

Every message from the head ends in `\n` (`0x0A`):

| Message | Bytes | Meaning |
|---|---|---|
| data frame | `0x02 'M' <type> <text> <CRC> \n` | a report; `<type>` is one letter, `<text>` space-separated fields, `<CRC>` 4 hex digits |
| poll request | `0x00 0x02 'P' \n` (the `0x00` only before the first of a burst) | head asks to be polled |
| ack / nack | `0x02 'A' \n` / `0x02 'N' \n` | answer to a host command (we don't send commands) |
| lone zero | `0x00` | what an **unpolled** head sends, every ~2.1 s. No `\n`, so never a complete message |

**CRC:** CRC-16/XMODEM (polynomial 0x1021, initial value 0, no reflection) over `<type><text>`,
i.e. everything after the `M` up to the CRC; written as 4 uppercase hex digits.

From the host:

| Host sends | Bytes | When |
|---|---|---|
| poll | `0x01` | on opening the port, and whenever no complete message arrived for `repoll_s` (2 s) |
| ack + poll | `0x02 'a' \n 0x01` | after every good data frame and every P frame |
| nack + poll | `0x02 'n' \n 0x01` | after a data frame with a bad CRC |

(thusser/boltwood sends uppercase `'A'`; lowercase `'a'` is what eddyem uses and what our head
accepts. Host commands `0x02 'm' <cmd> <CRC> \n` exist (`b` close roof, `c` get calibration,
`l` get wetness calibration, `t` set thresholds) but **we never send any**: `t` without
arguments resets the thresholds to defaults.)

### Conversation (as seen on SN 04015)

```
host: 01                                   (poll)
head: 02 'MI ...' CRC 0A                   reset info, once after power-up
host: 02 'a' 0A 01                         (ack + poll)
head: 'MT ...'  'MK ...'  'MW ...'  'MC ...'   thresholds, calibrations (each acked)
head: 'MD ...'                             sensor report
head: 00 02 'P' 0A  x5-6, ~0.2 s apart     poll requests (each answered with ack + poll)
head: 'MD ...'                             next report ~2 s after the previous
...                                        T, K, W, C again about every 100 s
```

Unpolled, the head sends **no frames**, only a lone `0x00` every ~2.1 s (on a scope: a ~30 ms
burst with the pair driven to a steady level). It looks like a dead transmitter but isn't.

### Frame types

| Type | Name | When | Parsed |
|---|---|---|---|
| `D` | sensor report | every ~2 s while polled | fully, [below](#d-sensor-report-fields) |
| `T` | thresholds, serial, firmware | at startup, then ~every 100 s (followed by K, W, C) | [below](#t-thresholds) |
| `I` | reset | once after power-up | [below](#i-reset) |
| `C` | thermopile calibration | with T | text only: `cal bestK bestD offset ?` (e.g. `1 -4.000e-07 0.320 0.00 10.1`) |
| `K` | wetness calibration | with T | text only: `cal oscFactor rawWetAvg caseT ambientT nomOsc oscDry minWetAvg` |
| `W` | wetness data | with T | text only: `caseT ambT wAvgW wAvgC nomOsc rawWT wetAvg` (meanings uncertain) |
| `Q`, `R` | echo of host command, roof close | not seen | text only |

### D: sensor report fields

35 space-separated fields, in this order. Column `[M]` is the heading in the manual's
Appendix A. **Temperatures °C, speeds km/h** (manual: *"All temperatures are Celsius and
speeds are km/hr"*; our head's windy thresholds 15/30 are the manual's km/h defaults).

| # | API name | [M] | Type | Meaning |
|---|---|---|---|---|
| 1 | `humidstat_temp_code` | E | int | humidity/ambient sensor error code ([codes](#status-codes)) |
| 2 | `cloud_cond` | C | int | cloud condition |
| 3 | `wind_cond` | W | int | wind condition |
| 4 | `rain_cond` | R | int | rain condition |
| 5 | `sky_cond` | 1 | int | sky condition for old Cloud Sensor I programs |
| 6 | `roof_close_requested` | c | int | 1 if the head requests roof close this cycle |
| 7 | `sky_minus_ambient_c` | SKY | float | **the cloud measurement**: sky minus ambient temperature. Clear sky ≈ −20 °C or lower; near 0 = overcast (or indoors). Sentinels: 999.9 / −999.9 / −998.0 |
| 8 | `ambient_c` | AMB | float | ambient temperature |
| 9 | `wind_kmh` | WIND | float | wind speed, km/h; negative = anemometer status code |
| 10 | `wet_sensor` | w | char | `N` dry, `W` wet now, `w` wet in last minute |
| 11 | `rain_sensor` | r | char | `N` no rain, `R` drops this cycle, `r` drops in last minute |
| 12 | `humidity_pct` | HUM | int | relative humidity, % |
| 13 | `dew_point_c` | DEW | float | dew point |
| 14 | `case_c` | CASE | float | thermopile case temperature; 999.9 / −99.9 = saturated |
| 15 | `rain_heater_pct` | HEA | int | rain (wetness) heater power, % |
| 16 | `black_body_c` | BLKT | float | calibration black body (factory only; −99.9 normally) |
| 17 | `rain_heater_state` | H | int | rain heater control state |
| 18 | `power_voltage` | PWR | float | the +24 V line measured at the head. Good health check |
| 19 | `anemometer_temp_diff_c` | WNDTD | float | anemometer tip minus ambient; heater power is reduced at 25 °C |
| 20 | `wetness_drop` | WDROP | int | max drop in wetness oscillator counts this cycle (rain drops) |
| 21 | `wetness_avg` | WAVG | int | wetness oscillator count difference from base dry value |
| 22 | `wetness_dry` | WDRY | int | same, for current dry value |
| 23 | `rain_heater_pwm` | RHT | int | rain heater PWM |
| 24 | `anemometer_heater_pwm` | AHT | int | anemometer heater PWM |
| 25 | `thermopile_adc` | ASKY | int | thermopile raw A/D (0 on digital-thermopile heads like ours) |
| 26 | `thermistor_adc` | ACSE | int | thermopile thermistor raw A/D (0 on digital heads) |
| 27 | `power_adc` | APSV | int | supply voltage raw A/D |
| 28 | `block_adc` | ABLK | int | calibration block thermistor raw A/D |
| 29 | `anemometer_thermistor_adc` | AWND | int | anemometer tip thermistor raw A/D |
| 30 | `davis_vane_adc` | AVNE | int | factory only |
| 31 | `dk_mph` | DKMPH | float | external anemometer, factory only |
| 32 | `ext_anemometer_direction` | VNE | int | factory only |
| 33 | `raw_wetness_osc` | RWOSC | int | raw wetness oscillator counts |
| 34 | `day_cond` | D | int | daylight condition |
| 35 | `daylight_adc` | ADAY | int | daylight photodiode raw A/D, 0 dark … 1023 brightest |

Real example (indoors, daytime, just powered on):
`0 3 0 1 3 1 -0.6 25.3 -1.0 N N 50 14.0 30.1 70 -99.9 4 24.4 19.3 1 464 417 074 044 0000 0000 0958 1023 0186 0190 0.0 066 11972 3 0609`

The anemometer is a heated gold finger: wind speed is estimated from how much the wind cools
it. No moving parts; slow, so it misses gusts; most accurate at low speeds; calm air can read
a small positive speed (manual §4.5).

### T: thresholds

17 fields: `serial firmware send_errors cloudy_c very_cloudy_c windy_kmh very_windy_kmh rain wet
daylight_code day_threshold very_day_threshold` + 5 not understood. Ours:
`04015 00067 00000 -25.0 -10.0 15.0 30.0 12 100 1 132 220 5 47360 49257 0 1`.
The head compares its readings against these for `cloud_cond`, `wind_cond`, `rain_cond`,
`day_cond` and `roof_close_requested`. Defaults (manual): cloudy −25 °C, very cloudy −10 °C,
windy 15 km/h, very windy 30 km/h.

### I: reset

6 fields: `serial firmware` + 4 not understood (`0x00 000 0x93 00000` on ours).

---

## Status codes

All from the manual (section 20 enums, Appendix A) unless marked [E] (eddyem only).
`GET /api/v1/codes` returns the same tables.

| Field | 0 | 1 | 2 | 3 | other |
|---|---|---|---|---|---|
| `cloud_cond` | unknown | clear | cloudy | very cloudy | |
| `wind_cond` | unknown | calm | windy | very windy | |
| `rain_cond` | unknown | dry | wet (water on sensor) | rain (falling drops) | |
| `day_cond` | unknown | dark | light | very light | |
| `sky_cond` [E] | unknown | clear | cloudy | very cloudy | 4 wet |

`humidstat_temp_code` (firmware ≥ 54): 0 ok, 1 humidity write failure, 2 humidity measurement
never finished, 3 ambient write failure, 4 ambient measurement never finished, 5 humidity data
line not high, 6 ambient data line not high.

`rain_heater_state` [E, unverified; thusser disagrees about 0/1]: 0 too hot, 1 at target,
2–5 too cold, 6 cannot control because the case temperature is saturated (shutdown),
7 normal control.

Sentinel values:

| Field | Value | Meaning | Treated as |
|---|---|---|---|
| `sky_minus_ambient_c` | 999.9 | saturated hot | error |
| | −999.9 | saturated cold | error |
| | −998.0 | sensor wet, sky temperature unknown (thusser: −998.9; both accepted) | note |
| `wind_kmh` | −1 | still heating up (first ~minute after power-on) | note |
| | −2 | wet | note |
| | −3 | A/D from probe bad (firmware < 56 only; unused since) | error |
| | −4 | probe not heating (failure) | error |
| | −5 | A/D low: probe shorted (firmware ≥ 56) | error |
| | −6 | A/D high: no probe plugged in (firmware ≥ 56) | error |
| `ambient_c` + `humidity_pct` | −40 and 0 together | no comms with that sensor, likely water ingress [E] | error |
| `case_c` | 999.9 / −99.9 | saturated ("should never occur") | error |

---

## Errors, notes and messages

### In a report (`errors` / `notes`)

`errors` make `ok` false; `notes` are expected states and don't. The affected value is null
in `values` either way.

| Message | List | Cause | Values nulled |
|---|---|---|---|
| `sky temperature saturated hot` | errors | SKY = 999.9 | sky |
| `sky temperature saturated cold` | errors | SKY = −999.9 | sky |
| `sky sensor wet` | notes | SKY = −998.x: water on the sensor; it's raining or dewed up | sky |
| `anemometer heating up` | notes | WIND = −1, first minute after power-on | wind |
| `anemometer wet` | notes | WIND = −2 | wind |
| `anemometer A/D bad (firmware < 56 only)` | errors | WIND = −3 | wind |
| `anemometer not heating` | errors | WIND = −4 | wind |
| `anemometer shorted (A/D low)` | errors | WIND = −5 | wind |
| `no anemometer probe (A/D high)` | errors | WIND = −6: anemometer cable unplugged | wind |
| `anemometer code <n>` | errors | any other negative WIND | wind |
| `no comms with ambient/humidity sensor (water ingress?)` | errors | AMB = −40 and HUM = 0 | ambient, humidity, dew point |
| `humidity/ambient sensor: <text>` | errors | `humidstat_temp_code` ≠ 0 (text from the table above) | ambient, humidity, dew point |
| `thermopile case temperature saturated` | errors | CASE = 999.9 or ≤ −99.9 | case |
| `rain heater: cannot control: case temperature saturated (shutdown)` | errors | `rain_heater_state` = 6 | — |
| `unknown wet_sensor value 'X'` / `unknown rain_sensor value 'X'` | errors | a letter other than N/W/w, N/R/r | — |

### Service and link messages

`error` in `/api/v1/report` and `/api/v1/health`, first match wins:

| Message | Meaning | What to check |
|---|---|---|
| `port: <exception>` | the serial port can't be opened or failed (e.g. `SerialException: could not open port ...: No such file or directory`) | adapter plugged in? right device name? another program using it? `dialout` group? |
| `no report yet` | port open but no D frame since start | head powered? wiring/`PE`/polarity? `rotse-boltwood-cli monitor PORT --raw` |
| `stale: last report <n> s ago (max_age_s 30)` | reports stopped | head lost power or the cable; `last_message_age_s` and `counts` in health |
| (a report error from the table above) | head reports a fault | see the table |
| `serial loop not running` | health only: loop thread dead or hung | service log; systemd restarts it via the watchdog |

`last_error` in health (most recent link problem, sticks until the next one):

| Message | Meaning |
|---|---|
| `no frame start: b'...'` | a line that doesn't start with `0x02`: wrong baud, A/B swapped, missing `PE`, noise (counted as `junk`) |
| `unknown frame kind 'X': ...` | `0x02` followed by something other than M/P/A/N (counted as `junk`) |
| `empty M frame: ...` | `0x02 'M' \n` (counted as `junk`) |
| `bad CRC on <type> frame` | checksum mismatch; we NACK and the previous report stays (counted as `crc_bad`) |
| `D frame has <n> fields, expected 35: ...` | truncated or garbled D (counted as `parse_error`) |
| `D field <name>=<value>: ...` | a field that isn't a number (counted as `parse_error`) |
| `T frame has <n> fields, expected >= 12: ...`, `T frame: ...`, `I frame ...` | same for T/I |

### Service log (journal / stderr)

| Line | Meaning |
|---|---|
| `starting station=... mode=hardware port=... config=... (loaded \| not found, using defaults)` | startup |
| `port <device> open` | port (re)opened; the session sends a poll |
| `port <device>: <exception>` (WARNING) | open/read/write failed; retried every `reconnect_s` (5 s). Logged once per distinct error |
| `port <device>: read glitch: ...` (DEBUG) | macOS FTDI driver reported a line glitch as `device reports readiness to read but returned no data`. Up to 5 in a row are ignored (counted as `read_glitch`); more in a row = unplugged, the port is reopened |

### CLI messages

| Message | Meaning |
|---|---|
| `JUNK <bytes> (...; wrong baud, A/B swapped or no PE?)` | unparseable line |
| `<type> CRC BAD <text>` / `<type> PARSE ERROR ...` | as above |
| `read error: ...` | macOS FTDI driver reports a line break condition as a read error; harmless once, wiring problem if constant |
| `nothing received: PSU on? PE wired to head GND? right port? ...` | summary when zero bytes came in |
| `bytes but no reports: only 0x00s means not polled / A-B swapped; junk means wrong baud` | summary when bytes came but no D frame |
| `FAIL: no report within <n> s (<bytes> bytes received)` | `check` gave up (exit 1) |
| `FAIL: head reports errors` | `check` got a report with `errors` (exit 1) |
| `cannot reach <url>: ...` | `status`: service not running (exit 2) |

---

## Configuration

Defaults < YAML (`--config`, default `./config.yaml`) < environment (`BOLTWOOD_<KEY>`,
`BOLTWOOD_API__PORT`). Unknown keys are rejected. See `config.example.yaml`.

| Key | Default | Meaning |
|---|---|---|
| `station` | `boltwood` | name in the JSON |
| `port` | `/dev/ttyUSB0` | serial device or pyserial URL (`--port` overrides) |
| `baud` | 4800 | fixed by the head |
| `ack` | `a` | ACK character (`a` or `A`) |
| `repoll_s` | 2.0 | re-poll after this long without a complete message |
| `reconnect_s` | 5.0 | wait before reopening the port after an error |
| `max_age_s` | 30 | report age limit for `ok` (must be ≥ 3 × `repoll_s`) |
| `history` | 200 | messages kept for `/api/v1/frames` |
| `api.host`, `api.port` | `0.0.0.0`, 8081 | HTTP (`--host`, `--http-port` override) |
| `pyobs_weather.documents` | rain / sky / ambient / wind, see [pyobs-weather](#pyobs-weather) | document name → {report value → JSON key} |
| `sim.seed`, `sim.report_interval_s`, `sim.sky_c` | 0, 2.0, −25 | `--sim` only |

## Layout

```
src/rotse_boltwood/
  protocol.py   framing, CRC, D/T/I parsers, code tables, sentinels (pure)
  session.py    host side of the conversation: poll / ACK / re-poll state machine (pure)
  sim.py        simulated head (pyserial-like), mimics the real captures
  poller.py     serial loop in a thread, reconnects, latest report in memory
  api.py, config.py, main.py, sdnotify.py   the service
  cli.py        rotse-boltwood-cli
tests/captures/ real traffic from SN 04015 (polled, unpolled)
deploy/         systemd unit (not deployed yet)
docs/           hardware investigation notes
```

Deploy (not done yet): target `ocs-server`. Venv in `/opt/rotse-boltwood`, config in
`/etc/rotse-boltwood/config.yaml` with `port` = the `/dev/serial/by-id/...` name, then
`deploy/rotse-boltwood.service` (adjust user/paths).

## Sources

- **[M]** Cloud Sensor II User's Manual V0029 (Diffraction Limited): §4.5 anemometer, §17 data
  file, §20 COM enums, Appendix A "D" record. Units, codes and sentinels come from here.
- **[E]** [eddyem/boltwood](https://github.com/eddyem/boltwood) `Protocol.md`, `term.c`: framing,
  CRC, host commands, T/I/K/W/C layouts.
- [thusser/boltwood](https://github.com/thusser/boltwood): poll/ACK loop. Its wind conversion
  (÷ 1.609) is wrong: the head already sends km/h.
- Our own captures from SN 04015 (`tests/captures/`). What's confirmed on the head is listed
  at the top of `src/rotse_boltwood/protocol.py`.
