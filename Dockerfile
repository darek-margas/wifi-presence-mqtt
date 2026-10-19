# wifi-presence-mqtt: Wi-Fi presence over MQTT. Configuration in /config/config.yaml
# (mounted read-only), state in /data (a volume), credentials in the environment.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CONFIG_FILE=/config/config.yaml \
    DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN pip install . \
    && groupadd --system --gid 1000 bridge \
    && useradd --system --uid 1000 --gid 1000 --home-dir /data bridge \
    && mkdir -p /data /config \
    && chown bridge:bridge /data \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh

# Starts as root only to hand /data to the bridge user (a bind-mounted ./data is created
# by Docker as root), then drops to that user: the bridge itself never runs as root.
# See docker-entrypoint.sh.
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["wifi-presence-mqtt", "--health"]
ENTRYPOINT ["docker-entrypoint.sh"]
