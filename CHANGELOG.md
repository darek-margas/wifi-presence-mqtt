# Changelog

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
