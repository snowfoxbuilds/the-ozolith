#!/usr/bin/env bash
# TheOzolith Node Daemon installer: turn a fresh Linux box into a
# Container-Host with ONE paste (ADR-0023). `theozolith join-token create`
# on the Control Node prints the exact line:
#
#   curl -fsSL <github-release-url>/install-nodedaemon.sh | sudo bash -s -- 'ozjoin1:...'
#
# or, from a checkout / scp copy:
#
#   sudo ./install-nodedaemon.sh 'ozjoin1:...' [--node <name>] [--source <checkout>]
#
# The installer and the distribution ride only pre-trusted channels (GitHub
# release HTTPS, or scp) — never the plaintext bootstrap listener: code over
# an unverified channel would be remote code execution for a LAN MITM. The
# join string itself is verified machine-side by `provision` (CA fingerprint
# before any transmission; the token is disposable). No .env, no manual
# configuration: `provision` persists everything under /var/lib/theozolith.
#
# Host baseline (ADR-0015): systemd Linux, docker (with the compose plugin),
# python3 >= 3.11.

set -euo pipefail

JOIN=""
NODE_NAME="$(hostname)"
SOURCE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --node) NODE_NAME="$2"; shift 2 ;;
        --source) SOURCE="$2"; shift 2 ;;
        ozjoin*) JOIN="$1"; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$JOIN" ]; then
    echo "usage: install-nodedaemon.sh 'ozjoin1:...' [--node <name>] [--source <checkout>]" >&2
    echo "mint the join string with 'theozolith join-token create' on the Control Node" >&2
    echo "(provisioning requires a join string — there is no manual path; ADR-0023)" >&2
    exit 2
fi

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 2; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 (>= 3.11) is required" >&2; exit 2; }
command -v systemctl >/dev/null || { echo "systemd is required" >&2; exit 2; }

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

# The daemon state dir, owned by the service user before provision writes
# into it (provision chowns its files to the dir owner).
install -d -m 0750 -o ozolith -g ozolith /var/lib/theozolith

# The unit, embedded so `curl | bash` needs no sidecar files. Every
# TheOzolith process on this node (the daemon, the driver processes it
# supervises) lives in this unit's cgroup: KillMode=control-group means
# they are live descendants or do not exist. Configuration comes from the
# provisioned state dir — no environment file exists.
cat > /etc/systemd/system/theozolith-nodedaemon.service <<'UNIT'
[Unit]
Description=TheOzolith Node Daemon (Container-Host: Stacks, drivers, builds, heartbeats)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
# Not root: the daemon only needs docker access and its own state dirs.
User=ozolith
Group=ozolith
SupplementaryGroups=docker
ExecStart=/opt/theozolith/bin/theozolith-nodedaemon
KillMode=control-group
Restart=always
RestartSec=10
# /var/lib/theozolith: provisioned identity (control-url, node-token,
# ca.pem, node-name), config cache, materialized compose files (disk).
StateDirectory=theozolith
Environment=THEOZOLITH_STATE_DIR=/var/lib/theozolith
# /run/theozolith: secrets tmpfs — values never touch node disk and vanish
# with the daemon (NODE-SUBSTRATE.md).
RuntimeDirectory=theozolith
RuntimeDirectoryMode=0700
Environment=THEOZOLITH_RUNTIME_DIR=/run/theozolith

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload

# The final step IS provisioning (ADR-0023): verify the pinned CA
# fingerprint, exchange the join token for this node's own token, persist,
# enable the unit, first heartbeat. Fails closed and loud.
/opt/theozolith/bin/theozolith-nodedaemon provision "$JOIN" --node "$NODE_NAME"

echo "node daemon installed; watch:  journalctl -fu theozolith-nodedaemon"
