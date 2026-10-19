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
RUN pip install . \
    && useradd --system --uid 1000 --home-dir /data bridge \
    && mkdir -p /data /config \
    && chown bridge /data

USER bridge
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["wifi-presence-mqtt", "--health"]
ENTRYPOINT ["wifi-presence-mqtt"]
