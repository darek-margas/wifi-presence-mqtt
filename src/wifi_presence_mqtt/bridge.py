"""The running bridge: polling, instant arrivals, presence, MQTT, the state file.

Two independent loops: the poll loop reads the access points into a PresenceTracker
(the library's, the same rules as the Home Assistant integration) and says what the
retained topics should hold now; the MQTT link keeps the broker up to date with that,
reconnecting when it drops and republishing everything on each connect. A broker
outage never stops presence from being tracked, and a slow access point never holds up
MQTT.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import importlib
import json
import logging
import os
from pathlib import Path
import ssl
import time
from typing import Any, Protocol

from wifi_ap_associations import DRIVERS, AccessPointDriver, AccessPointError, snmp_trap
from wifi_ap_associations.presence import AccessPointRead, stored_at
from wifi_ap_associations.tracker import (
    AllAccessPointsFailed,
    PresenceTracker,
    poll_all,
    watch_forever,
)

from . import discovery, views
from .config import Config, MqttSettings

_LOGGER = logging.getLogger("wifi_presence_mqtt")

STATE_VERSION = 1
SAVE_INTERVAL = timedelta(minutes=10)
# A change reported by an access point polls at once, then at most once per this long.
TRIGGERED_POLL_COOLDOWN = 5.0
# Events kept while the broker is unreachable; older ones are dropped.
EVENT_BUFFER = 200
RECONNECT_MIN = 5.0
RECONNECT_MAX = 60.0


def _uid() -> str:
    getuid = getattr(os, "getuid", None)
    return str(getuid()) if getuid else "?"


def utcnow() -> datetime:
    return datetime.now(UTC)


def local_today() -> Any:
    return datetime.now().astimezone().date()


class Link(Protocol):
    """Where the bridge's messages go (the MQTT link, or a test double)."""

    connected: bool

    def set_retained(self, messages: Mapping[str, str]) -> None: ...

    def publish_event(self, topic: str, payload: str) -> None: ...


class MqttLink:
    """Keeps the broker's retained topics equal to what the bridge says they should be."""

    def __init__(self, settings: MqttSettings) -> None:
        self.settings = settings
        self.status_topic = f"{settings.topic_prefix}/status"
        self.connected = False
        self._desired: dict[str, str] = {}
        self._dirty: set[str] = set()
        self._events: deque[tuple[str, str]] = deque(maxlen=EVENT_BUFFER)
        self._wake = asyncio.Event()

    def set_retained(self, messages: Mapping[str, str]) -> None:
        """Publish each topic whose payload changed (an empty payload clears it)."""
        for topic, payload in messages.items():
            if self._desired.get(topic) != payload:
                self._desired[topic] = payload
                self._dirty.add(topic)
        if self._dirty:
            self._wake.set()

    def publish_event(self, topic: str, payload: str) -> None:
        self._events.append((topic, payload))
        self._wake.set()

    async def run(self) -> None:
        """Connect, publish, and reconnect for ever (until cancelled)."""
        import aiomqtt

        delay = RECONNECT_MIN
        logged_down = False
        s = self.settings
        tls = None
        if s.tls:
            tls = aiomqtt.TLSParameters(ca_certs=s.ca_file, cert_reqs=ssl.CERT_REQUIRED)
        while True:
            try:
                async with aiomqtt.Client(
                    s.host,
                    port=s.port,
                    username=s.username,
                    password=s.password,
                    identifier=s.client_id,
                    tls_params=tls,
                    will=aiomqtt.Will(self.status_topic, "offline", qos=1, retain=True),
                    keepalive=60,
                ) as client:
                    self.connected = True
                    delay = RECONNECT_MIN
                    _LOGGER.info("Connected to MQTT broker %s:%s", s.host, s.port)
                    logged_down = False
                    await client.publish(self.status_topic, "online", qos=1, retain=True)
                    # Everything again: the broker may have lost retained messages.
                    self._dirty = set(self._desired)
                    try:
                        while True:
                            await self._flush(client)
                            await self._wake.wait()
                            self._wake.clear()
                    except asyncio.CancelledError:
                        # Stopping: say so, rather than leave it to the last will.
                        try:
                            await asyncio.wait_for(
                                client.publish(self.status_topic, "offline", qos=1, retain=True), 5
                            )
                        except Exception:  # noqa: BLE001 - best effort while stopping
                            pass
                        raise
            except aiomqtt.MqttError as err:
                self.connected = False
                if not logged_down:
                    _LOGGER.warning("MQTT broker %s:%s unreachable (%s); retrying", s.host, s.port, err)
                    logged_down = True
                else:
                    _LOGGER.debug("MQTT reconnect failed: %s", err)
            finally:
                self.connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    async def _flush(self, client: Any) -> None:
        while self._dirty:
            topic = self._dirty.pop()
            payload = self._desired.get(topic)
            if payload is None:
                continue
            try:
                await client.publish(topic, payload, qos=1, retain=True)
            except BaseException:
                self._dirty.add(topic)
                raise
        while self._events:
            topic, payload = self._events[0]
            await client.publish(topic, payload, qos=1, retain=False)
            self._events.popleft()


class Bridge:
    """Polls the access points and tells the link what to publish."""

    def __init__(self, config: Config, link: Link) -> None:
        self.config = config
        self.link = link
        self.prefix = config.mqtt.topic_prefix
        self.access_points = {ap.id: ap for ap in config.access_points}
        self.devices = {d.id: d for d in config.devices}
        self.drivers: dict[str, AccessPointDriver] = {
            ap.id: DRIVERS[ap.type](dict(ap.settings)) for ap in config.access_points
        }
        self.grace = timedelta(seconds=config.presence.grace_period)
        fast = config.presence.fast_departure
        self.tracker = PresenceTracker(
            self.grace,
            poll_interval=timedelta(seconds=config.presence.poll_interval),
            fast_departure=timedelta(seconds=fast) if fast else None,
        )
        # D-Link DAPs report joins as SNMP traps to this port (one listener for all).
        snmp_trap.LISTENER.configure(port=config.presence.snmp_trap_port)
        # Fast departure: the polls due at the deadlines after leaves, one per leave (a
        # later leave never moves an earlier deadline).
        self._departure_polls: set[asyncio.TimerHandle] = set()
        self.trigger = asyncio.Event()
        self.data_dir = Path(config.data_dir)
        self.state_file = self.data_dir / "state.json"
        self.health_file = self.data_dir / "health.json"
        self._errors: dict[str, str] = {}  # last error per access point id
        self._failing: set[str] = set()
        self._device_views: dict[str, views.DeviceView] = {}
        self._next_save: datetime | None = None
        self._outage = False

    # --- state file ---

    def restore(self) -> None:
        """Load the state saved at the last stop; clear topics of removed ids."""
        now = utcnow()
        stored: dict[str, Any] = {}
        try:
            stored = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _LOGGER.info("No saved state in %s: starting fresh", self.state_file)
        except (OSError, ValueError) as err:
            _LOGGER.warning("Saved state %s unreadable (%s): starting fresh", self.state_file, err)
        self.tracker.restore(stored.get("tracker"), now, local_today())
        published = stored.get("published") or {}
        gone_devices = set(published.get("devices") or []) - set(self.devices)
        gone_aps = set(published.get("access_points") or []) - set(self.access_points)
        if gone_devices or gone_aps:
            _LOGGER.info(
                "Clearing topics of removed devices %s and access points %s",
                sorted(gone_devices) or "-", sorted(gone_aps) or "-",
            )
            self.link.set_retained(
                dict.fromkeys(views.removed_topics(self.prefix, gone_devices, gone_aps), "")
            )
        # Who was home when the state was saved: the first round's events are measured
        # against that, so an arrival while stopped is an event, and a visit that went
        # on across the restart isn't (the time stopped is no evidence of leaving).
        # Home Assistant entities no longer configured (or discovery turned off): an
        # empty config removes them.
        stale = set(stored.get("discovery_topics") or []) - discovery.discovery_topics(self.config)
        if stale:
            _LOGGER.info("Removing %d Home Assistant discovery entities no longer configured", len(stale))
            self.link.set_retained(dict.fromkeys(sorted(stale), ""))
        # Saved while presence wasn't known (no access point read since start, or an
        # outage): nothing to compare with, so the first round raises no events.
        saved = stored_at(stored.get("tracker"))
        known = stored.get("presence_known", True) is True
        self._device_views = self._views(saved, outage=False)[0] if saved and known else {}

    def save(self, clean: bool = False) -> None:
        now = utcnow()
        data = {
            "version": STATE_VERSION,
            "tracker": self.tracker.to_storage(now, clean=clean),
            "published": {"devices": sorted(self.devices), "access_points": sorted(self.access_points)},
            "presence_known": self.tracker.last_success is not None and not self._outage,
            "discovery_topics": sorted(discovery.discovery_topics(self.config)),
        }
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temp = self.state_file.with_suffix(".tmp")
            temp.write_text(json.dumps(data), encoding="utf-8")
            temp.replace(self.state_file)
        except PermissionError as err:
            _LOGGER.warning(
                "Cannot save state to %s (%s): the data directory isn't writable for user %s. "
                "Make it so (e.g. chown it to that user) or don't set a container user; until "
                "then visits and today's counts won't survive a restart",
                self.state_file, err.strerror, _uid(),
            )
        except OSError as err:
            _LOGGER.warning("Cannot save state to %s: %s", self.state_file, err)

    def write_health(self, round_ok: bool) -> None:
        data = {
            "at": utcnow().isoformat(timespec="seconds"),
            "mqtt_connected": self.link.connected,
            "round_ok": round_ok,
            "stale_after": 3 * self.config.presence.poll_interval + 30,
        }
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.health_file.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    # --- one round ---

    def _views(
        self, now: datetime, outage: bool
    ) -> tuple[dict[str, views.DeviceView], dict[str, views.AccessPointView]]:
        states = self.tracker.access_points
        ap_names = {
            ap_id: views.access_point_name(ap, states.get(ap_id))
            for ap_id, ap in self.access_points.items()
        }
        devices = {
            device.id: views.device_view(
                device,
                self.tracker.current(device.mac, now),
                self.tracker.sightings.get(device.mac),
                self.tracker.coverage.counts.get(device.mac),
                self.tracker.consider_home,
                self.access_points,
                ap_names,
                outage,
                self.tracker.away_since(device.mac, now),
            )
            for device in self.config.devices
        }
        aps = {
            ap_id: views.access_point_view(
                ap,
                states.get(ap_id),
                self.tracker.access_point_coverage.get(ap_id),
                self._errors.get(ap_id),
            )
            for ap_id, ap in self.access_points.items()
        }
        return devices, aps

    def _log_result(self, ap_id: str, result: object) -> None:
        if not isinstance(result, BaseException):
            self._errors.pop(ap_id, None)
            if ap_id in self._failing:
                _LOGGER.info("Access point %s is readable again", ap_id)
                self._failing.discard(ap_id)
            return
        reason = str(result) or type(result).__name__
        if isinstance(result, TimeoutError):
            reason = "timeout"
        self._errors[ap_id] = reason
        if ap_id in self._failing:
            return
        self._failing.add(ap_id)
        if isinstance(result, (AccessPointError, TimeoutError)):
            _LOGGER.warning("Cannot read access point %s: %s", ap_id, reason)
        else:
            _LOGGER.error("Unexpected error reading access point %s", ap_id, exc_info=result)

    async def poll_once(self) -> None:
        """Read every access point once and publish what changed."""
        started = time.monotonic()
        # The reads happen between this and the end of the round (fast departure).
        round_started = utcnow()
        ids = list(self.drivers)
        results = await poll_all([self.drivers[i] for i in ids])
        reads = []
        for ap_id, result in zip(ids, results, strict=True):
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result
            self._log_result(ap_id, result)
            info = None if isinstance(result, Exception) else result.info
            name = self.access_points[ap_id].name or (info.name if info and info.name else ap_id)
            reads.append(AccessPointRead(ap_id, name, self.drivers[ap_id].SIGNAL_UNIT, result))
        now = utcnow()
        outage = False
        try:
            self.tracker.update(reads, now, local_today(), started=round_started)
        except AllAccessPointsFailed:
            outage = True
        if outage and not self._outage:
            _LOGGER.warning("No access point readable past the grace period: presence unknown")
        elif self._outage and not outage:
            _LOGGER.info("Access points readable again: presence known")
        self._outage = outage
        _LOGGER.debug("Polled %d access point(s) in %.2f s", len(ids), time.monotonic() - started)

        devices, aps = self._views(now, outage)
        messages = views.retained_messages(self.prefix, devices, aps)
        messages |= discovery.messages(self.config, aps)
        if self.config.presence.publish_all_clients and not outage:
            messages[f"{self.prefix}/clients"] = views.clients_payload(self.tracker.sightings, now)
        self.link.set_retained(messages)
        for payload in views.events(self._device_views, devices, self.devices, now):
            _LOGGER.info("Event: %s", payload)
            self.link.publish_event(f"{self.prefix}/event", payload)
        self._device_views = devices
        if not outage and (self._next_save is None or now >= self._next_save):
            self.save()
            self._next_save = now + SAVE_INTERVAL
        self.write_health(round_ok=not outage)

    # --- loops ---

    async def poll_loop(self) -> None:
        interval = self.config.presence.poll_interval
        last = -float("inf")
        due = time.monotonic()
        while True:
            try:
                await asyncio.wait_for(self.trigger.wait(), max(0.0, due - time.monotonic()))
                triggered = True
            except TimeoutError:
                triggered = False
            if triggered:
                wait = last + TRIGGERED_POLL_COOLDOWN - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                _LOGGER.debug("Change reported: polling now")
            self.trigger.clear()
            last = time.monotonic()
            await self.poll_once()
            due = last + interval

    def on_leave(self, ap_id: str) -> None:
        """An access point reported a client leaving (fast departure is on): note it,
        and poll at the deadline so a device still missing then turns away on time."""
        fast = self.tracker.fast_departure
        if fast is None:
            return
        _LOGGER.debug("Client left %s: devices missing from it are away in %s", ap_id, fast)
        self.tracker.note_leave(ap_id, utcnow())
        self._book_departure_poll(fast.total_seconds())

    def book_pending_departures(self) -> None:
        """After restore(), in the event loop: fast departures that were pending at the
        restart are still due on time."""
        now = utcnow()
        for at in self.tracker.pending_departures(now):
            self._book_departure_poll((at - now).total_seconds())

    def _book_departure_poll(self, delay: float) -> None:
        """Poll just after a fast departure deadline `delay` seconds from now."""

        def due() -> None:
            self._departure_polls.discard(handle)
            self.trigger.set()

        handle = asyncio.get_running_loop().call_later(delay + 1, due)
        self._departure_polls.add(handle)

    def cancel_departure_polls(self) -> None:
        for handle in self._departure_polls:
            handle.cancel()
        self._departure_polls.clear()

    def start_watchers(self) -> list[asyncio.Task[None]]:
        """Instant arrivals: one watch per watch key, for the types that can."""
        if not self.config.presence.instant_arrivals:
            return []
        tasks: list[asyncio.Task[None]] = []
        watched: set[Any] = set()
        for ap_id, driver in self.drivers.items():
            if not driver.WATCHES or (key := driver.watch_key()) in watched:
                continue
            watched.add(key)
            tasks.append(
                asyncio.create_task(
                    watch_forever(
                        driver,
                        self.trigger.set,
                        on_leave=(lambda i=ap_id: self.on_leave(i)) if self.tracker.fast_departure else None,
                        on_paused=lambda reason, i=ap_id: _LOGGER.info(
                            "Instant arrivals for %s paused (%s); polling goes on", i, reason
                        ),
                        on_resumed=lambda i=ap_id: _LOGGER.info("Instant arrivals for %s work again", i),
                        on_failed_again=lambda reason, i=ap_id: _LOGGER.debug(
                            "Watching %s again failed: %s", i, reason
                        ),
                    ),
                    name=f"watch {ap_id}",
                )
            )
        return tasks


def import_blocking_modules(config: Config) -> None:
    """Modules drivers load on first use that read files (pysnmp), before the loop runs."""
    for ap in config.access_points:
        for module in DRIVERS[ap.type].BLOCKING_IMPORTS:
            importlib.import_module(module)


async def run(config: Config) -> None:
    """Run until cancelled; on the way out save the state cleanly."""
    link = MqttLink(config.mqtt)
    bridge = Bridge(config, link)
    bridge.restore()
    bridge.book_pending_departures()
    tasks = [asyncio.create_task(link.run(), name="mqtt")]
    tasks += bridge.start_watchers()
    poller = asyncio.create_task(bridge.poll_loop(), name="poll")
    tasks.append(poller)
    _LOGGER.info(
        "Watching %d access point(s) for %d device(s); publishing under %s/",
        len(config.access_points), len(config.devices), config.mqtt.topic_prefix,
    )
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()  # a crash in a loop is a bug: raise it
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        bridge.cancel_departure_polls()
        for driver in bridge.drivers.values():  # e.g. a UniFi or Omada controller session
            await driver.async_close()
        bridge.save(clean=True)
        _LOGGER.info("Stopped; state saved")
