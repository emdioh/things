#!/bin/sh
# Install cloudflared on Alpine as an OpenRC service that runs a Cloudflare
# Tunnel, and make the dashboard listen on localhost only so the tunnel is
# the only way in. Run as root; safe to re-run (e.g. to change the token).
#
#   sh install-cloudflared.sh <tunnel-token>
#   sh install-cloudflared.sh              # prompts, keeping the token out of shell history
set -eu

TOKEN_FILE=/etc/cloudflared/token
BIN=/usr/local/bin/cloudflared

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"
[ -f /etc/alpine-release ] || die "this script is for Alpine Linux"

token=${1:-}
if [ -z "$token" ]; then
    printf 'Tunnel token: '
    stty -echo 2>/dev/null || true
    read -r token
    stty echo 2>/dev/null || true
    echo
fi
# Accept a pasted `cloudflared service install <token>` command too.
token=${token##* }
[ -n "$token" ] || die "no token given"

case $(uname -m) in
    x86_64) arch=amd64 ;;
    aarch64) arch=arm64 ;;
    armv7l | armv6l) arch=arm ;;
    *) die "unsupported architecture: $(uname -m)" ;;
esac

log "downloading cloudflared ($arch)"
apk add --no-cache curl >/dev/null
curl -fsSLo "$BIN.new" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch"
chmod 755 "$BIN.new"
mv "$BIN.new" "$BIN"
"$BIN" --version

log "saving token to $TOKEN_FILE"
install -d -m 700 /etc/cloudflared
(umask 077 && printf '%s\n' "$token" > "$TOKEN_FILE")

log "installing OpenRC service"
cat > /etc/init.d/cloudflared <<EOF
#!/sbin/openrc-run

name="cloudflared"
description="Cloudflare Tunnel"
supervisor="supervise-daemon"
command="$BIN"
command_args="tunnel --no-autoupdate run --token-file $TOKEN_FILE"
output_log="/var/log/cloudflared.log"
error_log="/var/log/cloudflared.log"
respawn_delay=5

depend() {
	need net
	after dashboard
}
EOF
chmod 755 /etc/init.d/cloudflared
rc-update add cloudflared default >/dev/null
rc-service cloudflared restart >/dev/null

log "making the dashboard listen on localhost only"
if grep -q '^DASHBOARD_HOST=' /etc/conf.d/dashboard 2>/dev/null; then
    sed -i 's/^DASHBOARD_HOST=.*/DASHBOARD_HOST=127.0.0.1/' /etc/conf.d/dashboard
else
    echo 'DASHBOARD_HOST=127.0.0.1' >> /etc/conf.d/dashboard
fi
if [ -x /etc/init.d/dashboard ]; then
    rc-service dashboard restart >/dev/null
fi

cat <<EOF

cloudflared is running (logs: /var/log/cloudflared.log).

In the Cloudflare dashboard (Zero Trust > Networks > Tunnels > your tunnel),
add a public hostname with service type HTTP and URL:
  localhost:8080
and protect that hostname with an Access application, since the dashboard
itself has no login.
EOF
