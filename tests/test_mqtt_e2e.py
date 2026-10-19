"""The whole bridge against a real MQTT broker (skipped without MQTT_TEST_HOST).

CI runs it with a Mosquitto service container; locally: MQTT_TEST_HOST=<broker> pytest.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

from wifi_ap_associations import DRIVERS
from wifi_presence_mqtt.bridge import run
from wifi_presence_mqtt.config import (
    AccessPointConfig,
    Config,
    DeviceConfig,
    MqttSettings,
    PresenceSettings,
)

from test_bridge import PHONE, FakeAP

HOST = os.environ.get("MQTT_TEST_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="no MQTT_TEST_HOST broker")


def test_the_bridge_publishes_retained_state_and_says_offline_when_stopped(tmp_path) -> None:
    import aiomqtt

    DRIVERS["fake_bridge"] = FakeAP
    FakeAP.answers = {"hall": [(PHONE, -50)]}
    prefix = f"wifi-presence-test-{uuid.uuid4().hex[:8]}"
    config = Config(
        mqtt=MqttSettings(host=HOST, port=int(os.environ.get("MQTT_TEST_PORT", 1883)), topic_prefix=prefix,
                          client_id=f"bridge-{prefix}"),
        presence=PresenceSettings(grace_period=60, poll_interval=10, instant_arrivals=False),
        access_points=[AccessPointConfig(id="hall", type="fake_bridge", settings={"host": "hall"}, area="Hall")],
        devices=[DeviceConfig(id="phone", mac=PHONE, name="Phone")],
        data_dir=str(tmp_path / "data"),
    )
    seen: dict[str, str] = {}

    async def main() -> None:
        bridge = asyncio.create_task(run(config))
        async with aiomqtt.Client(HOST, port=config.mqtt.port, identifier=f"probe-{prefix}") as client:
            await client.subscribe(f"{prefix}/#", qos=1)

            async def until(**expected: str) -> None:
                """Until every topic (suffix, with / as __) holds its payload; any order."""
                wanted = {f"{prefix}/{k.replace('__', '/')}": v for k, v in expected.items()}
                async for message in client.messages:
                    seen[str(message.topic)] = message.payload.decode()
                    if all(seen.get(t) == v for t, v in wanted.items()):
                        return

            await asyncio.wait_for(
                until(status="online", device__phone__state="home",
                      device__phone__access_point="hall", ap__hall__state="online"),
                20,
            )
            bridge.cancel()
            await asyncio.gather(bridge, return_exceptions=True)
            await asyncio.wait_for(until(status="offline"), 10)
            # Leave the broker clean: clear this test's retained topics.
            for topic in list(seen):
                await client.publish(topic, b"", qos=1, retain=True)

    try:
        if sys.platform == "win32":
            asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
        else:
            asyncio.run(main())
    finally:
        DRIVERS.pop("fake_bridge", None)
    assert (tmp_path / "data" / "state.json").exists()
