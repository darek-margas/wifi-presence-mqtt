"""Configuration: environment plus YAML, every fault reported with its place, then exit."""

from __future__ import annotations

from pathlib import Path
import textwrap

import pytest

from wifi_presence_mqtt.__main__ import main
from wifi_presence_mqtt.config import ConfigError, load

GOOD = """
mqtt:
  host: broker.lan
presence:
  grace_period: 120
access_points:
  - id: hallway
    name: Hallway
    area: Hall
    type: openwrt_ssh
    host: 192.168.1.2
    username: root
    password: ${HALLWAY_PASSWORD}
  - id: upstairs
    type: mikrotik_capsman_ssh
    host: 192.168.1.1
    username: presence
    password: ${MIKROTIK_PASSWORD}
    cap: ap_upstairs
devices:
  - id: alice_phone
    name: Alice's phone
    mac: 5c-ad-ba-00-00-01
"""
ENV = {"HALLWAY_PASSWORD": "hall-secret", "MIKROTIK_PASSWORD": "mt-secret"}


def write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


def errors(tmp_path: Path, text: str, **env: str) -> list[str]:
    with pytest.raises(ConfigError) as err:
        load({"CONFIG_FILE": write(tmp_path, text), **ENV, **env})
    return err.value.errors


def test_a_good_config_loads(tmp_path: Path) -> None:
    config = load({"CONFIG_FILE": write(tmp_path, GOOD), **ENV})
    assert config.mqtt.host == "broker.lan" and config.mqtt.port == 1883
    assert config.mqtt.topic_prefix == "wifi-presence"
    assert config.presence.grace_period == 120 and config.presence.poll_interval == 60
    hallway, upstairs = config.access_points
    assert hallway.settings == {"host": "192.168.1.2", "port": 22, "username": "root", "password": "hall-secret"}
    assert (hallway.name, hallway.area, hallway.line) == ("Hallway", "Hall", 7)
    assert upstairs.settings["cap"] == "ap_upstairs" and upstairs.settings["password"] == "mt-secret"
    assert config.devices[0].mac == "5C:AD:BA:00:00:01"
    assert config.notes == []


def test_environment_overrides_the_file_and_says_so(tmp_path: Path) -> None:
    config = load({"CONFIG_FILE": write(tmp_path, GOOD), **ENV, "MQTT_HOST": "other", "POLL_INTERVAL": "30",
                   "INSTANT_ARRIVALS": "off", "MQTT_PASSWORD": "p"})
    assert config.mqtt.host == "other" and config.mqtt.password == "p"
    assert config.presence.poll_interval == 30 and config.presence.instant_arrivals is False
    assert config.notes == ["MQTT_HOST from the environment overrides mqtt.host in the file"]


def test_secrets_from_files(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("from-a-file\n", encoding="utf-8")
    config = load({"CONFIG_FILE": write(tmp_path, GOOD), "MIKROTIK_PASSWORD": "x",
                   "HALLWAY_PASSWORD_FILE": str(secret), "MQTT_PASSWORD_FILE": str(secret)})
    assert config.access_points[0].settings["password"] == "from-a-file"
    assert config.mqtt.password == "from-a-file"
    found = errors(tmp_path, GOOD, MQTT_PASSWORD_FILE=str(tmp_path / "missing"))
    assert any("environment MQTT_PASSWORD_FILE: cannot read" in e for e in found)


def test_yaml_syntax_error_has_line_and_column(tmp_path: Path) -> None:
    found = errors(tmp_path, """
        mqtt:
          host: broker
        access_points:
          - id: hallway
            type: openwrt_ssh
           host: 192.168.1.2
        """)
    assert len(found) == 1
    assert "line 7, column 4" in found[0] and "YAML syntax" in found[0] and "indentation" in found[0]


def test_duplicate_keys_are_a_syntax_error(tmp_path: Path) -> None:
    found = errors(tmp_path, GOOD.replace("  grace_period: 120", "  grace_period: 120\n  grace_period: 60"))
    assert len(found) == 1 and "line 6" in found[0] and "grace_period" in found[0]


def test_every_fault_is_reported_with_its_place(tmp_path: Path) -> None:
    found = errors(tmp_path, """
        mqtt:
          host: broker
          prot: 1883
        presence:
          poll_interval: sixty
          grace_period: 5
        controllers: {}
        access_points:
          - id: hallway
            type: openwrt_ssh
            host: 192.168.1.2
            username: root
            password: ${NOT_SET}
          - id: upstairs
            type: mikrotik_capsman_ssh
            host: 192.168.1.1
            username: presence
            password: x
            caps: ap_upstairs
          - id: hallway
            type: openwrt
            host: 192.168.1.3
          - id: bad id!
            type: openwrt_ssh
          - id: snmp
            type: cisco_wlc_snmp
            host: 10.0.0.1
            ap_name: AP01
            snmp_version: v4
        devices:
          - id: alice_phone
            mac: 5C:AD:BA:00:01
          - id: alice_phone
            mac: 5C:AD:BA:00:00:02
            nmae: Bob
        """)
    text = "\n".join(found)
    expected = [
        "line 4, column 3: mqtt: unknown key 'prot' (did you mean \"port\"?)",
        "line 6, column 3: presence.poll_interval: 'sixty' is not a whole number of seconds",
        "line 7, column 3: presence.grace_period: 5 is outside 30 to 86400",
        "line 8, column 1: unknown section 'controllers' (UniFi and Omada access points take their controller's login in each entry)",
        "line 14, column 5: access_points[0] (hallway): password: ${NOT_SET} is not set in the environment",
        "line 20, column 5: access_points[1] (upstairs): unknown key 'caps' for type mikrotik_capsman_ssh (did you mean \"cap\"?)",
        "access_points[1] (upstairs): missing required key 'cap' for type mikrotik_capsman_ssh",
        "line 21, column 5: access_points[2] (hallway): id 'hallway' is already used (line 10)",
        "access_points[2] (hallway): type 'openwrt' is not a known type (did you mean \"openwrt_ssh\"?)",
        "access_points[3] (bad id!): id 'bad id!' must be 1-64 letters, digits, - or _",
        "access_points[4] (snmp): snmp_version: 'v4' is not one of v2c, v3",
        "line 33, column 5: devices[0] (alice_phone): mac '5C:AD:BA:00:01' is not a MAC address",
        "line 36, column 5: devices[1] (alice_phone): unknown key 'nmae' (did you mean \"name\"?)",
        "line 34, column 5: devices[1] (alice_phone): id 'alice_phone' is already used (line 32)",
    ]
    for line in expected:
        assert line in text, line
    assert all(e.startswith("config error: ") for e in found)


def test_controller_types_need_their_login_and_the_ap(tmp_path: Path) -> None:
    found = errors(tmp_path, GOOD.replace("type: openwrt_ssh", "type: unifi_network"))
    assert any("missing required key 'ap_mac' for type unifi_network" in e for e in found)


def test_secrets_never_appear_in_messages(tmp_path: Path) -> None:
    # A malformed secret (a list where text belongs) is described, not shown.
    text = GOOD.replace("password: ${HALLWAY_PASSWORD}", "password: [hall-secret, more]")
    found = errors(tmp_path, text, MQTT_PASSWORD="mqtt-secret", MQTT_PORT="none")
    joined = "\n".join(found)
    assert "access_points[0] (hallway): password: the value must be text or a number" in joined
    for secret in ("hall-secret", "mt-secret", "mqtt-secret"):
        assert secret not in joined
    assert "environment MQTT_PORT: 'none' is not a whole number" in joined  # not a secret: shown


def test_the_same_access_point_twice(tmp_path: Path) -> None:
    found = errors(tmp_path, GOOD.replace("id: upstairs\n    type: mikrotik_capsman_ssh\n    host: 192.168.1.1",
                                          "id: upstairs\n    type: openwrt_ssh\n    host: 192.168.1.2")
                   .replace("    cap: ap_upstairs\n", ""))
    assert any("the same access point as access_points[0] (hallway)" in e for e in found)


def test_missing_pieces(tmp_path: Path) -> None:
    found = errors(tmp_path, "mqtt: {}\n")
    text = "\n".join(found)
    assert "the MQTT broker is not set" in text
    assert "access_points: at least one access point is needed" in text
    assert "devices: at least one device to track is needed" in text
    with pytest.raises(ConfigError) as err:
        load({"CONFIG_FILE": str(tmp_path / "nope.yaml")})
    assert "file not found" in err.value.errors[0]


def test_log_level_and_booleans(tmp_path: Path) -> None:
    found = errors(tmp_path, GOOD, LOG_LEVEL="loud", PUBLISH_ALL_CLIENTS="maybe")
    text = "\n".join(found)
    assert "environment LOG_LEVEL: 'loud' is not one of debug, info, warning, error" in text
    assert "environment PUBLISH_ALL_CLIENTS: 'maybe' is not true or false" in text


def test_check_config_exits_1_with_every_error_and_0_when_good(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CONFIG_FILE", write(tmp_path, GOOD.replace("grace_period: 120", "grace_period: x")))
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    assert main(["--check-config"]) == 1
    err = capsys.readouterr().err
    assert "presence.grace_period: 'x' is not a whole number of seconds" in err
    assert "1 configuration error; nothing was started." in err
    monkeypatch.setenv("CONFIG_FILE", write(tmp_path, GOOD))
    assert main(["--check-config"]) == 0
    assert "configuration OK: 2 access point(s), 1 device(s), broker broker.lan:1883" in capsys.readouterr().out


def test_a_unifi_access_point_with_its_controller_login(tmp_path: Path) -> None:
    text = GOOD.replace("""  - id: upstairs
    type: mikrotik_capsman_ssh
    host: 192.168.1.1
    username: presence
    password: ${MIKROTIK_PASSWORD}
    cap: ap_upstairs""", """  - id: porch
    type: unifi_network
    host: 192.168.1.1
    username: presence
    password: ${MIKROTIK_PASSWORD}
    ap_mac: 78:8a:20:00:00:10""")
    config = load({"CONFIG_FILE": write(tmp_path, text), **ENV})
    porch = config.access_points[1]
    assert porch.settings == {
        "host": "192.168.1.1", "port": 443, "username": "presence", "password": "mt-secret",
        "site": "default", "verify_ssl": "no", "ap_mac": "78:8a:20:00:00:10",
    }
