"""Configuration: environment variables for the basics, a YAML file for the details.

Everything is read and checked before anything connects. Every problem found is
collected with where it is (file, line, column, the entry's path and id) and what is
wrong; load() raises ConfigError with all of them, and the program logs them and exits.
A configuration that loads is complete: nothing is guessed later.

Secrets (passwords, communities, keys) never appear in a message, not even when the
value itself is what's wrong.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import difflib
import os
from pathlib import Path
import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import MarkedYAMLError, YAMLError

from wifi_ap_associations import DRIVERS, AccessPointDriver, normalize_mac

DEFAULT_CONFIG_FILE = "/config/config.yaml"
DEFAULT_DATA_DIR = "/data"
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Types whose access points are read through a controller connection; the bridge makes
# that connection itself from a later beta on.


class ConfigError(Exception):
    """The configuration can't be used; `errors` lists every problem found."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass(frozen=True, slots=True)
class MqttSettings:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    topic_prefix: str = "wifi-presence"
    client_id: str = "wifi-presence-mqtt"
    tls: bool = False
    ca_file: str | None = None


@dataclass(frozen=True, slots=True)
class PresenceSettings:
    grace_period: int = 180
    poll_interval: int = 60
    instant_arrivals: bool = True
    publish_all_clients: bool = False
    fast_departure: int = 0  # seconds after a reported leave; 0 = off
    snmp_trap_port: int = 162  # D-Link DAP instant arrivals


@dataclass(frozen=True, slots=True)
class HomeAssistantSettings:
    discovery: bool = False
    discovery_prefix: str = "homeassistant"


@dataclass(frozen=True, slots=True)
class AccessPointConfig:
    id: str
    type: str
    settings: dict[str, Any]  # the driver's FIELDS, as the driver takes them
    name: str | None = None
    area: str | None = None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    id: str
    mac: str  # normalized: upper case with colons
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Config:
    mqtt: MqttSettings
    presence: PresenceSettings
    access_points: list[AccessPointConfig]
    devices: list[DeviceConfig]
    homeassistant: HomeAssistantSettings = field(default_factory=HomeAssistantSettings)
    data_dir: str = DEFAULT_DATA_DIR
    log_level: str = "info"
    # What the environment overrode in the file, logged once at start.
    notes: list[str] = field(default_factory=list)


# --- one setting: where it may come from, and how it is checked ---------------------


@dataclass(frozen=True, slots=True)
class _Setting:
    section: str  # top-level YAML section
    key: str  # key in that section
    env: str  # environment variable
    kind: str  # "str", "int", "bool"
    default: Any = None
    secret: bool = False
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] | None = None


_SETTINGS = (
    _Setting("mqtt", "host", "MQTT_HOST", "str"),
    _Setting("mqtt", "port", "MQTT_PORT", "int", 1883, minimum=1, maximum=65535),
    _Setting("mqtt", "username", "MQTT_USERNAME", "str"),
    _Setting("mqtt", "password", "MQTT_PASSWORD", "str", secret=True),
    _Setting("mqtt", "topic_prefix", "MQTT_TOPIC_PREFIX", "str", "wifi-presence"),
    _Setting("mqtt", "client_id", "MQTT_CLIENT_ID", "str", "wifi-presence-mqtt"),
    _Setting("mqtt", "tls", "MQTT_TLS", "bool", False),
    _Setting("mqtt", "ca_file", "MQTT_CA_FILE", "str"),
    _Setting("presence", "grace_period", "GRACE_PERIOD", "int", 180, minimum=30, maximum=86400),
    _Setting("presence", "poll_interval", "POLL_INTERVAL", "int", 60, minimum=10, maximum=3600),
    _Setting("presence", "instant_arrivals", "INSTANT_ARRIVALS", "bool", True),
    _Setting("presence", "publish_all_clients", "PUBLISH_ALL_CLIENTS", "bool", False),
    _Setting("presence", "fast_departure", "FAST_DEPARTURE", "int", 0, minimum=0, maximum=3600),
    _Setting("presence", "snmp_trap_port", "SNMP_TRAP_PORT", "int", 162, minimum=1, maximum=65535),
    _Setting("homeassistant", "discovery", "HA_DISCOVERY", "bool", False),
    _Setting("homeassistant", "discovery_prefix", "HA_DISCOVERY_PREFIX", "str", "homeassistant"),
)
_SECTIONS = ("mqtt", "presence", "homeassistant")
_SECTION_KEYS = {
    section: {s.key for s in _SETTINGS if s.section == section} for section in _SECTIONS
}
_TOP_LEVEL = (*_SECTIONS, "access_points", "devices")
_AP_OWN_KEYS = ("id", "type", "name", "area")
_DEVICE_KEYS = ("id", "name", "mac")
_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}
LOG_LEVELS = ("debug", "info", "warning", "error")


def _suggest(key: str, valid: Any) -> str:
    close = difflib.get_close_matches(str(key), [str(v) for v in valid], n=1, cutoff=0.6)
    return f' (did you mean "{close[0]}"?)' if close else ""


class _Reader:
    """Collects errors with their place in the file."""

    def __init__(self, path: str, env: Mapping[str, str]) -> None:
        self.path = path
        self.env = env
        self.errors: list[str] = []
        self.secret_values: set[str] = set()

    # -- places --

    def where(self, node: Any = None, key: Any = None, index: int | None = None) -> str:
        line = col = None
        try:
            if isinstance(node, CommentedMap) and key is not None and key in node:
                line, col = node.lc.key(key)
            elif isinstance(node, CommentedSeq) and index is not None:
                line, col = node.lc.item(index)
            elif isinstance(node, (CommentedMap, CommentedSeq)):
                line, col = node.lc.line, node.lc.col
        except (AttributeError, KeyError, IndexError, TypeError):
            pass
        if line is None:
            return self.path
        return f"{self.path} line {line + 1}, column {col + 1}"

    def line_of(self, node: Any, key: Any) -> str:
        """'line N' of a key, for pointing at an earlier entry."""
        try:
            return f"line {node.lc.key(key)[0] + 1}"
        except (AttributeError, KeyError, TypeError):
            return "an earlier entry"

    def error(self, place: str, message: str) -> None:
        self.errors.append(f"config error: {place}: {message}")

    # -- environment --

    def env_value(self, name: str) -> str | None:
        """A variable, or the content of the file its NAME_FILE variable names."""
        if name in self.env:
            return self.env[name]
        file_name = self.env.get(f"{name}_FILE")
        if file_name is None:
            return None
        try:
            return Path(file_name).read_text(encoding="utf-8").strip()
        except OSError as err:
            self.error(f"environment {name}_FILE", f"cannot read {file_name}: {err.strerror}")
            return None

    def substitute(self, value: Any, place: str, secret: bool, label: str = "") -> Any:
        """${NAME} in a string from the file, replaced by that environment variable."""
        if not isinstance(value, str):
            return value

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            found = self.env_value(name)
            if found is None:
                self.error(place, f"{label}${{{name}}} is not set in the environment")
                return ""
            if secret:
                self.secret_values.add(found)
            return found

        return _ENV_REF.sub(replace, value)

    # -- values --

    def convert(self, setting: _Setting, value: Any, place: str, label: str) -> Any:
        shown = "the value" if setting.secret else repr(value)
        label = f"{label}: " if label else ""
        if setting.kind == "str":
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                self.error(place, f"{label}{shown} is not text")
                return None
            return str(value)
        if setting.kind == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in _TRUE:
                return True
            if text in _FALSE:
                return False
            self.error(place, f"{label}{shown} is not true or false")
            return None
        # int
        if isinstance(value, bool):
            self.error(place, f"{label}{shown} is not a whole number")
            return None
        try:
            number = int(str(value).strip())
        except ValueError:
            unit = " of seconds" if setting.section == "presence" else ""
            self.error(place, f"{label}{shown} is not a whole number{unit}")
            return None
        if setting.minimum is not None and number < setting.minimum or (
            setting.maximum is not None and number > setting.maximum
        ):
            self.error(place, f"{label}{number} is outside {setting.minimum} to {setting.maximum}")
            return None
        return number


def _load_yaml(path: str, reader: _Reader) -> CommentedMap | None:
    file = Path(path)
    if not file.exists():
        reader.error(path, "file not found (mount it at this path, or set CONFIG_FILE)")
        return None
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        reader.error(path, f"cannot read: {err}")
        return None
    yaml = YAML(typ="rt")
    yaml.allow_duplicate_keys = False
    try:
        data = yaml.load(text)
    except MarkedYAMLError as err:
        mark = err.problem_mark or err.context_mark
        place = f"{path} line {mark.line + 1}, column {mark.column + 1}" if mark else path
        problem = err.problem or err.context or "invalid YAML"
        hint = ""
        if "block" in problem or "mapping values" in problem or "indent" in problem:
            hint = "; check the indentation of this line and the one above"
        reader.error(place, f"{problem} (YAML syntax{hint})")
        return None
    except YAMLError as err:
        reader.error(path, f"{err} (YAML syntax)")
        return None
    if data is None:
        return CommentedMap()
    if not isinstance(data, CommentedMap):
        reader.error(path, "the file must be a mapping of sections (mqtt, presence, homeassistant, access_points, devices)")
        return None
    return data


def _section(reader: _Reader, data: CommentedMap, name: str, kind: type) -> Any:
    value = data.get(name)
    if value is None:
        return kind()
    expected = "a mapping of keys" if kind is CommentedMap else "a list"
    if not isinstance(value, kind):
        reader.error(reader.where(data, name), f"{name}: must be {expected}")
        return kind()
    return value


def _settings(reader: _Reader, data: CommentedMap) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """mqtt, presence and homeassistant: environment over file over defaults."""
    values: dict[str, dict[str, Any]] = {name: {} for name in _SECTIONS}
    notes: list[str] = []
    sections = {name: _section(reader, data, name, CommentedMap) for name in values}
    for name, section in sections.items():
        for key in section:
            if key not in _SECTION_KEYS[name]:
                reader.error(
                    reader.where(section, key),
                    f"{name}: unknown key {key!r}{_suggest(key, _SECTION_KEYS[name])}",
                )
    for setting in _SETTINGS:
        section = sections[setting.section]
        label = f"{setting.section}.{setting.key}"
        from_env = reader.env_value(setting.env)
        if from_env is not None:
            if setting.key in section:
                notes.append(f"{setting.env} from the environment overrides {label} in the file")
            value = reader.convert(setting, from_env, f"environment {setting.env}", "")
        elif setting.key in section:
            place = reader.where(section, setting.key)
            raw = reader.substitute(section[setting.key], place, setting.secret, f"{label}: ")
            value = reader.convert(setting, raw, place, label)
        else:
            value = setting.default
        if setting.secret and isinstance(value, str) and value:
            reader.secret_values.add(value)
        values[setting.section][setting.key] = value
    if not values["mqtt"].get("host"):
        reader.error(reader.path, "the MQTT broker is not set: MQTT_HOST, or mqtt.host in the file")
    return values, notes


def _access_points(reader: _Reader, data: CommentedMap) -> list[AccessPointConfig]:
    items = _section(reader, data, "access_points", CommentedSeq)
    if not items:
        reader.error(reader.where(data, "access_points") if "access_points" in data else reader.path,
                     "access_points: at least one access point is needed")
    result: list[AccessPointConfig] = []
    seen_ids: dict[str, str] = {}
    seen_aps: dict[tuple[str, ...], str] = {}
    for index, item in enumerate(items):
        path = f"access_points[{index}]"
        place = reader.where(items, index=index)
        if not isinstance(item, CommentedMap):
            reader.error(place, f"{path}: must be a mapping of keys (id, type, ...)")
            continue
        ap_id = item.get("id")
        label = f"{path} ({ap_id})" if isinstance(ap_id, str) else path
        ok = True
        if not isinstance(ap_id, str) or not ID_PATTERN.match(ap_id):
            reader.error(reader.where(item, "id") if "id" in item else place,
                         f"{label}: id {ap_id!r} must be 1-64 letters, digits, - or _" if ap_id is not None
                         else f"{label}: missing required key 'id'")
            ok = False
        elif ap_id in seen_ids:
            reader.error(reader.where(item, "id"), f"{label}: id {ap_id!r} is already used ({seen_ids[ap_id]})")
            ok = False
        else:
            seen_ids[ap_id] = reader.line_of(item, "id")
        type_ = item.get("type")
        if type_ is None:
            reader.error(place, f"{label}: missing required key 'type'")
            continue
        driver_cls = DRIVERS.get(str(type_))
        if driver_cls is None:
            known = sorted(DRIVERS)
            reader.error(reader.where(item, "type"),
                         f"{label}: type {type_!r} is not a known type{_suggest(type_, known)} "
                         f"(known: {', '.join(known)})")
            continue
        settings = _driver_settings(reader, item, label, driver_cls)
        if settings is None or not ok:
            continue
        for key in ("name", "area"):
            if key in item and not isinstance(item[key], str):
                reader.error(reader.where(item, key), f"{label}: {key} must be text")
                ok = False
        try:
            driver_cls(dict(settings))
        except (KeyError, ValueError, TypeError) as err:
            reader.error(place, f"{label}: the {type_} driver rejects these settings: {_safe(err, reader)}")
            continue
        identity = driver_cls.identity(settings)
        if identity is not None:
            if identity in seen_aps:
                reader.error(place, f"{label}: the same access point as {seen_aps[identity]}")
                continue
            seen_aps[identity] = label
        if ok:
            result.append(
                AccessPointConfig(
                    id=ap_id, type=str(type_), settings=settings,
                    name=item.get("name"), area=item.get("area"), line=item.lc.line + 1,
                )
            )
    return result


def _driver_settings(
    reader: _Reader, item: CommentedMap, label: str, driver_cls: type[AccessPointDriver]
) -> dict[str, Any] | None:
    """The driver's FIELDS from the entry: required ones present, values checked."""
    fields = {f.key: f for f in driver_cls.FIELDS}
    ok = True
    for key in item:
        if key not in fields and key not in _AP_OWN_KEYS:
            valid = [*fields, *_AP_OWN_KEYS]
            reader.error(reader.where(item, key),
                         f"{label}: unknown key {key!r} for type {driver_cls.TYPE}{_suggest(key, valid)} "
                         f"(its keys: {', '.join(fields)})")
            ok = False
    settings: dict[str, Any] = {}
    for key, spec in fields.items():
        if key not in item:
            if spec.default is not None:
                settings[key] = spec.default
            elif spec.optional:
                settings[key] = ""
            else:
                reader.error(reader.where(item), f"{label}: missing required key {key!r} for type {driver_cls.TYPE}")
                ok = False
            continue
        place = reader.where(item, key)
        value = reader.substitute(item[key], place, spec.secret, f"{label}: {key}: ")
        shown = "the value" if spec.secret else repr(value)
        if value is None or isinstance(value, (list, dict, bool)) or isinstance(value, float):
            reader.error(place, f"{label}: {key}: {shown} must be text or a number")
            ok = False
            continue
        if isinstance(spec.default, int) and not isinstance(spec.default, bool):
            try:
                value = int(str(value))
            except ValueError:
                reader.error(place, f"{label}: {key}: {shown} is not a whole number")
                ok = False
                continue
        else:
            value = str(value)
            if spec.secret and value:
                reader.secret_values.add(value)
            if not value and not spec.optional and not spec.secret:
                reader.error(place, f"{label}: {key} is empty")
                ok = False
                continue
        if spec.choices is not None and value not in spec.choices:
            reader.error(place, f"{label}: {key}: {shown} is not one of {', '.join(spec.choices)}"
                         + ("" if spec.secret else _suggest(value, spec.choices)))
            ok = False
            continue
        settings[key] = value
    return settings if ok else None


def _devices(reader: _Reader, data: CommentedMap) -> list[DeviceConfig]:
    items = _section(reader, data, "devices", CommentedSeq)
    if not items:
        reader.error(reader.where(data, "devices") if "devices" in data else reader.path,
                     "devices: at least one device to track is needed")
    result: list[DeviceConfig] = []
    seen_ids: dict[str, str] = {}
    seen_macs: dict[str, str] = {}
    for index, item in enumerate(items):
        path = f"devices[{index}]"
        place = reader.where(items, index=index)
        if not isinstance(item, CommentedMap):
            reader.error(place, f"{path}: must be a mapping of keys (id, mac, name)")
            continue
        dev_id = item.get("id")
        label = f"{path} ({dev_id})" if isinstance(dev_id, str) else path
        ok = True
        for key in item:
            if key not in _DEVICE_KEYS:
                reader.error(reader.where(item, key), f"{label}: unknown key {key!r}{_suggest(key, _DEVICE_KEYS)}")
                ok = False
        if not isinstance(dev_id, str) or not ID_PATTERN.match(dev_id):
            reader.error(reader.where(item, "id") if "id" in item else place,
                         f"{label}: id {dev_id!r} must be 1-64 letters, digits, - or _" if dev_id is not None
                         else f"{label}: missing required key 'id'")
            ok = False
        elif dev_id in seen_ids:
            reader.error(reader.where(item, "id"), f"{label}: id {dev_id!r} is already used ({seen_ids[dev_id]})")
            ok = False
        else:
            seen_ids[dev_id] = reader.line_of(item, "id")
        raw_mac = item.get("mac")
        mac = None
        if raw_mac is None:
            reader.error(place, f"{label}: missing required key 'mac'")
            ok = False
        else:
            try:
                mac = normalize_mac(
                    str(reader.substitute(raw_mac, reader.where(item, "mac"), False, f"{label}: mac: "))
                )
            except ValueError:
                reader.error(reader.where(item, "mac"), f"{label}: mac {str(raw_mac)!r} is not a MAC address")
                ok = False
        if mac is not None:
            if mac in seen_macs:
                reader.error(reader.where(item, "mac"), f"{label}: mac {mac} is already {seen_macs[mac]}")
                ok = False
            else:
                seen_macs[mac] = label
        if "name" in item and not isinstance(item["name"], str):
            reader.error(reader.where(item, "name"), f"{label}: name must be text")
            ok = False
        if ok and mac is not None:
            result.append(DeviceConfig(id=dev_id, mac=mac, name=item.get("name")))
    return result


def _safe(err: BaseException, reader: _Reader) -> str:
    text = str(err) or type(err).__name__
    for secret in reader.secret_values:
        if secret:
            text = text.replace(secret, "***")
    return text


def load(env: Mapping[str, str] | None = None) -> Config:
    """Read the environment and the configuration file; ConfigError lists every problem."""
    env = dict(os.environ if env is None else env)
    path = env.get("CONFIG_FILE", DEFAULT_CONFIG_FILE)
    reader = _Reader(path, env)
    data = _load_yaml(path, reader)
    if data is None:
        raise ConfigError(reader.errors)
    for key in data:
        if key not in _TOP_LEVEL:
            hint = (" (UniFi and Omada access points take their controller's login in each "
                    "entry)" if key == "controllers" else _suggest(key, _TOP_LEVEL))
            reader.error(reader.where(data, key), f"unknown section {key!r}{hint}")
    values, notes = _settings(reader, data)
    access_points = _access_points(reader, data)
    devices = _devices(reader, data)
    log_level = (reader.env_value("LOG_LEVEL") or "info").strip().lower()
    if log_level not in LOG_LEVELS:
        reader.error("environment LOG_LEVEL", f"{log_level!r} is not one of {', '.join(LOG_LEVELS)}")
    if reader.errors:
        raise ConfigError(reader.errors)
    return Config(
        mqtt=MqttSettings(**values["mqtt"]),
        presence=PresenceSettings(**values["presence"]),
        homeassistant=HomeAssistantSettings(**values["homeassistant"]),
        access_points=access_points,
        devices=devices,
        data_dir=env.get("DATA_DIR", DEFAULT_DATA_DIR),
        log_level=log_level,
        notes=notes,
    )
