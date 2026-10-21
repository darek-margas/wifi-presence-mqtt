# Changelog

## 0.1.0b7

- UniFi instant arrivals poll the controller anew instead of reusing its client list
  from up to 20 seconds before, and so does fast departure's deadline poll after a
  disconnect (library 0.14.1).
- Two entries for the same UniFi or Omada access point are refused as duplicates also
  when the MAC is written differently (`78-8a-…` and `78:8A:…`).
- Uses wifi-ap-associations 0.14.1.

## 0.1.0b6

- **UniFi and TP-Link Omada access points** (`unifi_network`, `omada_network`): the library
  now logs in to the controller itself, so the bridge reads them too. Each entry has the
  controller's `host`, `port` (443; 8443 for a self-hosted UniFi Network application,
  8043 for an Omada software controller), `username`, `password` (a local account),
  `site` and the AP's `ap_mac`; entries with the same controller share one login and one
  read. UniFi instant arrivals come from the controller's event stream. Omada is
  experimental.
- The controller sessions are closed when the bridge stops.
- Uses wifi-ap-associations 0.14.0.

## 0.1.0b5

- **Standalone TP-Link EAP over SNMP** (`tplink_eap_snmp`, experimental): EAP access
  points without an Omada controller, SNMP v2c or v3. Presence and which AP only: the
  EAP's client table has no SSID, band or signal, so with discovery its access points
  get no signal quality sensor.
- Uses wifi-ap-associations 0.13.1.

## 0.1.0b4

- **Instant arrivals for D-Link DAP** through SNMP traps: set the DAP's trap server to the
  bridge's host and publish the port (`ports: ["162:162/udp"]`, now in the example). The
  container still isn't privileged: it keeps only the `NET_BIND_SERVICE` capability when
  it drops to its own user, enough to open port 162 (checked: uid 1000, that one
  capability). `SNMP_TRAP_PORT` for another port. Tested in the container against a real
  DAP-2610: its traps arrive with the DAP's own address and are logged in full at debug.
- **Fast departure** (`FAST_DEPARTURE`, seconds, off by default): when an access point
  reports a client leaving, a device missing from it is away after that time instead of
  after the grace period; `away_since` follows it.
- Uses wifi-ap-associations 0.13.0.

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
