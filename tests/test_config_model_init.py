"""Following Docker Model Runner's init job while a module or overlay job waits for it."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from app.core.config import model_init
from app.core.jobs import Job, JobContext

NAME = "papaia-model-runner-model-init-1"


def _context(tmp_path: Path) -> JobContext:
    job = Job(id="job-1", action="x", target="t", user="admin", created_at=None)  # type: ignore[arg-type]
    return JobContext(job=job, log_path=tmp_path / "job.log")


def _config(tmp_path: Path) -> str:
    config = tmp_path / "config"
    config.mkdir()
    (config / ".env").write_text("COMPOSE_PROJECT_NAME=papaia-dev\n", encoding="utf-8")
    return str(config)


class _Docker:
    """Answers `ps`, `logs` and `inspect` from a script of (state, log lines) per poll."""

    def __init__(self, polls: list[tuple[str, list[str]]], exit_code: str = "0") -> None:
        self.polls = polls
        self.exit_code = exit_code
        self.calls: list[tuple[str, ...]] = []
        self._at = -1

    async def __call__(self, *args: str) -> tuple[int, str]:
        self.calls.append(args)
        if args[0] == "ps":
            self._at = min(self._at + 1, len(self.polls) - 1)
            return 0, f"{NAME}\t{self.polls[self._at][0]}\n"
        if args[0] == "logs":
            return 0, "\n".join(self.polls[self._at][1]) + "\n"
        return 0, self.exit_code + "\n"


def _follow(tmp_path: Path, docker: _Docker, **kwargs: float) -> tuple[str | None, str]:
    ctx = _context(tmp_path)
    found = asyncio.run(
        model_init.follow(ctx, config_dir=_config(tmp_path), docker=docker, interval=0, **kwargs)
    )
    log = ctx.log_path.read_text(encoding="utf-8") if ctx.log_path.exists() else ""
    return found, log


def test_the_pulls_are_logged_once_each_until_the_job_completes(tmp_path: Path) -> None:
    first = ["[model-init] ai/qwen3 already present — skipped", "[model-init] Pulling ai/nomic ..."]
    docker = _Docker([("running", first), ("exited", [*first, "[model-init] ai/nomic done"])])
    found, log = _follow(tmp_path, docker)
    assert found is None
    assert log.count("Pulling ai/nomic") == 1
    assert "ai/nomic done" in log
    ps = docker.calls[0]
    assert "label=com.docker.compose.project=papaia-dev" in ps
    assert "label=de.fidonis.role=model-init" in ps


def test_a_failed_pull_is_a_problem(tmp_path: Path) -> None:
    lines = [
        "[model-init] ai/x FAILED: Unauthorized",
        "[model-init] 1 model(s) could not be pulled",
    ]
    found, log = _follow(tmp_path, _Docker([("exited", lines)], exit_code="1"))
    assert found is not None and "model download failed" in found
    assert "ai/x FAILED" in log


def test_a_long_pull_ends_the_wait_without_failing_the_download(tmp_path: Path) -> None:
    docker = _Docker([("running", ["[model-init] Pulling ai/big ..."])])
    found, _ = _follow(tmp_path, docker, limit=0)
    assert found is not None and "still being pulled" in found


def test_no_init_job_is_not_a_verdict(tmp_path: Path) -> None:
    async def none(*args: str) -> tuple[int, str]:
        return 0, ""

    ctx = _context(tmp_path)
    assert asyncio.run(model_init.follow(ctx, config_dir=_config(tmp_path), docker=none)) is None
    assert "not followed" in ctx.log_path.read_text(encoding="utf-8")


def test_a_distributed_installation_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    topology = {"topology": {"mode": "distributed", "controller": "a", "nodes": {"a": {}}}}
    Path(config, "deployment.yaml").write_text(yaml.safe_dump(topology), encoding="utf-8")
    docker = _Docker([("exited", [])])
    ctx = _context(tmp_path)
    assert asyncio.run(model_init.follow(ctx, config_dir=config, docker=docker)) is None
    assert docker.calls == []
