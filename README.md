# rotse-boltwood

Boltwood Cloud Sensor II **head** (no adaptor box) over RS-485 for ROTSE-IIIc: a small
service that keeps polling the head and serves the latest report as JSON (port 8081), plus a
CLI for checking and debugging.

**Status 2026-11-25:** head SN 04015 (firmware 67) runs on its site wiring (Cat cable, GX16-6
connector, box with a Mornsun 24 V supply and a Waveshare isolated USB-RS485 adapter), read from
a Mac. Service and CLI verified against the real head. Not deployed; no pyobs-weather station
set up yet.

Protocol, report fields, status codes and error messages are documented in the code
(`src/rotse_boltwood/protocol.py`) and in the Cloud Sensor II manual (see [Sources](#sources)).

---

## Wiring

### Head connector (manual V0029 §9.3 / §21.5)

5-pin rectangular connector at the head; the "1" for pin 1 is moulded next to a side slot, pin 5
has "2695" moulded over it. Colours are those of the original Boltwood cable.

| Pin | Boltwood wire | Signal |
|---|---|---|
| 1 | red | Data− |
| 2 | green | Data+ |
| 3 | yellow | +24 V |
| 4 | black | power return |
| 5 | bare | shield |

- **Never plug/unplug the head while powered.** Switch mains off first.
- Power: 24 V regulated, low-noise (the head wants 22–25 V). ~0.1 A normally, ~0.4 A with
  heaters, 0.63 A worst case.
- The wetness disc and the anemometer get hot (70 °C).

### Site wiring

```
head ── Boltwood cable stub ── splice ── Cat cable ── GX16-6 plug ══ GX16-6 panel socket ── box:
                                                       1,2 → Waveshare TB / TA
                                                       3,4 → PSU +Vo / −Vo
                                                       6   → Waveshare PE   (data reference)
                                                       5   → shield (earth, or open)
mains → 3.15 A slow-blow fuse → PSU AC(L) pin 3, AC(N) pin 2
```

| GX16 pin | Signal | Head pin | Cat wires (planned, T568B; TODO confirm at the splice) |
|---|---|---|---|
| 1 | Data− | 1 red | orange |
| 2 | Data+ | 2 green | white/orange |
| 3 | +24 V | 3 yellow | blue + white/blue |
| 4 | 0 V power return | 4 black | brown + white/brown |
| 5 | shield | 5 bare | drain / foil |
| 6 | data reference | 4 black | green + white/green |

Pins 4 and 6 join only at the head. **Pin 6 (→ Waveshare `PE`) must be connected**: without it,
or with pins 5 and 6 swapped, nothing is received at all. Only one Data+/Data− orientation works.

Box, as built (TODO: fill in):

| Box terminal | Wire colour in the box | From GX16 pin |
|---|---|---|
| Waveshare `TB` | yellow | TODO |
| Waveshare `TA` | TODO | TODO |
| Waveshare `PE` | white | 6 |
| PSU pin 4 (+Vo) | TODO | 3 |
| PSU pin 8 (−Vo) | TODO | 4 |
| earth / open | TODO | 5 (shield) |

**Power supply:** MORNSUN LD30-23B24R2A2S, 24 V 1.3 A, Class II (no earth terminal). Terminals:
1 NC, 2 AC(N), 3 AC(L) | 4 +Vo, 5–7 NC, 8 −Vo. Needs an external 3.15 A slow-blow fuse. Don't use
the NC terminals as tie points.

**Rules:**
- The Cat cable carries 24 V: never plug it into network gear.
- GX16 is not for live plugging and only splash-proof when mated (socket facing down, drip loop,
  cap it when open).
- Never earth the 24 V side (−Vo, Waveshare `PE`). Only mains earth, a metal box and the cable
  shield go to earth. For the permanent install earth the shield at the box and the mast to a
  ground rod (manual §13).

---

## Quick start

```sh
uv sync
uv run pytest                                   # tests, no hardware needed

uv run rotse-boltwood-cli ports                 # find the adapter (FTDI FT232R)
uv run rotse-boltwood-cli check /dev/cu.usbserial-XXXX     # one report; exit 0 if the head is ok
uv run rotse-boltwood --port /dev/cu.usbserial-XXXX        # service on http://localhost:8081
uv run rotse-boltwood --sim                     # same with a simulated head
uv run rotse-boltwood-cli status                # summary of the running service
open http://localhost:8081/docs                 # interactive API docs
```

On Linux use `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_<serial>-if00-port0`; the user needs
the `dialout` group. **Only one program can have the port open:** stop the service before
`check` or `monitor`.

## CLI: `rotse-boltwood-cli`

| Command | What |
|---|---|
| `ports` | list serial ports |
| `check PORT` | wait for one report and print it. Exit 0 = ok |
| `monitor PORT` | print every message live (add `--raw -v` for raw bytes); saves a capture in `captures/` |
| `monitor PORT --listen` | listen only, never transmit |
| `replay FILE.jsonl` | decode a saved capture |
| `status [URL]` | state of a running service (default `http://localhost:8081`) |

`--help` on each command lists all options.

## Troubleshooting

| Symptom | Check |
|---|---|
| `port: ...` | adapter plugged in? right device name? another program using the port? `dialout` group? |
| `no report yet` / nothing received | head powered? GX16 pin 6 to `PE`? Data+/Data− swapped? Run `monitor PORT --raw -v` |
| only `0x00` bytes every ~2 s | the head isn't being polled, or A/B swapped |
| junk bytes | wrong baud, A/B swapped or missing `PE` |
| `stale: last report ... ago` | head lost power or the cable |
| occasional `read error` on macOS | harmless FTDI driver glitch; a wiring problem only if constant |
| `sky sensor wet`, `anemometer heating up` | normal: rain/dew, or the first minute after power-on |

Power at the head is in every report (`power_voltage`), a good health check.

---

## HTTP API (:8081)

Read-only; `GET /docs` shows everything interactively.

| Endpoint | What |
|---|---|
| `GET /api/v1/report` | latest sensor report. Check `ok`; a value is `null` when the head flags it invalid, with the reason in `errors`/`notes` |
| `GET /api/v1/health` | link and service state; 503 when unhealthy (`error` says why) |
| `GET /api/v1/head` | serial, firmware, the head's own thresholds |
| `GET /api/v1/frames` | recent raw messages (debugging) |
| `GET /api/v1/pyobs-weather[/{name}]` | documents for pyobs-weather, see below |

Key values in a report: `sky_minus_ambient_c` (the cloud measurement: clear sky ≈ −20 °C or
colder, near 0 = overcast), `ambient_c`, `humidity_pct`, `dew_point_c`, `wind_kmh` (km/h),
`wet`, `rain`, `roof_close_requested` (the head's own decision).

## pyobs-weather

Each document is a separate pyobs-weather station of class `pyobs_weather.weather.stations.JSON`.
They are split so that a wet head (no sky or wind value) doesn't hide `rain`: an invalid or
stale document returns 503 and pyobs-weather skips it.

| Document | JSON |
|---|---|
| `rain` | `{"time": ..., "rain": 0.0}` (1.0 if the rain **or** wet sensor fires) |
| `sky` | `{"time": ..., "skytemp": -24.8}` (sky − ambient, °C) |
| `ambient` | `{"time": ..., "temp": 18.0, "humid": 45.0, "dewpoint": 5.9}` |
| `wind` | `{"time": ..., "windspeed": 7.7}` (km/h) |

Station kwargs (Admin → Stations). Give `name` and `unit` exactly as below (pyobs-weather 2.2.5
crashes with only `code`); station code and plot colour max 20 characters. Replace `<host>`.

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

**Attach evaluators** (`valid` on every station, rain = 0 on `bw_rain`) before trusting these for
the roof: otherwise pyobs-weather keeps showing the last values as GOOD when a station stops
updating. Cloud and wind limits are still to be decided.

---

## Configuration

Defaults < YAML (`--config`, default `./config.yaml`) < environment (`BOLTWOOD_<KEY>`). See
`config.example.yaml`.

| Key | Default | Meaning |
|---|---|---|
| `port` | `/dev/ttyUSB0` | serial device (`--port` overrides) |
| `max_age_s` | 30 | a report older than this is stale |
| `api.host`, `api.port` | `0.0.0.0`, 8081 | HTTP (`--host`, `--http-port` override) |
| `pyobs_weather.documents` | rain / sky / ambient / wind | how values are split into documents |

Other keys (`station`, `baud`, `ack`, `repoll_s`, `reconnect_s`, `history`, `sim.*`) rarely need
changing; see `src/rotse_boltwood/config.py`.

## Install on the server (not done yet)

Target: `ocs-server` (Ubuntu 24.04, has Python 3.12 and `uv`). The repo is a git clone in
`/opt/rotse-boltwood`, owned by the admin user so you can `git pull`; the service runs as its
own user `boltwood`, which only reads it. Config lives outside the repo in `/etc/rotse-boltwood/`.

**1. Clone and build the venv** (as the admin user):

```sh
sudo mkdir /opt/rotse-boltwood
sudo chown "$USER": /opt/rotse-boltwood
git clone https://github.com/ROTSEIIIc/rotse-boltwood.git /opt/rotse-boltwood
cd /opt/rotse-boltwood
UV_PYTHON_PREFERENCE=only-system uv sync --frozen --no-dev    # creates .venv/
```

`only-system` makes the venv use Ubuntu's `/usr/bin/python3.12`. A uv-downloaded Python would live
in your home directory, which the service user can't see.

**2. Service user and serial port:**

```sh
sudo useradd --system --no-create-home --shell /usr/sbin/nologin --groups dialout boltwood
ls /dev/serial/by-id/          # note the usb-FTDI_FT232R_USB_UART_...-if00-port0 name
```

**3. Config:**

```sh
sudo mkdir /etc/rotse-boltwood
sudo cp config.example.yaml /etc/rotse-boltwood/config.yaml
sudo nano /etc/rotse-boltwood/config.yaml     # set port: /dev/serial/by-id/usb-FTDI_...
```

**4. Test by hand, then install the service:**

```sh
.venv/bin/rotse-boltwood-cli check /dev/serial/by-id/usb-FTDI_...    # exit 0 = head ok

sudo cp deploy/rotse-boltwood.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rotse-boltwood
journalctl -u rotse-boltwood -f
.venv/bin/rotse-boltwood-cli status            # or: curl localhost:8081/api/v1/health
```

### Update after changes

```sh
cd /opt/rotse-boltwood
git pull
UV_PYTHON_PREFERENCE=only-system uv sync --frozen --no-dev
sudo systemctl restart rotse-boltwood
.venv/bin/rotse-boltwood-cli status
```

If `deploy/rotse-boltwood.service` changed, copy it again and run `sudo systemctl daemon-reload`
before the restart. If `config.example.yaml` gained keys, compare it with
`/etc/rotse-boltwood/config.yaml` (your config is never overwritten by a pull). Don't edit files in
`/opt/rotse-boltwood` on the server: make changes in the repo, push, then pull.

To roll back: `git checkout <older commit or tag>`, `uv sync` as above, restart.

## Sources

- Cloud Sensor II User's Manual V0029 (Diffraction Limited): units, codes, the report layout.
- [eddyem/boltwood](https://github.com/eddyem/boltwood) and [thusser/boltwood](https://github.com/thusser/boltwood):
  framing and poll loop (thusser's wind conversion is wrong: the head already sends km/h).
- Our captures from SN 04015 in `tests/captures/`.
