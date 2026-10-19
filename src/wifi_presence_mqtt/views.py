"""What to publish: topics and payloads from the presence state, and the events.

Pure functions (no MQTT, no clock): the bridge calls them after every poll round and
publishes what changed. States are retained; events are not.

Topics, under the prefix (default "wifi-presence"):

    status                          online / offline (the last will)
    device/<id>/state               home / not_home / unknown
    device/<id>/attributes          JSON
    device/<id>/home_since          ISO time, empty while away
    device/<id>/away_since          ISO time, empty while home
    device/<id>/access_point        access point id, empty while away
    ap/<id>/state                   online / offline
    ap/<id>/attributes              JSON
    event                           JSON, not retained: arrived / left / roamed
    clients                         JSON list of every client seen (opt-in)
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from typing import Any

from wifi_ap_associations.coverage import AccessPointCoverage, DeviceCounts
from wifi_ap_associations.presence import AccessPointState, Sighting

from .config import AccessPointConfig, DeviceConfig

HOME = "home"
NOT_HOME = "not_home"
UNKNOWN = "unknown"
DEVICE_TOPICS = ("state", "attributes", "home_since", "away_since", "access_point")
AP_TOPICS = ("state", "attributes")


def _iso(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value else ""


def _json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class DeviceView:
    """One tracked device as published."""

    state: str
    access_point: str  # id, "" unless home
    home_since: str
    away_since: str
    attributes: dict[str, Any]


def device_view(
    device: DeviceConfig,
    current: Sighting | None,
    known: Sighting | None,
    counts: DeviceCounts | None,
    grace: timedelta,
    access_points: Mapping[str, AccessPointConfig],
    ap_names: Mapping[str, str],
    outage: bool,
    departed: datetime | None = None,
) -> DeviceView:
    """The device's state after a round.

    current: its sighting if home (within the grace period); known: its last sighting at
    all (kept for days), for when it left. outage: no access point readable for longer
    than the grace period, so nobody's presence is known.
    """
    attributes: dict[str, Any] = {"mac": device.mac, "name": device.name or device.id}
    if counts is not None:
        attributes |= {
            "roams_today": counts.roams,
            "short_drops_today": counts.drops,
            "late_roams_today": counts.late_roams,
        }
    if outage:
        return DeviceView(UNKNOWN, "", "", "", attributes)
    if current is None:
        if departed is None and known is not None:
            departed = known.last_seen + grace
        away_since = _iso(departed)
        if known is not None:
            attributes["last_seen"] = _iso(known.last_seen)
        attributes["away_since"] = away_since
        return DeviceView(NOT_HOME, "", "", away_since, attributes)
    ap_id = current.access_point_id
    ap = access_points.get(ap_id)
    home_since = _iso(current.arrived)
    attributes |= {
        "access_point": ap_id,
        "access_point_name": ap_names.get(ap_id, ap_id),
        "area": ap.area if ap else None,
        "ssid": current.ssid,
        "band": current.band,
        "signal": current.signal,
        "signal_unit": current.signal_unit,
        "quality": current.quality,
        "last_seen": _iso(current.last_seen),
        "home_since": home_since,
    }
    return DeviceView(HOME, ap_id, home_since, "", attributes)


@dataclass(frozen=True, slots=True)
class AccessPointView:
    """One access point as published."""

    state: str
    attributes: dict[str, Any]


def access_point_name(ap: AccessPointConfig, state: AccessPointState | None) -> str:
    """The configured name, else what the access point reports, else its id."""
    reported = state.info.name if state and state.info else None
    return ap.name or reported or ap.id


def access_point_view(
    ap: AccessPointConfig,
    state: AccessPointState | None,
    coverage: AccessPointCoverage | None,
    error: str | None,
) -> AccessPointView:
    """The access point's state after a round (offline until first read)."""
    online = state is not None and state.available
    info = state.info if state else None
    attributes: dict[str, Any] = {
        "name": access_point_name(ap, state),
        "type": ap.type,
        "area": ap.area,
    }
    if info is not None:
        attributes |= {
            "model": info.model,
            "firmware": info.firmware,
            "hardware": info.hardware,
            "uptime_seconds": info.uptime_seconds,
            "cpu_percent": info.cpu_percent,
            "memory_percent": info.memory_percent,
        }
    if online and state is not None:
        attributes |= {
            "last_boot": _iso(state.last_boot),
            "clients": sum(state.clients_by_band.values()),
            "clients_by_band": dict(state.clients_by_band),
            "average_signal_quality": coverage.average_quality if coverage else None,
            "weak_clients": coverage.weak_clients if coverage else 0,
        }
    else:
        attributes["error"] = error or "not read yet"
    return AccessPointView("online" if online else "offline", attributes)


def retained_messages(
    prefix: str,
    devices: Mapping[str, DeviceView],
    access_points: Mapping[str, AccessPointView],
) -> dict[str, str]:
    """Every retained topic and its payload."""
    messages: dict[str, str] = {}
    for device_id, view in devices.items():
        base = f"{prefix}/device/{device_id}"
        messages[f"{base}/state"] = view.state
        messages[f"{base}/attributes"] = _json(view.attributes)
        messages[f"{base}/home_since"] = view.home_since
        messages[f"{base}/away_since"] = view.away_since
        messages[f"{base}/access_point"] = view.access_point
    for ap_id, view in access_points.items():
        base = f"{prefix}/ap/{ap_id}"
        messages[f"{base}/state"] = view.state
        messages[f"{base}/attributes"] = _json(view.attributes)
    return messages


def removed_topics(prefix: str, devices: Iterable[str], access_points: Iterable[str]) -> list[str]:
    """Retained topics of devices and access points no longer configured, to clear."""
    topics = [f"{prefix}/device/{d}/{t}" for d in devices for t in DEVICE_TOPICS]
    topics += [f"{prefix}/ap/{a}/{t}" for a in access_points for t in AP_TOPICS]
    return topics


def events(
    before: Mapping[str, DeviceView],
    after: Mapping[str, DeviceView],
    devices: Mapping[str, DeviceConfig],
    now: datetime,
) -> list[str]:
    """Event payloads for what changed between two rounds.

    Only between home and not_home (and roams while home): a device that goes from or
    to unknown (an outage, or the first round) was not seen arriving or leaving.
    """
    payloads: list[str] = []
    for device_id, new in after.items():
        old = before.get(device_id)
        if old is None:
            continue
        device = devices[device_id]
        base = {"device": device_id, "name": device.name or device_id, "at": _iso(now)}
        if old.state == NOT_HOME and new.state == HOME:
            payloads.append(_json({"event": "arrived", **base, "access_point": new.access_point}))
        elif old.state == HOME and new.state == NOT_HOME:
            payloads.append(_json({"event": "left", **base, "access_point": old.access_point}))
        elif old.state == HOME and new.state == HOME and old.access_point != new.access_point:
            payloads.append(
                _json({"event": "roamed", **base, "access_point": new.access_point, "from": old.access_point})
            )
    return payloads


def clients_payload(sightings: Mapping[str, Sighting], seen_at: datetime) -> str:
    """Every client seen in the latest round (publish_all_clients)."""
    return _json(
        {
            "at": _iso(seen_at),
            "clients": [
                {
                    "mac": mac,
                    "access_point": s.access_point_id,
                    "ssid": s.ssid,
                    "band": s.band,
                    "signal": s.signal,
                    "signal_unit": s.signal_unit,
                }
                for mac, s in sorted(sightings.items())
                if s.last_seen == seen_at
            ],
        }
    )
