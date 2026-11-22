"""A papaia-docker-gate that answers the way the core's gate does, for the manager's tests.

An in-memory fake behind `httpx.MockTransport`. It knows a few containers, shows only those
of its named projects, cut to the fields the real gate lets through, and answers start and
stop of a named project's container with 204 and of anything else with 403 and the gate's
error code. Every request is recorded with its correlation id, so a test can tell what
reached the gate.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

PROJECT_LABEL = "com.docker.compose.project"
LIST_FIELDS = ("Id", "Names", "Image", "ImageID", "Created", "State", "Status", "Labels")
_PATH_RE = re.compile(r"\A(?:/v1\.\d+)?/containers/([^/]+)/(start|stop|json)\Z")


@dataclass
class Container:
    name: str
    project: str
    service: str
    module: str = ""
    role: str = ""
    state: str = "running"
    status: str = "Up 1 day"

    @property
    def id(self) -> str:
        return (self.name.encode().hex() * 64)[:64]

    def listed(self) -> dict[str, Any]:
        labels = {PROJECT_LABEL: self.project, "com.docker.compose.service": self.service}
        if self.module:
            labels["de.fidonis.module"] = self.module
        if self.role:
            labels["de.fidonis.role"] = self.role
        full = {
            "Id": self.id,
            "Names": [f"/{self.name}"],
            "Image": "example/image:1",
            "ImageID": "sha256:" + "0" * 64,
            "Created": 1,
            "State": self.state,
            "Status": self.status,
            "Labels": labels,
            # Fields Docker sends and the gate cuts away.
            "Ports": [{"PublicPort": 8000}],
            "Mounts": [{"Source": "/"}],
        }
        return {key: full[key] for key in LIST_FIELDS}


@dataclass
class FakeGate:
    projects: frozenset[str] = frozenset({"papaia"})
    containers: list[Container] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    down: bool = False
    # A status other than 200 for the list call, to make the gate fail.
    list_status: int = 200

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]

    def _find(self, ref: str) -> Container | None:
        for container in self.containers:
            if ref in (container.name, container.id):
                return container
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("no route to papaia-docker-gate", request=request)
        self.requests.append(request)
        echo = {"X-Papaia-Correlation-Id": request.headers.get("X-Papaia-Correlation-Id", "")}
        path = request.url.path
        if path.endswith("/_ping") and request.method in ("GET", "HEAD"):
            return httpx.Response(200, text="OK", headers=echo)
        if path.endswith("/containers/json") and request.method == "GET":
            if self.list_status != 200:
                return httpx.Response(
                    self.list_status, json={"error": "docker_gate.upstream_failed"}
                )
            query = parse_qs(request.url.query.decode())
            everything = query.get("all") == ["1"]
            listed = [
                c.listed()
                for c in self.containers
                if c.project in self.projects and (everything or c.state == "running")
            ]
            return httpx.Response(200, json=listed, headers=echo)
        match = _PATH_RE.match(path)
        expected = {"json": "GET", "start": "POST", "stop": "POST"}
        if match is None or request.method != expected[match.group(2)]:
            return httpx.Response(403, json={"error": "docker_gate.not_allowed"}, headers=echo)
        if request.content:
            return httpx.Response(
                400, json={"error": "docker_gate.invalid_request"}, headers=echo
            )
        container = self._find(match.group(1))
        if container is None or container.project not in self.projects:
            return httpx.Response(
                403, json={"error": "docker_gate.foreign_project"}, headers=echo
            )
        action = match.group(2)
        if action == "json":
            return httpx.Response(200, content=json.dumps({"Id": container.id}), headers=echo)
        wanted = "running" if action == "start" else "exited"
        if container.state == wanted:
            return httpx.Response(304, headers=echo)
        container.state = wanted
        container.status = "Up 1 second" if wanted == "running" else "Exited (0) 1 second ago"
        return httpx.Response(204, headers=echo)
