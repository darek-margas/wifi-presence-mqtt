# wifi-presence-mqtt

**Who is home, and in which room, published over MQTT, from your Wi-Fi access points'
own lists of connected clients.** For Node-RED, openHAB, ioBroker, Domoticz, Home
Assistant or anything else that speaks MQTT. Runs as a small container; nothing to
install on the access points.

> **Beta (0.1.0b1).** Works with OpenWrt, MikroTik RouterOS (also CAPsMAN), D-Link DAP
> and Cisco Catalyst 9800 (SSH or SNMP). UniFi and TP-Link Omada come in a later beta.
> Feedback welcome in the [issues](https://github.com/darek-margas/wifi-presence-mqtt/issues).

Why access points: a phone stays connected to Wi-Fi while it sleeps, so it doesn't
"leave" every few minutes the way ping or ARP based presence does, and the access
point it is on tells you the room. The rules that decide home and away are the same as
the Home Assistant integration
[Wi-Fi Association Presence](https://github.com/darek-margas/wifi-association-presence),
from the shared library
[wifi-ap-associations](https://github.com/darek-margas/wifi-ap-associations):

- A device is **home** while an access point lists it, and **away** only after it has
  been missing for the **grace period** (3 minutes by default). Phones drop off and
  rejoin all the time; that never makes them leave.
- A device on two access points is placed on the one with the stronger signal.
- If no access point can be read for longer than the grace period, presence becomes
  **unknown**, never "everyone left".
- **Instant arrivals:** MikroTik and OpenWrt report a client joining as it happens, and
  the bridge polls at once: an arrival shows in a couple of seconds instead of up to a
  minute.
- Arrival times and today's counts survive a restart.

## Quick start

1. Copy [`examples/docker-compose.yml`](examples/docker-compose.yml),
   [`examples/config.yaml`](examples/config.yaml) and
   [`examples/env.example`](examples/env.example) (as `.env`) into a folder.
2. Edit `config.yaml`: your access points and the devices to track. Put passwords in
   `.env`; the file refers to them as `${NAME}`.
3. Check it before starting:

   ```bash
   docker run --rm -v ./config.yaml:/config/config.yaml:ro --env-file .env ghcr.io/darek-margas/wifi-presence-mqtt:beta --check-config
   ```

4. `docker compose up -d`, then watch the topics:

   ```bash
   mosquitto_sub -v -t 'wifi-presence/#'
   ```

The image is built for `amd64` and `arm64` (Raspberry Pi 4/5). It only makes outgoing
connections: to the access points (SSH 22, SNMP 161) and to the broker.

## Configuration

Two parts: **environment variables** for the broker, the presence settings and
credentials; a **YAML file** for the access points and devices. A setting given in both
takes the environment's value (logged once at start).

| Environment variable | File key | Default | |
|---|---|---|---|
| `MQTT_HOST` | `mqtt.host` | (required) | broker address |
| `MQTT_PORT` | `mqtt.port` | `1883` | |
| `MQTT_USERNAME`, `MQTT_PASSWORD` | `mqtt.username`, `mqtt.password` | none | |
| `MQTT_TOPIC_PREFIX` | `mqtt.topic_prefix` | `wifi-presence` | |
| `MQTT_CLIENT_ID` | `mqtt.client_id` | `wifi-presence-mqtt` | |
| `MQTT_TLS`, `MQTT_CA_FILE` | `mqtt.tls`, `mqtt.ca_file` | off | |
| `GRACE_PERIOD` | `presence.grace_period` | `180` | seconds unseen before away (30-86400) |
| `POLL_INTERVAL` | `presence.poll_interval` | `60` | seconds between reads (10-3600) |
| `INSTANT_ARRIVALS` | `presence.instant_arrivals` | `true` | |
| `PUBLISH_ALL_CLIENTS` | `presence.publish_all_clients` | `false` | also publish every client seen |
| `LOG_LEVEL` | | `info` | `debug` shows each line the access points report |
| `TZ` | | UTC | your time zone: "today" counts reset at local midnight |
| `CONFIG_FILE`, `DATA_DIR` | | `/config/config.yaml`, `/data` | |

Any variable `X` can instead come from a file: `X_FILE=/run/secrets/x` (Docker
secrets). `${X}` in the YAML file is replaced by that variable.

The YAML file, in full in [`examples/config.yaml`](examples/config.yaml):

```yaml
access_points:
  - id: hallway                # used in topics: letters, digits, - and _
    name: Hallway              # optional, else what the access point reports
    area: Hallway              # optional, published with the devices on it
    type: openwrt_ssh
    host: 192.168.1.2
    username: root
    password: ${HALLWAY_PASSWORD}

devices:
  - id: alice_phone            # used in topics; stays when the MAC changes
    name: Alice's phone
    mac: 5C:AD:BA:00:00:01
```

Types and their keys (the same settings as the integration's setup form):

| `type` | Keys | Guide |
|---|---|---|
| `openwrt_ssh` | `host`, `port` (22), `username` (root), `password` | [OpenWrt](https://github.com/darek-margas/wifi-association-presence/wiki/OpenWrt) |
| `mikrotik_ssh` | `host`, `port` (22), `username`, `password` | [MikroTik](https://github.com/darek-margas/wifi-association-presence/wiki/MikroTik-RouterOS) |
| `mikrotik_capsman_ssh` | `host`, `port`, `username`, `password`, `cap` (one entry per CAP) | [MikroTik](https://github.com/darek-margas/wifi-association-presence/wiki/MikroTik-RouterOS#capsman) |
| `dlink_dap_ssh` | `host`, `port`, `username`, `password` | [D-Link DAP](https://github.com/darek-margas/wifi-association-presence/wiki/D-Link-DAP) |
| `cisco_wlc_ssh` | `host`, `port`, `username`, `password`, `ap_name` (one entry per AP) | [Cisco 9800](https://github.com/darek-margas/wifi-association-presence/wiki/Cisco-Catalyst-9800) |
| `cisco_wlc_snmp` | `host`, `port` (161), `ap_name`, `snmp_version` (v2c / v3), `community`, or `username`, `auth_key`, `auth_protocol`, `priv_key`, `priv_protocol` | [Cisco 9800](https://github.com/darek-margas/wifi-association-presence/wiki/Cisco-Catalyst-9800#over-snmp) |

The guides are the integration's wiki: the access point side (accounts, permissions) is
the same.

### Mistakes are reported, all of them, and nothing starts

The whole configuration is checked before anything connects. Any problem stops the
container with **exit status 1** and a list of everything wrong, each with its place:

```
config error: /config/config.yaml line 14, column 5: access_points[1] (upstairs): unknown key 'caps' for type mikrotik_capsman_ssh (did you mean "cap"?)
config error: /config/config.yaml line 20, column 5: access_points[2] (hallway): type 'openwrt' is not a known type (did you mean "openwrt_ssh"?)
config error: /config/config.yaml line 33, column 5: devices[0] (alice_phone): mac '5C:AD:BA:00:01' is not a MAC address
config error: /config/config.yaml line 12, column 5: access_points[0] (hallway): password: ${HALLWAY_PASSWORD} is not set in the environment
config error: environment POLL_INTERVAL: 'sixty' is not a whole number of seconds
5 configuration errors; nothing was started.
```

Passwords never appear in these messages. Problems that aren't the configuration's (an
access point unreachable, a wrong password on one, the broker down) don't stop it: they
are logged and retried, and the access point shows `offline`.

## Topics

Under the prefix (`wifi-presence` by default). States are **retained**, so a client that
subscribes later gets them at once; events are not.

| Topic | Payload |
|---|---|
| `wifi-presence/status` | `online` / `offline` (also the last will) |
| `wifi-presence/device/<id>/state` | `home`, `not_home` or `unknown` |
| `wifi-presence/device/<id>/access_point` | id of the access point it is on; empty while away |
| `wifi-presence/device/<id>/home_since` | when the current visit started (ISO time); empty while away |
| `wifi-presence/device/<id>/away_since` | when it was declared away; empty while home |
| `wifi-presence/device/<id>/attributes` | JSON: `mac`, `name`, `access_point`, `access_point_name`, `area`, `ssid`, `band`, `signal`, `signal_unit`, `quality` (0-100), `last_seen`, `home_since`, `roams_today`, `short_drops_today`, `late_roams_today` |
| `wifi-presence/ap/<id>/state` | `online` / `offline` |
| `wifi-presence/ap/<id>/attributes` | JSON: `name`, `type`, `area`, `model`, `firmware`, `uptime_seconds`, `last_boot`, `cpu_percent`, `memory_percent`, `clients`, `clients_by_band`, `average_signal_quality`, `weak_clients`, or `error` while offline |
| `wifi-presence/event` | JSON, not retained: `{"event": "arrived" / "left" / "roamed", "device", "name", "access_point", "from", "at"}` |
| `wifi-presence/clients` | JSON list of every client seen (only with `PUBLISH_ALL_CLIENTS`) |

`left` comes when the grace period runs out, not when the phone disconnects. A device
going to or from `unknown` (an outage, the first start) raises no event: it wasn't seen
arriving or leaving. Topics use the ids from the configuration, so a new phone or a
private Wi-Fi address only changes the `mac:` line, not your flows. A device or access
point removed from the configuration has its topics cleared at the next start.

The `state`, `access_point`, `home_since` and `away_since` topics are plain text, so a
Node-RED `mqtt in` node works without parsing JSON. An example flow:
[`examples/node-red-flow.json`](examples/node-red-flow.json) (Menu → Import).

## Troubleshooting

- `--check-config` checks a change without restarting anything.
- `LOG_LEVEL=debug` shows each line an access point's change feed reports, whether it
  asked for a poll, and how long each poll took.
- An access point that can't be read logs `Cannot read access point <id>: <reason>`
  once, and `readable again` when it recovers; its `ap/<id>/attributes` holds the
  error.
- The health check (`docker ps` shows it) fails when the last poll round is more than
  three intervals old, or the broker isn't connected.

## Security

The SSH types log in with a password and don't verify the access point's host key (it
changes when an access point is reset), and only run read-only commands. Use a trusted
network, a dedicated read-only account where the access point allows it (the
integration's wiki shows how per type), and keep passwords in the environment or Docker
secrets rather than in the file. Only the devices you list are published, unless
`PUBLISH_ALL_CLIENTS` is on.

## Development

```bash
pip install -e . pytest ruff
ruff check --select E9,F src tests
python -m pytest                      # MQTT_TEST_HOST=<broker> also runs the end-to-end test
```

Releasing: set `__version__` in `src/wifi_presence_mqtt/__init__.py`, add a
`## <version>` section to `CHANGELOG.md`, push to `main`. The *publish* workflow runs
the tests, builds the image for amd64 and arm64, pushes it to ghcr.io (`beta` for
pre-releases, `latest` otherwise), and creates the tag and GitHub release.

Design: [docs/design/mqtt-bridge.md](https://github.com/darek-margas/wifi-association-presence/blob/main/docs/design/mqtt-bridge.md)
in the integration's repository.

## License

GPL-3.0-only, like the wifi-ap-associations library it is built on.
