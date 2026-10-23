"""Load the YAML config describing hosts and the services deployed on them."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = "/etc/dashboard/config.yaml"
DEFAULT_DATA_DIR = "/var/lib/dashboard"

# Used when a service does not list its own steps. `pull` refreshes any
# registry images the compose file references; `up --build` rebuilds the
# images that come from the checked-out code and recreates changed containers.
DEFAULT_STEPS = [
    "docker compose pull --ignore-buildable",
    "docker compose up -d --build --remove-orphans",
]


@dataclass
class Host:
    name: str
    address: str
    user: str = "root"
    port: int = 22
    ctid: int | None = None  # Proxmox container id, only used for setup hints


@dataclass
class Service:
    name: str
    host: str
    repo_dir: str
    default_ref: str = ""  # empty means the remote's default branch (origin/HEAD)
    steps: list[str] = field(default_factory=lambda: list(DEFAULT_STEPS))
    description: str = ""


@dataclass
class Config:
    data_dir: Path
    hosts: dict[str, Host]
    services: dict[str, Service]

    @property
    def key_path(self) -> Path:
        return self.data_dir / "id_ed25519"

    @property
    def known_hosts_path(self) -> Path:
        return self.data_dir / "known_hosts"


def config_path() -> Path:
    return Path(os.environ.get("DASHBOARD_CONFIG", DEFAULT_CONFIG_PATH))


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    raw = yaml.safe_load(path.read_text()) or {}

    hosts = {}
    for name, h in (raw.get("hosts") or {}).items():
        hosts[name] = Host(name=name, **h)

    services = {}
    for name, s in (raw.get("services") or {}).items():
        service = Service(name=name, **s)
        if service.host not in hosts:
            raise ValueError(f"service {name!r} uses unknown host {service.host!r}")
        if not service.steps:
            raise ValueError(f"service {name!r} has an empty steps list")
        services[name] = service

    data_dir = Path(raw.get("data_dir") or DEFAULT_DATA_DIR)
    return Config(data_dir=data_dir, hosts=hosts, services=services)
