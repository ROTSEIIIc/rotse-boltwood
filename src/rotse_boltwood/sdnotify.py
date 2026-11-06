"""Minimal systemd sd_notify (copied from allsky-station). No-op when NOTIFY_SOCKET is unset."""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)


def notify(message: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr or not hasattr(socket, "AF_UNIX"):
        return False
    if addr.startswith("@"):  # abstract namespace socket
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
        return True
    except OSError as e:
        log.debug("sd_notify failed: %s", e)
        return False


def watchdog_interval_s() -> float | None:
    """Half of WatchdogSec (systemd recommends pinging at < half the timeout)."""
    usec = os.environ.get("WATCHDOG_USEC")
    return int(usec) / 2e6 if usec and usec.isdigit() else None
