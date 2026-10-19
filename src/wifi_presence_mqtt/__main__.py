"""wifi-presence-mqtt: run the bridge, check the configuration, or check its health.

    wifi-presence-mqtt                  run (until SIGTERM / Ctrl+C)
    wifi-presence-mqtt --check-config   check the configuration only; exit 0 or 1
    wifi-presence-mqtt --health         for the container's health check; exit 0 or 1

Configuration problems are all printed, each with where it is, and the program exits
with status 1 before connecting to anything.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import signal
import sys

from . import __version__
from .config import DEFAULT_DATA_DIR, ConfigError, load


def _health(data_dir: str) -> int:
    """0 while the last round is recent and the broker connected, else 1."""
    path = Path(data_dir) / "health.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age = (datetime.now(UTC) - datetime.fromisoformat(data["at"])).total_seconds()
    except (OSError, ValueError, KeyError) as err:
        print(f"unhealthy: no health file ({err})")
        return 1
    if age > data.get("stale_after", 210):
        print(f"unhealthy: last poll round {age:.0f} s ago")
        return 1
    if not data.get("mqtt_connected"):
        print("unhealthy: not connected to the MQTT broker")
        return 1
    print("healthy")
    return 0


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    numeric = getattr(logging, level.upper())
    logging.getLogger("wifi_presence_mqtt").setLevel(numeric)
    # The library's own debug lines (feed lines, command times) with LOG_LEVEL=debug.
    logging.getLogger("wifi_ap_associations").setLevel(numeric)
    if numeric > logging.DEBUG:
        logging.getLogger("asyncssh").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wifi-presence-mqtt", description=__doc__.split("\n\n")[0])
    parser.add_argument("--check-config", action="store_true", help="check the configuration and exit")
    parser.add_argument("--health", action="store_true", help="exit 0 if healthy (for a container health check)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if args.health:
        return _health(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))

    try:
        config = load()
    except ConfigError as err:
        for line in err.errors:
            print(line, file=sys.stderr)
        count = len(err.errors)
        print(f"{count} configuration error{'s' if count != 1 else ''}; nothing was started.", file=sys.stderr)
        return 1

    if args.check_config:
        print(
            f"configuration OK: {len(config.access_points)} access point(s), "
            f"{len(config.devices)} device(s), broker {config.mqtt.host}:{config.mqtt.port}"
        )
        return 0

    _setup_logging(config.log_level)
    log = logging.getLogger("wifi_presence_mqtt")
    log.info("wifi-presence-mqtt %s", __version__)
    for note in config.notes:
        log.info("%s", note)

    from .bridge import import_blocking_modules, run

    import_blocking_modules(config)

    async def runner() -> None:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, name, None)
            if sig is None or task is None:
                continue
            try:
                loop.add_signal_handler(sig, task.cancel)
            except (NotImplementedError, RuntimeError):  # Windows: Ctrl+C still works
                pass
        try:
            await run(config)
        except asyncio.CancelledError:
            pass

    # The MQTT client needs add_reader(), which Windows' default event loop lacks.
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        asyncio.run(runner(), loop_factory=factory)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
