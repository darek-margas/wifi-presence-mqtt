"""Home Assistant MQTT discovery: config messages, and removing what's gone."""

from __future__ import annotations

import dataclasses
import json

from wifi_presence_mqtt.bridge import Bridge
from wifi_presence_mqtt.config import HomeAssistantSettings

from test_bridge import PHONE, Clock, FakeAP, FakeLink, fake_driver, make_config, round_  # noqa: F401


def with_discovery(config, on: bool = True):
    return dataclasses.replace(config, homeassistant=HomeAssistantSettings(discovery=on))


def configs(link: FakeLink) -> dict[str, dict]:
    return {t: json.loads(p) for t, p in link.retained.items() if t.startswith("homeassistant/") and p}


def test_off_by_default_publishes_no_configs(tmp_path, monkeypatch) -> None:
    Clock(monkeypatch)
    link = FakeLink()
    b = Bridge(make_config(tmp_path), link)
    b.restore()
    round_(b)
    assert not any(t.startswith("homeassistant/") for t in link.retained)


def test_entities_for_devices_access_points_and_the_bridge(tmp_path, monkeypatch) -> None:
    Clock(monkeypatch)
    FakeAP.answers = {"hall": [(PHONE, -50)], "den": []}
    link = FakeLink()
    b = Bridge(with_discovery(make_config(tmp_path)), link)
    b.restore()
    round_(b)
    found = configs(link)

    tracker = found["homeassistant/device_tracker/wifi_presence_phone/config"]
    assert tracker["state_topic"] == "wifi-presence/device/phone/state"
    assert tracker["json_attributes_topic"] == "wifi-presence/device/phone/attributes"
    assert (tracker["payload_home"], tracker["payload_not_home"], tracker["payload_reset"]) == (
        "home", "not_home", "unknown")
    assert tracker["source_type"] == "router" and tracker["name"] is None
    assert tracker["availability_topic"] == "wifi-presence/status"
    assert tracker["device"]["connections"] == [["mac", PHONE.lower()]]
    assert tracker["device"]["via_device"] == "wifi_presence_bridge"
    assert tracker["device"]["identifiers"] == ["wifi_presence_phone"]

    sensors = {c["name"]: c for t, c in found.items() if t.startswith("homeassistant/sensor/wifi_presence_phone")}
    assert set(sensors) == {"Access point", "Area", "Home since", "Signal quality", "Roams today", "Short drops today"}
    assert sensors["Home since"]["device_class"] == "timestamp"
    assert sensors["Signal quality"]["unit_of_measurement"] == "%"
    # All of one device's entities are on one Home Assistant device.
    assert {json.dumps(c["device"]) for c in sensors.values()} == {json.dumps(tracker["device"])}

    hall = found["homeassistant/binary_sensor/wifi_presence_ap_hall_connectivity/config"]
    assert hall["state_topic"] == "wifi-presence/ap/hall/state" and hall["device_class"] == "connectivity"
    assert hall["device"]["model"] == "X1" and hall["device"]["name"] == "AP hall"
    assert hall["device"]["suggested_area"] == "Hall"
    bridge = found["homeassistant/binary_sensor/wifi_presence_bridge_status/config"]
    assert "availability_topic" not in bridge  # shows offline itself rather than unavailable
    unique_ids = [c["unique_id"] for c in found.values()]
    assert len(unique_ids) == len(set(unique_ids)) == 1 + 7 + 2 * 3


def test_templates_give_unknown_while_away() -> None:
    from jinja2 import Template  # Home Assistant renders these with Jinja

    from wifi_presence_mqtt.discovery import _FIELD, _TEXT

    field = Template(_FIELD.format("access_point_name"))

    class Obj(dict):
        __getattr__ = dict.get

    assert field.render(value_json=Obj(access_point_name="Hall")) == "Hall"
    assert field.render(value_json=Obj(mac="x")) == "None"
    assert field.render(value_json=Obj(access_point_name=None)) == "None"
    assert Template(_TEXT).render(value="") == "None"
    assert Template(_TEXT).render(value="2026-10-20T08:00:00+00:00") == "2026-10-20T08:00:00+00:00"


def test_turning_discovery_off_or_removing_a_device_removes_its_entities(tmp_path, monkeypatch) -> None:
    Clock(monkeypatch)
    first = Bridge(with_discovery(make_config(tmp_path, devices=("phone", "tablet"))), FakeLink())
    first.restore()
    round_(first)
    first.save()

    link = FakeLink()
    second = Bridge(with_discovery(make_config(tmp_path, devices=("phone",))), link)
    second.restore()
    cleared = {t for t, p in link.retained.items() if p == "" and t.startswith("homeassistant/")}
    assert "homeassistant/device_tracker/wifi_presence_tablet/config" in cleared
    assert not any("wifi_presence_phone" in t for t in cleared)
    round_(second)
    second.save()

    link = FakeLink()
    third = Bridge(with_discovery(make_config(tmp_path, devices=("phone",)), on=False), link)
    third.restore()
    cleared = {t for t, p in link.retained.items() if p == "" and t.startswith("homeassistant/")}
    assert "homeassistant/device_tracker/wifi_presence_phone/config" in cleared
    assert "homeassistant/binary_sensor/wifi_presence_bridge_status/config" in cleared


def test_no_signal_quality_entity_for_types_without_signal(tmp_path, monkeypatch) -> None:
    Clock(monkeypatch)
    monkeypatch.setattr(FakeAP, "CLIENT_DETAILS", frozenset())  # MACs only, as TP-Link EAP
    FakeAP.answers = {"hall": [(PHONE, None)], "den": []}
    link = FakeLink()
    b = Bridge(with_discovery(make_config(tmp_path)), link)
    b.restore()
    round_(b)
    found = configs(link)
    assert "homeassistant/sensor/wifi_presence_ap_hall_clients/config" in found
    assert not any("_ap_" in t and t.endswith("_signal_quality/config") for t in found)
