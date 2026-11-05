"""Follow Docker Model Runner's one-shot init job, which pulls the models of `models.txt`.

`papaia-ctl start` only starts the job: Compose returns once the container runs, and a pull of
several GB goes on in the background. A job that switches the module on, or applies its model
list, waits for it here and copies its lines into the job log, so the operator sees which
models were already present, which were pulled and which failed.

The container is found by the labels the core's compose file gives it (`de.fidonis.role:
model-init`) within this deployment's Compose project. In a distributed installation it runs
next to LiteLLM, possibly on another node, where this machine's `docker` does not reach, so
there the downloads are not followed and the log says so.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.core import nodes, services
from app.core.jobs import JobContext
from app.i18n import gettext as _

PROFILE = "model-runner"
ROLE = "model-init"

# A first pull of the default models is a few GB; larger models in the list take longer.
TIMEOUT_SECONDS = 3600.0
INTERVAL_SECONDS = 3.0
_DOCKER_TIMEOUT = 30.0

Docker = Callable[..., Awaitable[tuple[int, str]]]


async def _docker(*args: str) -> tuple[int, str]:
    """Exit code and combined output of a `docker` CLI call; 127 when it cannot run at all."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        return 127, str(exc)
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=_DOCKER_TIMEOUT)
    except TimeoutError:
        proc.kill()
        return 124, "docker did not answer"
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


async def follow(
    ctx: JobContext,
    *,
    config_dir: str,
    docker: Docker | None = None,
    limit: float | None = None,
    interval: float | None = None,
) -> str | None:
    """Wait for the init job and log its lines. None when it completed, else the sentence that
    says what did not.

    No container to follow (a node elsewhere, an unreadable Docker) is not a verdict: the log
    says so and the job goes on, as `wait_healthy` does for an unreadable Docker."""
    run = docker or _docker
    if nodes.is_distributed(config_dir):
        ctx.log("[info] model downloads not followed: the init job runs next to LiteLLM's node")
        return None
    filters = (
        "--filter", f"label=com.docker.compose.project={services.compose_project(config_dir)}",
        "--filter", f"label=de.fidonis.role={ROLE}",
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (TIMEOUT_SECONDS if limit is None else limit)
    pause = INTERVAL_SECONDS if interval is None else interval
    logged = 0
    while True:
        code, out = await run("ps", "-a", *filters, "--format", "{{.Names}}\t{{.State}}")
        rows = [line.split("\t") for line in out.splitlines() if "\t" in line]
        if code != 0 or not rows:
            ctx.log("[info] model downloads not followed: the init job was not found")
            return None
        name, state = rows[0][0], rows[0][1]
        code, out = await run("logs", name)
        lines = out.splitlines() if code == 0 else []
        for line in lines[logged:]:
            ctx.log(line)
        logged = max(logged, len(lines))
        if state in ("exited", "dead"):
            code, out = await run("inspect", "--format", "{{.State.ExitCode}}", name)
            if code == 0 and out.strip() == "0":
                return None
            return _(
                "The model download failed; the log above names the models that could not be "
                "pulled."
            )
        if loop.time() >= deadline:
            return _(
                "The models are still being pulled after %(minutes)d minutes. The download goes "
                "on in the background; the Host page shows which models are present."
            ) % {"minutes": int((TIMEOUT_SECONDS if limit is None else limit) // 60)}
        await asyncio.sleep(pause)
