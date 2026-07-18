#!/usr/bin/env bash
# TheOzolith Node Daemon installer: turn a fresh Linux box into a
# Container-Host (NODE-SUBSTRATE.md). Bootstrap = install the daemon;
# everything else flows from config.
#
#   sudo ./install-nodedaemon.sh \
#     --control-url https://<slug>.theozolith.internal:8443 \
#     --ca /path/to/ca.pem \
#     [--node <name>] [--source /path/to/theozolith-checkout]
#
# The --control-url host must be the Control Node's canonical origin
# (ADR-0019: the name minted by `origin-init` and carried in its TLS cert),
# reachable via trusted-network DNS — cert verification pins that name.
#
# The node token is read from THEOZOLITH_NODE_TOKEN in the environment or
# prompted for (never passed as an argument: argv is world-readable).
# TLS provisioning: --ca installs the Control Node's self-signed CA bundle
# (minted by `theozolith-control tls-init`) at /etc/theozolith/ca.pem and
# pins it via THEOZOLITH_TLS_CA.
#
# Host baseline (ADR-0015): systemd Linux, docker (with the compose plugin),
# python3 >= 3.11.

set -euo pipefail

CONTROL_URL=""
CA_FILE=""
NODE_NAME="$(hostname)"
SOURCE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --control-url) CONTROL_URL="$2"; shift 2 ;;
        --ca) CA_FILE="$2"; shift 2 ;;
        --node) NODE_NAME="$2"; shift 2 ;;
        --source) SOURCE="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 2; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 (>= 3.11) is required" >&2; exit 2; }
command -v systemctl >/dev/null || { echo "systemd is required" >&2; exit 2; }

if [ -n "$CONTROL_URL" ] && [ -z "${THEOZOLITH_NODE_TOKEN:-}" ]; then
    read -r -s -p "node token (THEOZOLITH_NODE_TOKEN): " THEOZOLITH_NODE_TOKEN; echo
fi

# The service user: unprivileged + docker group (run containers, builds).
id ozolith >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin ozolith
usermod -aG docker ozolith

# The product distribution: one venv, daemon + drivers + knowledge machinery
# updated together via the update command (ADR-0013 §8).
python3 -m venv /opt/theozolith
if [ -n "$SOURCE" ]; then
    /opt/theozolith/bin/pip install --quiet --upgrade \
        "$SOURCE/knowledge" "$SOURCE/worker" "$SOURCE/nodedaemon"
else
    /opt/theozolith/bin/pip install --quiet --upgrade \
        theozolith-knowledge theozolith-worker theozolith-nodedaemon
fi

install -d -m 0750 -o root -g ozolith /etc/theozolith
if [ -n "$CA_FILE" ]; then
    install -m 0640 -o root -g ozolith "$CA_FILE" /etc/theozolith/ca.pem
fi

if [ ! -f /etc/theozolith/.env ]; then
    umask 027
    {
        echo "# TheOzolith Node Daemon — see deploy/.env.example for every knob."
        echo "THEOZOLITH_NODE_NAME=${NODE_NAME}"
        [ -n "$CONTROL_URL" ] && echo "CONTROL_NODE_URL=${CONTROL_URL}"
        [ -n "${THEOZOLITH_NODE_TOKEN:-}" ] && echo "THEOZOLITH_NODE_TOKEN=${THEOZOLITH_NODE_TOKEN}"
        [ -n "$CA_FILE" ] && echo "THEOZOLITH_TLS_CA=/etc/theozolith/ca.pem"
    } > /etc/theozolith/.env
    chgrp ozolith /etc/theozolith/.env
else
    echo "keeping existing /etc/theozolith/.env"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
install -m 0644 "$SCRIPT_DIR/systemd/theozolith-nodedaemon.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now theozolith-nodedaemon

echo "node daemon installed and running; it registers as '${NODE_NAME}'"
echo "watch:  journalctl -fu theozolith-nodedaemon"
