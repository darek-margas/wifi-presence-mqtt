"""The bridge's rounds: what it publishes for arrivals, roams, departures, outages and
restarts, against fake access points, a fake MQTT link and a controlled clock."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
from typing import Any

import pytest

from wifi_ap_associations import (
    DRIVERS,
    AccessPointDriver,
    AccessPointError,
    AccessPointInfo,
    AssociatedClient,
    DriverField,
    PollResult,
)
from wifi_presence_mqtt import bridge as bridge_module
from wifi_presence_mqtt.bridge import Bridge, MqttLink
from wifi_presence_mqtt.config import (
    AccessPointConfig,
    Config,
    DeviceConfig,
    MqttSettings,
    PresenceSettings,
)

PHONE = "5C:AD:BA:00:00:01"
T0 = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


class FakeAP(AccessPointDriver):
    """Answers with whatever the test put in `clients` for its host (or raises)."""

    TYPE = "fake_bridge"
    NAME = "Fake"
    FIELDS = (DriverField("host"),)
    SIGNAL_UNIT = "dBm"
    answers: dict[str, Any] = {}

    async def async_get_associated_clients(self):
        return (await self.async_poll()).clients

    async def async_poll(self) -> PollResult:
        answer = FakeAP.answers.get(self.config["host"], [])
        if isinstance(answer, BaseException):
            raise answer
        return PollResult(
            [AssociatedClient(mac, "Home", "5GHz", signal) for mac, signal in answer],
            AccessPointInfo(name=f"AP {self.config['host']}", model="X1", uptime_seconds=3600),
        )


class FakeLink:
    connected = True

    def __init__(self) -> None:
        self.retained: dict[str, str] = {}
        self.events: list[dict[str, Any]] = []

    def set_retained(self, messages) -> None:
        self.retained.update(messages)

    def publish_event(self, topic: str, payload: str) -> None:
        self.events.append(json.loads(payload))

    def topic(self, suffix: str) -> str:
        return self.retained[f"wifi-presence/{suffix}"]


@pytest.fixture(autouse=True)
def fake_driver(monkeypatch):
    DRIVERS["fake_bridge"] = FakeAP
    FakeAP.answers = {}
    yield
    DRIVERS.pop("fake_bridge", None)


class Clock:
    def __init__(self, monkeypatch) -> None:
        self.now = T0
        monkeypatch.setattr(bridge_module, "utcnow", lambda: self.now)
        monkeypatch.setattr(bridge_module, "local_today", lambda: date(2026, 10, 20))

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_config(tmp_path: Path, devices=("phone",), aps=("hall", "den"), **presence) -> Config:
    return Config(
        mqtt=MqttSettings(host="broker"),
        presence=PresenceSettings(**{"grace_period": 180, "poll_interval": 60, **presence}),
        access_points=[
            AccessPointConfig(id=ap, type="fake_bridge", settings={"host": ap}, area=ap.title())
            for ap in aps
        ],
        devices=[DeviceConfig(id=d, mac=PHONE if d == "phone" else "5C:AD:BA:00:00:0" + str(i + 2), name=d.title())
                 for i, d in enumerate(devices)],
        data_dir=str(tmp_path / "data"),
    )


def round_(b: Bridge) -> None:
    asyncio.run(b.poll_once())


def test_arrival_roam_and_departure(tmp_path, monkeypatch) -> None:
    clock = Clock(monkeypatch)
    link = FakeLink()
    b = Bridge(make_config(tmp_path), link)
    b.restore()  # nothing saved: no baseline, so the first round is no event
    FakeAP.answers = {"hall": [], "den": []}
    round_(b)
    assert link.topic("device/phone/state") == "not_home"
    assert link.topic("ap/hall/state") == "online"
    assert json.loads(link.topic("ap/hall/attributes"))["clients"] == 0
    assert link.events == []

    clock.advance(60)
    FakeAP.answers = {"hall": [(PHONE, -50)], "den": []}
    round_(b)
    assert link.topic("device/phone/state") == "home"
    assert link.topic("device/phone/access_point") == "hall"
    assert link.topic("device/phone/home_since") == "2026-10-20T08:01:00+00:00"
    assert link.topic("device/phone/away_since") == ""
    attrs = json.loads(link.topic("device/phone/attributes"))
    assert attrs["area"] == "Hall" and attrs["signal"] == -50 and attrs["mac"] == PHONE
    assert link.events[-1] == {"event": "arrived", "device": "phone", "name": "Phone",
                               "at": "2026-10-20T08:01:00+00:00", "access_point": "hall"}

    clock.advance(60)
    FakeAP.answers = {"hall": [], "den": [(PHONE, -55)]}
    round_(b)
    assert link.topic("device/phone/access_point") == "den"
    assert link.topic("device/phone/home_since") == "2026-10-20T08:01:00+00:00"  # same visit
    assert link.events[-1]["event"] == "roamed" and link.events[-1]["from"] == "hall"
    assert json.loads(link.topic("device/phone/attributes"))["roams_today"] == 1

    FakeAP.answers = {"hall": [], "den": []}
    for _ in range(3):  # gone, but within the grace period: still home
        clock.advance(60)
        round_(b)
        if clock.now - T0 < timedelta(seconds=120 + 180):
            assert link.topic("device/phone/state") == "home"
    assert link.topic("device/phone/state") == "not_home"
    assert link.topic("device/phone/away_since") == "2026-10-20T08:05:00+00:00"  # last seen + grace
    assert link.topic("device/phone/access_point") == ""
    assert link.events[-1]["event"] == "left" and link.events[-1]["access_point"] == "den"
    assert [e["event"] for e in link.events] == ["arrived", "roamed", "left"]


def test_a_failing_ap_and_a_total_outage(tmp_path, monkeypatch) -> None:
    clock = Clock(monkeypatch)
    link = FakeLink()
    b = Bridge(make_config(tmp_path), link)
    b.restore()
    FakeAP.answers = {"hall": [(PHONE, -50)], "den": []}
    round_(b)
    clock.advance(60)
    FakeAP.answers = {"hall": [(PHONE, -50)], "den": AccessPointError("connection refused")}
    round_(b)
    assert link.topic("ap/den/state") == "offline"
    assert json.loads(link.topic("ap/den/attributes"))["error"] == "connection refused"
    assert link.topic("device/phone/state") == "home"

    # Nothing readable: ridden out within the grace period after the last good round,
    # then unknown, never not_home, and no "left".
    FakeAP.answers = {"hall": AccessPointError("down"), "den": AccessPointError("down")}
    clock.advance(60)
    round_(b)
    assert link.topic("device/phone/state") == "home"
    clock.advance(180)
    round_(b)
    assert link.topic("device/phone/state") == "unknown"
    assert link.topic("device/phone/access_point") == ""
    assert link.events == []

    clock.advance(60)
    FakeAP.answers = {"hall": [(PHONE, -50)], "den": []}
    round_(b)
    assert link.topic("device/phone/state") == "home"
    assert link.events == []  # from unknown: not seen arriving


def test_restart_keeps_the_visit_and_raises_no_false_event(tmp_path, monkeypatch) -> None:
    clock = Clock(monkeypatch)
    first = Bridge(make_config(tmp_path), FakeLink())
    first.restore()
    FakeAP.answers = {"hall": [(PHONE, -50)], "den": []}
    round_(first)
    first.save(clean=True)

    clock.advance(3600)  # stopped an hour, far past the grace period
    link = FakeLink()
    second = Bridge(make_config(tmp_path), link)
    second.restore()
    round_(second)
    assert link.topic("device/phone/state") == "home"
    assert link.topic("device/phone/home_since") == "2026-10-20T08:00:00+00:00"
    assert link.events == []


def test_arriving_while_stopped_is_an_event(tmp_path, monkeypatch) -> None:
    clock = Clock(monkeypatch)
    first = Bridge(make_config(tmp_path), FakeLink())
    first.restore()
    FakeAP.answers = {"hall": [], "den": []}
    round_(first)
    first.save(clean=True)
    clock.advance(600)
    link = FakeLink()
    second = Bridge(make_config(tmp_path), link)
    second.restore()
    FakeAP.answers = {"hall": [(PHONE, -50)], "den": []}
    round_(second)
    assert [e["event"] for e in link.events] == ["arrived"]


def test_removed_devices_and_access_points_are_cleared(tmp_path, monkeypatch) -> None:
    Clock(monkeypatch)
    first = Bridge(make_config(tmp_path, devices=("phone", "tablet"), aps=("hall", "den")), FakeLink())
    first.restore()
    round_(first)
    first.save()
    link = FakeLink()
    second = Bridge(make_config(tmp_path, devices=("phone",), aps=("hall",)), link)
    second.restore()
    cleared = {t for t, p in link.retained.items() if p == ""}
    assert "wifi-presence/device/tablet/state" in cleared
    assert "wifi-presence/device/tablet/attributes" in cleared
    assert "wifi-presence/ap/den/state" in cleared
    assert not any("phone" in t or "/hall/" in t for t in cleared)


def test_all_clients_topic_is_opt_in(tmp_path, monkeypatch) -> None:
    Clock(monkeypatch)
    FakeAP.answers = {"hall": [(PHONE, -50), ("AA:BB:CC:00:00:01", -70)], "den": []}
    link = FakeLink()
    b = Bridge(make_config(tmp_path), link)
    b.restore()
    round_(b)
    assert "wifi-presence/clients" not in link.retained
    link = FakeLink()
    b = Bridge(make_config(tmp_path, publish_all_clients=True), link)
    b.restore()
    round_(b)
    clients = json.loads(link.topic("clients"))["clients"]
    assert [c["mac"] for c in clients] == ["5C:AD:BA:00:00:01", "AA:BB:CC:00:00:01"]


def test_state_and_health_files(tmp_path, monkeypatch) -> None:
    from wifi_presence_mqtt.__main__ import main

    clock = Clock(monkeypatch)
    b = Bridge(make_config(tmp_path), FakeLink())
    b.restore()
    FakeAP.answers = {"hall": [(PHONE, -50)], "den": []}
    round_(b)
    state = json.loads((tmp_path / "data" / "state.json").read_text())
    assert state["version"] == 1 and PHONE in state["tracker"]["sightings"]
    assert state["published"] == {"devices": ["phone"], "access_points": ["den", "hall"]}
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    # The health file holds real time; the clock above is only the bridge's.
    health = json.loads((tmp_path / "data" / "health.json").read_text())
    health["at"] = datetime.now(UTC).isoformat()
    (tmp_path / "data" / "health.json").write_text(json.dumps(health))
    assert main(["--health"]) == 0
    health["mqtt_connected"] = False
    (tmp_path / "data" / "health.json").write_text(json.dumps(health))
    assert main(["--health"]) == 1
    health["mqtt_connected"] = True
    health["at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    (tmp_path / "data" / "health.json").write_text(json.dumps(health))
    assert main(["--health"]) == 1
    del clock


def test_triggered_polls_respect_the_cooldown(tmp_path, monkeypatch) -> None:
    Clock(monkeypatch)
    monkeypatch.setattr(bridge_module, "TRIGGERED_POLL_COOLDOWN", 0.2)
    b = Bridge(make_config(tmp_path, poll_interval=3600), FakeLink())
    b.restore()
    polls: list[float] = []

    async def counting_poll() -> None:
        polls.append(asyncio.get_running_loop().time())

    b.poll_once = counting_poll  # type: ignore[method-assign]

    async def main() -> None:
        task = asyncio.create_task(b.poll_loop())
        await asyncio.sleep(0.05)  # the first poll at start
        for _ in range(5):  # a burst of changes
            b.trigger.set()
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    assert len(polls) == 2  # at start, then one for the whole burst after the cooldown
    assert all(later - earlier >= 0.19 for earlier, later in zip(polls, polls[1:]))


def test_the_link_publishes_only_changes_and_everything_after_a_reconnect() -> None:
    link = MqttLink(MqttSettings(host="broker"))
    sent: list[tuple[str, str, bool]] = []

    class Client:
        async def publish(self, topic, payload, qos, retain):
            sent.append((topic, payload, retain))

    async def main() -> None:
        link.set_retained({"a": "1", "b": ""})
        link.publish_event("event", "{}")
        await link._flush(Client())
        assert sorted(sent) == [("a", "1", True), ("b", "", True), ("event", "{}", False)]
        sent.clear()
        link.set_retained({"a": "1", "b": ""})  # unchanged
        await link._flush(Client())
        assert sent == []
        link._dirty = set(link._desired)  # what run() does on each connect
        await link._flush(Client())
        assert sorted(sent) == [("a", "1", True), ("b", "", True)]

    asyncio.run(main())
