#!/bin/sh
# Started as root only to make the data directory the bridge user's (a bind-mounted
# ./data is created by Docker as root), then runs the bridge as that user. Started as
# another user (compose "user:"), it runs as that user and leaves /data alone.
set -e
DATA_DIR="${DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    if [ "$(stat -c %u "$DATA_DIR")" != "1000" ]; then
        chown -R 1000:1000 "$DATA_DIR"
    fi
    # The bridge user's own home, not root's (libraries look for ~/.ssh there).
    export HOME="$DATA_DIR" USER=bridge
    # Keep one capability: binding ports below 1024, for SNMP traps on UDP 162 (D-Link
    # instant arrivals). Nothing else; the container needs no privileges.
    exec setpriv --reuid=1000 --regid=1000 --clear-groups \
        --inh-caps=+net_bind_service --ambient-caps=+net_bind_service \
        wifi-presence-mqtt "$@"
fi
exec wifi-presence-mqtt "$@"
