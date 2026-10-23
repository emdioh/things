# Dashboard

A small web UI for redeploying services on the other machines of a Proxmox
server with one click. Each service is a git clone on its host; a redeploy:

1. `git fetch` in the service's directory,
2. checks out the remote's default branch, or a branch, tag or commit hash
   you type in,
3. runs the service's steps — by default `docker compose pull` and
   `docker compose up -d --build`, or your own commands/scripts.

The output streams live in the browser and every run is kept in the history.

The dashboard reaches hosts over SSH with its own key. There is no login:
put it behind something that authenticates (e.g. a Cloudflare Tunnel with
Cloudflare Access).

## Install (in a Debian/Ubuntu LXC)

```sh
apt install -y git openssh-client curl
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone -b dashboard https://github.com/emdioh/things /opt/dashboard
cd /opt/dashboard && ~/.local/bin/uv sync --frozen

mkdir -p /etc/dashboard
cp config.example.yaml /etc/dashboard/config.yaml   # then edit it

cp deploy/dashboard.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now dashboard
```

The UI listens on port 8080; point the Cloudflare tunnel at
`http://<lxc-ip>:8080`.

## Configuration

See [`config.example.yaml`](config.example.yaml). In short, `hosts` are the
machines to SSH into and `services` are git clones on those hosts:

| Service field | Meaning |
| --- | --- |
| `host` | name of a host in `hosts` |
| `repo_dir` | path of the git clone on that host |
| `default_ref` | branch/tag/hash used when the ref box is empty; omit for the remote's default branch |
| `steps` | shell commands run in `repo_dir` after checkout; the first failure stops the deploy |

Steps can use `$DEPLOY_REF` and `$DEPLOY_SHA`. The checkout is a detached
HEAD at the chosen commit; local edits to tracked files make the checkout
fail rather than being thrown away.

Restart the service after editing the config.

## Setting up SSH access

The dashboard creates its key pair (`data_dir/id_ed25519`) on first start, or
when you run `uv run dashboard pubkey`. To install it on the hosts:

- **Web:** the *SSH setup* page shows the public key and, per host, a
  command to paste. For hosts with a `ctid` it's a `pct exec` one-liner for
  the **Proxmox host shell**, plus a block that does every container at once.
  Hosts without a `ctid` (VMs, other machines) get a command to run inside
  that machine. Then press *Test connection*.
- **CLI** (inside the dashboard LXC, `DASHBOARD_CONFIG` set or config at the
  default path):

  ```sh
  uv run dashboard authorize        # print the commands for every host
  uv run dashboard check            # test SSH to every host
  ```

The commands are idempotent, so re-running them is harmless. Each target
needs an SSH server (`apt install openssh-server` if missing), plus `git`
and Docker. The first connection pins the host key in `data_dir/known_hosts`;
if you rebuild a container, delete its line there.

Fetching from private git remotes is the target host's business: give that
clone a deploy key or credentials so a plain `git fetch` works there.

## Development

```sh
uv sync
uv run pytest
DASHBOARD_CONFIG=./config.yaml uv run dashboard serve --port 8080
```
