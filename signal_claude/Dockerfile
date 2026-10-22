# Local Home Assistant OS app: amd64 only. Build on the Home Assistant box.
# Base image pinned by digest (node:22-trixie-slim, multi-arch index).
FROM node:22-trixie-slim@sha256:8eb619e402eaede1748a3c8173bffab391592812e85136452f781741c7242ddf

ARG BUILD_VERSION=0.8.2
ARG BUILD_ARCH=amd64
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv curl ca-certificates util-linux openjdk-25-jre-headless tzdata \
    && rm -rf /var/lib/apt/lists/*

# One unprivileged user per process (node, uid 1000, comes with the image and
# runs Claude). See run.sh.
RUN groupadd --system signal && useradd --system --gid signal --home-dir /data/signal \
      --no-create-home --shell /usr/sbin/nologin signal && \
    groupadd --system homebot && useradd --system --gid homebot --home-dir /data/homebot \
      --no-create-home --shell /usr/sbin/nologin homebot

# signal-cli's Java distribution on Debian's Java 25. Not the Linux-native
# build: that one requires x86-64-v3 CPUs and fails on older HA boxes with
# "CPU ISA level is lower than required". The checksum matches the digest
# GitHub publishes for the v0.14.9 release asset.
ARG SIGNAL_CLI_VERSION=0.14.9
ARG SIGNAL_CLI_SHA256=c32b87f587198cbd277c9d71228716ee6047ee2b63e8e88728e8280d9407d65a
RUN set -eux; \
    curl -fsSL "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" -o /tmp/signal-cli.tar.gz; \
    echo "${SIGNAL_CLI_SHA256}  /tmp/signal-cli.tar.gz" | sha256sum -c -; \
    tar xzf /tmp/signal-cli.tar.gz -C /opt; \
    ln -s "/opt/signal-cli-${SIGNAL_CLI_VERSION}" /opt/signal-cli; \
    ln -s /opt/signal-cli/bin/signal-cli /usr/local/bin/signal-cli; \
    rm /tmp/signal-cli.tar.gz; \
    signal-cli --version

# Pinned: the CLI flags and JSON envelope were verified with this version.
ARG CLAUDE_CODE_VERSION=2.1.306
RUN npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" && npm cache clean --force

WORKDIR /opt/homebot
COPY requirements.lock /opt/homebot/
RUN python3 -m venv /opt/homebot/.venv && \
    /opt/homebot/.venv/bin/pip install --no-cache-dir --retries 5 --no-deps -r requirements.lock
COPY bot/ /opt/homebot/bot/
COPY scripts/ /opt/homebot/scripts/
COPY run.sh /run.sh
RUN chmod 755 /run.sh

# Supervisor builds on the HA box itself, so this exercises libsignal's native
# code on the CPU that will run it.
RUN java -cp "/opt/signal-cli/lib/*" scripts/LibsignalCheck.java

# Fail the build if the test suite fails in the exact runtime environment.
COPY tests/ /opt/homebot/tests/
RUN /opt/homebot/.venv/bin/python -m unittest discover -s tests && rm -rf /opt/homebot/tests

ENV PATH="/opt/homebot/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/data \
    XDG_DATA_HOME=/data \
    CLAUDE_CONFIG_DIR=/data/claude/config \
    SIGNAL_DAEMON_URL=http://127.0.0.1:7583 \
    BOT_DB=/data/homebot/bot.sqlite3 \
    NOTIFY_BIND=0.0.0.0 \
    NOTIFY_PORT=8999

CMD ["/run.sh"]
