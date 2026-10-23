"""SSH key management and command construction for reaching the hosts."""

import asyncio
import shlex
import subprocess

from .config import Config, Host


def ensure_key(cfg: Config) -> bool:
    """Create the dashboard's SSH key pair if missing. Returns True if created."""
    if cfg.key_path.exists():
        return False
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "dashboard", "-f", str(cfg.key_path)],
        check=True,
    )
    return True


def public_key(cfg: Config) -> str:
    return cfg.key_path.with_suffix(".pub").read_text().strip()


def ssh_argv(cfg: Config, host: Host, remote_command: str) -> list[str]:
    # accept-new pins each host's key on first contact and refuses changes later.
    return [
        "ssh",
        "-i", str(cfg.key_path),
        "-p", str(host.port),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={cfg.known_hosts_path}",
        f"{host.user}@{host.address}",
        remote_command,
    ]


def authorize_snippet(pubkey: str, user: str) -> str:
    """Shell commands that add `pubkey` to `user`'s authorized_keys, idempotently."""
    k, u = shlex.quote(pubkey), shlex.quote(user)
    return (
        f'h=$(getent passwd {u} | cut -d: -f6) && '
        f'install -d -m 700 -o {u} "$h/.ssh" && '
        f'f="$h/.ssh/authorized_keys" && touch "$f" && chmod 600 "$f" && chown {u}: "$f" && '
        f'(grep -qxF {k} "$f" || echo {k} >> "$f")'
    )


def authorize_command(pubkey: str, host: Host) -> str:
    """One-liner that installs the key on `host`.

    With a ctid it is meant for the Proxmox host shell (via `pct exec`);
    otherwise it is meant to be run inside the target machine itself.
    """
    snippet = authorize_snippet(pubkey, host.user)
    if host.ctid is not None:
        return f"pct exec {host.ctid} -- sh -c {shlex.quote(snippet)}"
    return snippet


async def check(cfg: Config, host: Host) -> tuple[bool, str]:
    """Try to log in and run a trivial command. Returns (ok, output)."""
    proc = await asyncio.create_subprocess_exec(
        *ssh_argv(cfg, host, "echo ok: $(hostname); docker --version; git --version"),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode == 0, out.decode(errors="replace").strip()
