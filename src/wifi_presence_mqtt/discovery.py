"""Home Assistant MQTT discovery: entities for the bridge's topics (optional).

With homeassistant.discovery on, the bridge publishes retained config messages under the
discovery prefix (default "homeassistant"), and Home Assistant's MQTT integration
creates, without anything else installed:

- per device: a device_tracker (home / not_home; unknown during an outage) with the
  attributes, and sensors Access point, Area, Home since, Signal quality, Roams today and
  Short drops today;
- per access point: Connectivity (online / offline), Clients and Average signal quality,
  on a device with its model and firmware;
- a bridge device with its own Status; every other entity is unavailable while the
  bridge is offline (its last will).

Entities read the bridge's normal topics; nothing is published twice. Removing a device
or access point from the configuration, or turning discovery off, publishes empty
configs for what was there, which removes those entities from Home Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Any

from wifi_ap_associations import DRIVERS

from . import __version__
from .config import Config
from .views import AccessPointView

# Template that reads one attribute, or "None" (Home Assistant's "unknown") when the
# attribute is missing or empty, e.g. while away.
_FIELD = "{{{{ value_json.{0} if value_json.{0} is defined and value_json.{0} is not none else 'None' }}}}"
_TEXT = "{{ value if value else 'None' }}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_").lower() or "wifi_presence"


def _bridge_id(config: Config) -> str:
    return f"{_slug(config.mqtt.topic_prefix)}_bridge"


def _topic(config: Config, component: str, object_id: str) -> str:
    return f"{config.homeassistant.discovery_prefix}/{component}/{object_id}/config"


def _entities(config: Config) -> list[tuple[str, str, str, str]]:
    """(component, object id, kind, owner id) of every entity, for topics and cleanup."""
    base = _slug(config.mqtt.topic_prefix)
    entities = [("binary_sensor", f"{base}_bridge_status", "bridge_status", "")]
    for device in config.devices:
        stem = f"{base}_{_slug(device.id)}"
        entities += [
            ("device_tracker", stem, "tracker", device.id),
            ("sensor", f"{stem}_access_point", "access_point", device.id),
            ("sensor", f"{stem}_area", "area", device.id),
            ("sensor", f"{stem}_home_since", "home_since", device.id),
            ("sensor", f"{stem}_signal_quality", "signal_quality", device.id),
            ("sensor", f"{stem}_roams_today", "roams_today", device.id),
            ("sensor", f"{stem}_short_drops_today", "short_drops_today", device.id),
        ]
    for ap in config.access_points:
        stem = f"{base}_ap_{_slug(ap.id)}"
        entities += [
            ("binary_sensor", f"{stem}_connectivity", "ap_connectivity", ap.id),
            ("sensor", f"{stem}_clients", "ap_clients", ap.id),
            ("sensor", f"{stem}_signal_quality", "ap_signal_quality", ap.id),
        ]
    return entities


def discovery_topics(config: Config) -> set[str]:
    """Every config topic this configuration publishes (empty with discovery off)."""
    if not config.homeassistant.discovery:
        return set()
    return {_topic(config, component, object_id) for component, object_id, _, _ in _entities(config)}


def messages(config: Config, access_points: Mapping[str, AccessPointView]) -> dict[str, str]:
    """The retained config messages, from the current access point details."""
    if not config.homeassistant.discovery:
        return {}
    prefix = config.mqtt.topic_prefix
    bridge_id = _bridge_id(config)
    availability = {
        "availability_topic": f"{prefix}/status",
        "payload_available": "online",
        "payload_not_available": "offline",
    }
    bridge_device = {
        "identifiers": [bridge_id],
        "name": "Wi-Fi presence (MQTT)",
        "manufacturer": "wifi-presence-mqtt",
        "model": "MQTT bridge",
        "sw_version": __version__,
    }
    devices = {d.id: d for d in config.devices}
    aps = {a.id: a for a in config.access_points}
    out: dict[str, str] = {}
    for component, object_id, kind, owner in _entities(config):
        payload: dict[str, Any] = {"unique_id": object_id, "object_id": object_id}
        if kind == "bridge_status":
            payload |= {
                "name": "Status",
                "state_topic": f"{prefix}/status",
                "payload_on": "online",
                "payload_off": "offline",
                "device_class": "connectivity",
                "entity_category": "diagnostic",
                "device": bridge_device,
            }
            out[_topic(config, component, object_id)] = json.dumps(payload)
            continue
        if owner in devices:
            device = devices[owner]
            base = f"{prefix}/device/{device.id}"
            payload |= availability | {
                "device": {
                    "identifiers": [f"{_slug(prefix)}_{_slug(device.id)}"],
                    "name": device.name or device.id,
                    "connections": [["mac", device.mac.lower()]],
                    "via_device": bridge_id,
                },
            }
            if kind == "tracker":
                payload |= {
                    "name": None,
                    "state_topic": f"{base}/state",
                    "payload_home": "home",
                    "payload_not_home": "not_home",
                    "payload_reset": "unknown",
                    "source_type": "router",
                    "json_attributes_topic": f"{base}/attributes",
                    "icon": "mdi:cellphone-wireless",
                }
            elif kind == "access_point":
                payload |= {"name": "Access point", "state_topic": f"{base}/attributes",
                            "value_template": _FIELD.format("access_point_name"), "icon": "mdi:access-point"}
            elif kind == "area":
                payload |= {"name": "Area", "state_topic": f"{base}/attributes",
                            "value_template": _FIELD.format("area"), "icon": "mdi:home-map-marker"}
            elif kind == "home_since":
                payload |= {"name": "Home since", "state_topic": f"{base}/home_since",
                            "value_template": _TEXT, "device_class": "timestamp"}
            elif kind == "signal_quality":
                payload |= {"name": "Signal quality", "state_topic": f"{base}/attributes",
                            "value_template": _FIELD.format("quality"), "unit_of_measurement": "%",
                            "state_class": "measurement", "icon": "mdi:wifi", "entity_category": "diagnostic"}
            elif kind in ("roams_today", "short_drops_today"):
                payload |= {"name": "Roams today" if kind == "roams_today" else "Short drops today",
                            "state_topic": f"{base}/attributes", "value_template": _FIELD.format(kind),
                            "state_class": "measurement", "entity_category": "diagnostic",
                            "icon": "mdi:swap-horizontal" if kind == "roams_today" else "mdi:wifi-strength-alert-outline"}
        else:
            ap = aps[owner]
            view = access_points.get(owner)
            attrs = view.attributes if view else {}
            base = f"{prefix}/ap/{ap.id}"
            payload |= availability | {
                "device": {
                    "identifiers": [f"{_slug(prefix)}_ap_{_slug(ap.id)}"],
                    "name": attrs.get("name") or ap.name or ap.id,
                    "manufacturer": DRIVERS[ap.type].MANUFACTURER or None,
                    "model": attrs.get("model"),
                    "sw_version": attrs.get("firmware"),
                    "suggested_area": ap.area,
                    "via_device": bridge_id,
                },
            }
            payload["device"] = {k: v for k, v in payload["device"].items() if v}
            if kind == "ap_connectivity":
                payload |= {"name": "Connectivity", "state_topic": f"{base}/state", "payload_on": "online",
                            "payload_off": "offline", "device_class": "connectivity"}
            elif kind == "ap_clients":
                payload |= {"name": "Clients", "state_topic": f"{base}/attributes",
                            "value_template": _FIELD.format("clients"), "state_class": "measurement",
                            "icon": "mdi:account-multiple"}
            elif kind == "ap_signal_quality":
                payload |= {"name": "Average signal quality", "state_topic": f"{base}/attributes",
                            "value_template": _FIELD.format("average_signal_quality"), "unit_of_measurement": "%",
                            "state_class": "measurement", "icon": "mdi:wifi", "entity_category": "diagnostic"}
        out[_topic(config, component, object_id)] = json.dumps(payload)
    return out
