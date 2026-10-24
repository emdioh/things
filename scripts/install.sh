#!/bin/sh
# Install or update the dashboard on Alpine Linux. Run as root; safe to re-run.
#
#   wget -qO- https://raw.githubusercontent.com/emdioh/things/dashboard/scripts/install.sh | sh
#
# Override REPO, BRANCH or DIR in the environment if needed.
set -eu

REPO=${REPO:-https://github.com/emdioh/things}
BRANCH=${BRANCH:-dashboard}
DIR=${DIR:-/opt/dashboard}
CONFIG=/etc/dashboard/config.yaml

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"
[ -f /etc/alpine-release ] || die "this script is for Alpine Linux"

log "installing packages"
apk add --no-cache python3 git openssh-client openssh-keygen curl >/dev/null

if ! command -v uv >/dev/null 2>&1; then
    log "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
fi

if [ -d "$DIR/.git" ]; then
    log "updating $DIR"
    git -C "$DIR" pull --ff-only -q
else
    log "cloning $REPO ($BRANCH) into $DIR"
    git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi

log "installing Python dependencies"
(cd "$DIR" && uv sync -q --frozen --no-dev)

if [ ! -f "$CONFIG" ]; then
    log "creating $CONFIG from the example"
    install -d /etc/dashboard
    cp "$DIR/config.example.yaml" "$CONFIG"
    new_config=1
fi

log "installing OpenRC service"
install -m 755 "$DIR/deploy/dashboard.openrc" /etc/init.d/dashboard
rc-update add dashboard default >/dev/null
rc-service dashboard restart >/dev/null

pubkey=$(DASHBOARD_CONFIG="$CONFIG" "$DIR/.venv/bin/dashboard" pubkey)

cat <<EOF

Dashboard is running on port 8080 (logs: /var/log/dashboard.log).

Its SSH public key, to authorize on your hosts (see the SSH setup page):
  $pubkey
EOF
if [ "${new_config:-}" ]; then
    cat <<EOF

Next: edit $CONFIG with your hosts and services, then
  rc-service dashboard restart
EOF
fi
