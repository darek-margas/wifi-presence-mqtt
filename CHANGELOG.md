# Changelog

## 0.1.0b3

- **Home Assistant MQTT discovery** (`HA_DISCOVERY=true`, or `homeassistant.discovery`
  in the file; off by default): Home Assistant's MQTT integration creates, per tracked
  device, a device tracker (home / not_home / unknown, with the attributes) and sensors
  Access point, Area, Home since, Signal quality, Roams today and Short drops today; per
  access point Connectivity, Clients and Average signal quality on a device with its
  model and firmware; and the bridge's Status. Everything is unavailable while the
  bridge is offline. Entities of removed devices and access points, or all of them when
  discovery is turned off, are removed at the next start.
- README: the complete Docker Compose setup with its `.env`.

## 0.1.0b2

From running the image with the example compose file:

- **State is saved with a bind-mounted `./data`.** Docker creates that folder as root,
  and the bridge (uid 1000) couldn't write to it, so visits and today's counts didn't
  survive a restart. The container now starts as root only to hand `/data` to the bridge
  user, then runs the bridge as that user (never as root). Run with your own `user:`,
  it leaves `/data` alone and says clearly when it can't write there.
- **No false events after a restart from a state saved while presence was unknown**
  (e.g. stopped before any access point was read).
- Tested as a container (amd64 and arm64 builds; the example compose file with
  Mosquitto) against OpenWrt VMs: a roam between access points, a return 3 s after
  rejoining via instant arrivals, and `docker compose restart` keeping the visit with no
  event; Docker reports it healthy.

## 0.1.0b1

First beta: Wi-Fi presence over MQTT, without Home Assistant.

- Reads OpenWrt, MikroTik RouterOS (also CAP via CAPsMAN), D-Link DAP and Cisco
  Catalyst 9800 (SSH or SNMP) access points with the
  [wifi-ap-associations](https://github.com/darek-margas/wifi-ap-associations) library
  (0.12), and decides presence with its rules, the same as the Home Assistant
  integration: grace period, roaming to the stronger signal, visits, coverage counts.
- Publishes retained state per device (`state`, `access_point`, `home_since`,
  `away_since`, `attributes`) and per access point, plus `arrived` / `left` / `roamed`
  events; `unknown`, never "away", when no access point can be read past the grace
  period. Optional list of every client seen.
- Instant arrivals for MikroTik and OpenWrt (a poll as soon as a client joins, at most
  one per 5 seconds).
- Configuration from the environment (broker, settings, credentials, `_FILE` secrets) and
  a YAML file (access points, devices), checked completely before starting: every
  problem is reported with file, line, column and entry, then the program exits with
  status 1. `--check-config` checks only.
- State in `/data`: arrival times and today's counts survive a restart; a restart raises
  no false events. Topics of removed devices and access points are cleared.
- Container for amd64 and arm64 on ghcr.io, non-root, with a health check.
- Tested against OpenWrt 25.12.5 and 22.03.7 VMs and Mosquitto 2.0: arrival in 2 s via
  instant arrivals, departure after the grace period, clean stop and restart.
