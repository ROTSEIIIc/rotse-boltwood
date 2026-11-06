"""Configuration: defaults < YAML file < environment variables (same scheme as rotse-weather-mast).

Environment overrides use the prefix ``BOLTWOOD_`` and ``__`` for nesting, e.g.
``BOLTWOOD_PORT=/dev/ttyUSB1`` or ``BOLTWOOD_API__PORT=9000``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiConfig(_Section):
    host: str = "0.0.0.0"
    port: int = Field(8081, ge=1, le=65535)


# Report values a pyobs-weather document can carry (see api.PYOBS_VALUES).
PyobsField = Literal[
    "sky_minus_ambient_c", "ambient_c", "humidity_pct", "dew_point_c", "wind_kmh", "rain", "wet", "rain_or_wet"
]

# Document name -> {report value -> JSON key}. Keys are pyobs-weather 2.2.5 SENSOR_TYPES codes
# (weather/stations/station.py). One document = one pyobs-weather station, so an invalid value
# only takes its own document down: a wet head reports sky -998 and wind -2, and wind is -1
# for the first minute after power-on, while rain/wet are always valid.
DEFAULT_DOCUMENTS: dict[str, dict[str, str]] = {
    "rain": {"rain_or_wet": "rain"},
    "sky": {"sky_minus_ambient_c": "skytemp"},
    "ambient": {"ambient_c": "temp", "humidity_pct": "humid", "dew_point_c": "dewpoint"},
    "wind": {"wind_kmh": "windspeed"},  # km/h on the wire (manual Appendix A) = pyobs-weather's unit
}


class PyobsWeatherConfig(_Section):
    documents: dict[str, dict[PyobsField, str]] = Field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_DOCUMENTS.items()},
        description="Document name -> {report value -> JSON key}; served at /api/v1/pyobs-weather/{name}",
    )

    @model_validator(mode="after")
    def _check(self) -> PyobsWeatherConfig:
        for name, fields in self.documents.items():
            if not fields:
                raise ValueError(f"pyobs_weather.documents.{name} is empty")
            if "time" in fields.values():
                raise ValueError(f"pyobs_weather.documents.{name}: 'time' is reserved")
        return self


class SimConfig(_Section):
    seed: int = 0
    report_interval_s: float = Field(2.0, gt=0)
    sky_c: float = Field(-25.0, description="Mean simulated sky - ambient (clear sky ~ -25 C)")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BOLTWOOD_", env_nested_delimiter="__", extra="forbid")

    _yaml_file: ClassVar[Path | None] = None

    station: str = "boltwood"
    port: str = Field("/dev/ttyUSB0", description="Serial device or pyserial URL (socket://host:port, loop://)")
    baud: int = 4800
    ack: Literal["a", "A"] = Field("a", description="ACK character sent to the head ('a' confirmed on SN 04015)")
    repoll_s: float = Field(2.0, gt=0, le=60, description="Send REQUEST_POLL after this long without a message")
    reconnect_s: float = Field(5.0, gt=0, description="Wait before reopening the port after an error")
    max_age_s: float = Field(30.0, gt=0, description="Reports older than this are stale")
    history: int = Field(200, ge=10, le=10000, description="Messages kept for /api/v1/frames")
    api: ApiConfig = Field(default_factory=ApiConfig)
    pyobs_weather: PyobsWeatherConfig = Field(default_factory=PyobsWeatherConfig)
    sim: SimConfig = Field(default_factory=SimConfig)

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.max_age_s < 3 * self.repoll_s:
            raise ValueError("max_age_s must be >= 3 * repoll_s (the head reports every ~2 s)")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if cls._yaml_file is not None and cls._yaml_file.exists():
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=cls._yaml_file))
        return tuple(sources)

    @classmethod
    def load(cls, path: Path | None, **overrides: Any) -> Settings:
        """Load defaults < ``path`` (if it exists) < environment < ``overrides``."""
        cls._yaml_file = path
        try:
            return cls(**overrides)
        finally:
            cls._yaml_file = None
