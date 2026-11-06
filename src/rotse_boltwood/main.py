"""Entry point: ``rotse-boltwood [--sim] [--config config.yaml]``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from . import sdnotify
from .api import create_app
from .config import Settings
from .poller import Poller, open_serial
from .sim import SimHead

log = logging.getLogger("rotse_boltwood")


async def _watchdog(poller: Poller, interval_s: float) -> None:
    """Ping systemd only while the serial loop is alive, so a hung loop gets restarted."""
    while True:
        if poller.loop_healthy():
            sdnotify.notify("WATCHDOG=1")
        await asyncio.sleep(interval_s)


def build_poller(settings: Settings, sim: bool) -> Poller:
    if sim:
        c = settings.sim
        return Poller(settings, lambda: SimHead(seed=c.seed, report_interval_s=c.report_interval_s, sky_c=c.sky_c))
    return Poller(settings, lambda: open_serial(settings))


def build_app(settings: Settings, sim: bool) -> FastAPI:
    poller = build_poller(settings, sim)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        tasks = [asyncio.create_task(poller.run(), name="poller")]
        wd = sdnotify.watchdog_interval_s()
        if wd:
            tasks.append(asyncio.create_task(_watchdog(poller, wd), name="watchdog"))
        sdnotify.notify("READY=1")
        try:
            yield
        finally:
            sdnotify.notify("STOPPING=1")
            poller.stop()
            for t in tasks[1:]:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return create_app(poller, mode="sim" if sim else "hardware", lifespan=lifespan)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="ROTSE Boltwood CS-II head over RS-485 -> JSON API")
    p.add_argument(
        "--sim",
        action="store_true",
        default=os.environ.get("BOLTWOOD_SIM") == "1",
        help="simulated head (no hardware needed)",
    )
    p.add_argument("--config", type=Path, default=Path("config.yaml"), help="YAML config (optional)")
    p.add_argument("--port", help="override the serial port (settings.port)")
    p.add_argument("--host", help="override api.host")
    p.add_argument("--http-port", type=int, help="override api.port")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    overrides = {"port": args.port} if args.port else {}
    settings = Settings.load(args.config, **overrides)
    log.info(
        "starting station=%s mode=%s port=%s config=%s (%s)",
        settings.station,
        "sim" if args.sim else "hardware",
        "sim" if args.sim else settings.port,
        args.config,
        "loaded" if args.config.exists() else "not found, using defaults",
    )
    uvicorn.run(
        build_app(settings, sim=args.sim),
        host=args.host or settings.api.host,
        port=args.http_port or settings.api.port,
        log_level=args.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
