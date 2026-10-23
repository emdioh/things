"""Command line entry point: run the server and help set up SSH access."""

import argparse
import asyncio
import sys

from . import config, ssh


def main() -> None:
    parser = argparse.ArgumentParser(prog="dashboard")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the web UI")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)

    sub.add_parser("pubkey", help="print the dashboard's public key (creating it if needed)")

    auth = sub.add_parser("authorize", help="print commands that install the key on hosts")
    auth.add_argument("hosts", nargs="*", help="host names (default: all)")

    check = sub.add_parser("check", help="test SSH access to hosts")
    check.add_argument("hosts", nargs="*", help="host names (default: all)")

    args = parser.parse_args()

    if args.cmd == "serve":
        import uvicorn

        from .app import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
        return

    cfg = config.load()
    if ssh.ensure_key(cfg):
        print(f"created {cfg.key_path}", file=sys.stderr)

    if args.cmd == "pubkey":
        print(ssh.public_key(cfg))
        return

    hosts = [cfg.hosts[n] for n in args.hosts] if args.hosts else list(cfg.hosts.values())

    if args.cmd == "authorize":
        pub = ssh.public_key(cfg)
        for h in hosts:
            where = "on the Proxmox host" if h.ctid is not None else f"inside {h.name} as root"
            print(f"# {h.name}: run {where}")
            print(ssh.authorize_command(pub, h))
        return

    if args.cmd == "check":
        failed = False
        for h in hosts:
            ok, out = asyncio.run(ssh.check(cfg, h))
            failed |= not ok
            print(f"{'OK  ' if ok else 'FAIL'} {h.name}")
            for line in out.splitlines():
                print(f"     {line}")
        sys.exit(1 if failed else 0)
