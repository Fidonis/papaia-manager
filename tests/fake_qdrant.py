"""A Qdrant that speaks just enough REST, for the tests of the pages that talk to one.

An in-memory fake behind `httpx.MockTransport`. It answers only the calls the manager
makes and in Qdrant's own error shape, and a request with the wrong api-key is a 403.
`Fleet` puts several of them on one transport, by host, which is how a test gives the
manager more than one connection.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

import httpx

from app.core.rag_collections import acl_point_id

API_KEY = "test-api-key"
ACL = "_rbac_acl"


def _ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"result": result, "status": "ok", "time": 0.0})


def _err(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"status": {"error": message}, "time": 0.0})


def _missing(name: str) -> httpx.Response:
    return _err(404, f"Not found: Collection `{name}` doesn't exist!")


class FakeQdrant:
    def __init__(self, api_key: str = API_KEY) -> None:
        self.api_key = api_key
        self.collections: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.down = False
        # (method, path) pairs that answer 500, to make a step in the middle fail.
        self.fail: set[tuple[str, str]] = set()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- seeding and inspection ------------------------------------------------

    def add(self, name: str, size: int = 4, extra_points: int = 0) -> None:
        self.collections[name] = {
            "vectors": {"size": size, "distance": "Cosine"},
            "points": {},
            "indexes": [],
            "extra": extra_points,
        }

    def put(self, collection: str, point_id: str, payload: dict[str, Any]) -> None:
        self.collections[collection]["points"][point_id] = {"vector": [0.0], "payload": payload}

    def grant(
        self,
        role: str,
        collection: str,
        access: str,
        doc_policy: Any = None,
        pid: str | None = None,
    ) -> str:
        if ACL not in self.collections:
            self.add(ACL, size=1)
        point_id = pid or acl_point_id(role, collection)
        self.put(
            ACL,
            point_id,
            {"role": role, "collection": collection, "access": access, "doc_policy": doc_policy},
        )
        return point_id

    def payloads(self, collection: str) -> dict[str, dict[str, Any]]:
        return {
            pid: point["payload"]
            for pid, point in self.collections.get(collection, {"points": {}})["points"].items()
        }

    def grants(self) -> set[tuple[str, str, str]]:
        return {
            (p["role"], p["collection"], p["access"]) for p in self.payloads(ACL).values()
        }

    def wrote(self) -> list[tuple[str, str]]:
        """The calls that changed something; a scroll or a retrieve is a POST that reads."""
        return [
            call
            for call in self.calls
            if call[0] in ("PUT", "DELETE") or call[1].endswith("/points/delete")
        ]

    # -- the wire ---------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        self.calls.append((request.method, unquote(path)))
        # A Qdrant without an api-key accepts a request that sends none.
        if (request.headers.get("api-key") or "") != self.api_key:
            return _err(403, "Invalid api-key")
        if (request.method, unquote(path)) in self.fail:
            return _err(500, "Service internal error: injected failure")

        parts = [unquote(part) for part in path.split("/")[1:]]
        body = json.loads(request.content) if request.content else {}
        method = request.method

        if parts == ["collections"] and method == "GET":
            return _ok({"collections": [{"name": name} for name in self.collections]})
        if parts[0] != "collections" or len(parts) < 2:
            return _err(404, "Not found")

        name, rest = parts[1], parts[2:]
        col = self.collections.get(name)

        if not rest:
            if method == "GET":
                if col is None:
                    return _missing(name)
                return _ok(
                    {
                        "status": "green",
                        "points_count": len(col["points"]) + col["extra"],
                        "config": {"params": {"vectors": col["vectors"]}},
                    }
                )
            if method == "PUT":
                if col is not None:
                    return _err(409, f"Wrong input: Collection `{name}` already exists!")
                self.add(name, size=body["vectors"]["size"])
                self.collections[name]["vectors"] = body["vectors"]
                return _ok(True)
            if method == "DELETE":
                if col is None:
                    return _missing(name)
                del self.collections[name]
                return _ok(True)

        if col is None:
            return _missing(name)

        if rest == ["index"] and method == "PUT":
            col["indexes"].append((body["field_name"], body["field_schema"]))
            return _ok({"status": "completed"})
        if rest == ["points"] and method == "PUT":
            for point in body["points"]:
                col["points"][point["id"]] = {
                    "vector": point["vector"],
                    "payload": point["payload"],
                }
            return _ok({"status": "completed"})
        if rest == ["points"] and method == "POST":
            return _ok(
                [
                    {"id": pid, "payload": col["points"][pid]["payload"]}
                    for pid in body["ids"]
                    if pid in col["points"]
                ]
            )
        if rest == ["points", "scroll"] and method == "POST":
            ids = list(col["points"])
            start = ids.index(body["offset"]) if body.get("offset") in ids else 0
            limit = body.get("limit", 10)
            page = ids[start : start + limit]
            following = ids[start + limit] if start + limit < len(ids) else None
            points = [{"id": pid, "payload": col["points"][pid]["payload"]} for pid in page]
            return _ok({"points": points, "next_page_offset": following})
        if rest == ["points", "delete"] and method == "POST":
            for pid in body["points"]:
                col["points"].pop(pid, None)
            return _ok({"status": "completed"})
        return _err(404, "Not found")


class Fleet:
    """Several fakes on one transport, told apart by the host of the request."""

    def __init__(self) -> None:
        self.by_host: dict[str, FakeQdrant] = {}

    def add(self, host: str, fake: FakeQdrant) -> FakeQdrant:
        self.by_host[host] = fake
        return fake

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._route)

    def _route(self, request: httpx.Request) -> httpx.Response:
        fake = self.by_host.get(request.url.host)
        if fake is None:
            raise httpx.ConnectError("no such host", request=request)
        return fake.handle(request)
