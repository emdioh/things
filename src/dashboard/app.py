"""FastAPI web UI. Authentication is expected in front (e.g. Cloudflare Access)."""

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from . import config, ssh
from .deploy import Deployer

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["ts"] = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""


def create_app(cfg: config.Config | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.cfg = cfg or config.load()
        ssh.ensure_key(app.state.cfg)
        app.state.deployer = Deployer(app.state.cfg)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx)

    @app.get("/")
    def index(request: Request):
        st = request.app.state
        return render(request, "index.html", cfg=st.cfg, last=st.deployer.last_per_service())

    @app.post("/services/{name}/deploy")
    async def deploy(request: Request, name: str, ref: str = Form("")):
        st = request.app.state
        if name not in st.cfg.services:
            raise HTTPException(404)
        try:
            job = st.deployer.start(name, ref)
        except (ValueError, RuntimeError) as e:
            return render(request, "_error.html", message=str(e))
        return Response(headers={"HX-Redirect": f"/jobs/{job.id}"})

    @app.get("/jobs/{job_id}")
    def job_page(request: Request, job_id: str):
        job = request.app.state.deployer.get(job_id)
        if not job:
            raise HTTPException(404)
        return render(request, "job.html", job=job)

    @app.get("/jobs/{job_id}/log")
    def job_log(request: Request, job_id: str):
        job = request.app.state.deployer.get(job_id)
        if not job:
            raise HTTPException(404)
        return render(request, "_log.html", job=job)

    @app.get("/history")
    def history(request: Request):
        return render(request, "history.html", jobs=request.app.state.deployer.history())

    @app.get("/ssh")
    def ssh_page(request: Request):
        cfg = request.app.state.cfg
        pub = ssh.public_key(cfg)
        commands = {h.name: ssh.authorize_command(pub, h) for h in cfg.hosts.values()}
        return render(request, "ssh.html", cfg=cfg, pubkey=pub, commands=commands)

    @app.post("/hosts/{name}/check")
    async def check_host(request: Request, name: str):
        cfg = request.app.state.cfg
        if name not in cfg.hosts:
            raise HTTPException(404)
        ok, output = await ssh.check(cfg, cfg.hosts[name])
        return render(request, "_check.html", ok=ok, output=output)

    return app
