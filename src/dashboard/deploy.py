"""Run redeploys on remote hosts and keep a record of each one."""

import asyncio
import json
import re
import shlex
import time
import uuid
from dataclasses import asdict, dataclass, field

from . import ssh
from .config import Config, Service

# Branch names, tags and commit hashes. Rejects whitespace, shell
# metacharacters and anything git would treat as an option.
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+@-]*$")

# Runs on the target with `bash -s -- <repo_dir> <ref>`. Everything lives in
# a function so bash has read the whole script before any step runs, and
# stdin is closed so a step can't swallow the rest of the script.
SCRIPT_TEMPLATE = r"""set -euo pipefail
deploy() {
  cd "$1"
  local ref=$2 target sha
  echo "==> git fetch ($(git remote get-url origin))"
  git fetch --prune --tags --force origin
  git remote set-head origin --auto >/dev/null
  if [ -z "$ref" ]; then
    target=origin/HEAD
  elif git rev-parse -q --verify "refs/remotes/origin/$ref^{commit}" >/dev/null; then
    target=origin/$ref
  else
    target=$ref
  fi
  sha=$(git rev-parse --verify "$target^{commit}")
  echo "==> checkout $target"
  git checkout -q --detach "$sha"
  git log -1 --format='    %h %s (%an, %ar)'
  export DEPLOY_REF=$target DEPLOY_SHA=$sha
__STEPS__
  echo "==> done"
}
deploy "$@" </dev/null
"""


def validate_ref(ref: str) -> str:
    ref = ref.strip()
    if ref and (not REF_RE.match(ref) or ".." in ref):
        raise ValueError(f"invalid git ref: {ref!r}")
    return ref


def build_script(service: Service) -> str:
    lines = []
    for step in service.steps:
        lines.append(f"  echo {shlex.quote('==> ' + step)}")
        lines.append(f"  {step}")
    return SCRIPT_TEMPLATE.replace("__STEPS__", "\n".join(lines))


@dataclass
class Job:
    id: str
    service: str
    ref: str
    started: float
    finished: float | None = None
    status: str = "running"  # running | ok | failed
    lines: list[str] = field(default_factory=list, repr=False)


class Deployer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.jobs_dir = cfg.data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.running: dict[str, Job] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.tasks: set[asyncio.Task] = set()

    def start(self, service_name: str, ref: str) -> Job:
        service = self.cfg.services[service_name]
        ref = validate_ref(ref) or service.default_ref
        lock = self.locks.setdefault(service_name, asyncio.Lock())
        if lock.locked():
            raise RuntimeError(f"{service_name} is already being deployed")
        job = Job(
            id=time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
            service=service_name,
            ref=ref or "(default branch)",
            started=time.time(),
        )
        self.running[job.id] = job
        # Hold a reference so the task isn't garbage-collected mid-run.
        task = asyncio.create_task(self._run(job, service, ref, lock))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return job

    async def _run(self, job: Job, service: Service, ref: str, lock: asyncio.Lock) -> None:
        async with lock:
            with (self.jobs_dir / f"{job.id}.log").open("w") as log:
                await self._run_logged(job, service, ref, log)

    async def _run_logged(self, job: Job, service: Service, ref: str, log) -> None:
        def emit(line: str) -> None:
            job.lines.append(line)
            log.write(line + "\n")
            log.flush()

        host = self.cfg.hosts[service.host]
        remote = f"bash -s -- {shlex.quote(service.repo_dir)} {shlex.quote(ref)}"
        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh.ssh_argv(self.cfg, host, remote),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            proc.stdin.write(build_script(service).encode())
            proc.stdin.close()
            async for raw in proc.stdout:
                emit(raw.decode(errors="replace").rstrip("\n"))
            code = await proc.wait()
            job.status = "ok" if code == 0 else "failed"
            if code:
                emit(f"==> exited with status {code}")
        except Exception as e:  # keep the dashboard alive whatever happens
            job.status = "failed"
            emit(f"==> error: {e}")
        finally:
            job.finished = time.time()
            meta = {k: v for k, v in asdict(job).items() if k != "lines"}
            (self.jobs_dir / f"{job.id}.json").write_text(json.dumps(meta))
            self.running.pop(job.id, None)

    def get(self, job_id: str) -> Job | None:
        if job_id in self.running:
            return self.running[job_id]
        meta_path = self.jobs_dir / f"{job_id}.json"
        if not re.fullmatch(r"[\w-]+", job_id) or not meta_path.exists():
            return None
        job = Job(**json.loads(meta_path.read_text()))
        log_path = self.jobs_dir / f"{job_id}.log"
        job.lines = log_path.read_text().splitlines() if log_path.exists() else []
        return job

    def history(self, limit: int = 100) -> list[Job]:
        done = sorted(self.jobs_dir.glob("*.json"), reverse=True)[:limit]
        jobs = list(self.running.values()) + [Job(**json.loads(p.read_text())) for p in done]
        return sorted(jobs, key=lambda j: j.started, reverse=True)[:limit]

    def last_per_service(self) -> dict[str, Job]:
        last: dict[str, Job] = {}
        for job in self.history(limit=500):
            last.setdefault(job.service, job)
        return last

