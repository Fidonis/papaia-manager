"""REST API of the Models page: the models LiteLLM serves and what they consumed.

Read-only and for administrators. The LiteLLM behind it is the in-memory fake of
`tests/fake_litellm.py`.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-models-api-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-models-api-workspace-")

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": _CONFIG_DIR,
    "PAPAIA_WORKSPACE_DIR": _WORKSPACE_DIR,
}.items():
    os.environ.setdefault(_key, _value)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core import model_usage  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers.models_deps import get_litellm_transport  # noqa: E402
from tests.fake_litellm import MASTER_KEY, FakeLitellm  # noqa: E402

TODAY = date(2026, 10, 24)
_BASE = "/api/v1/models"


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    model_usage.reset_cache()
    monkeypatch.setattr(model_usage, "utc_today", lambda: TODAY)
    yield
    model_usage.reset_cache()


@pytest.fixture
def litellm() -> FakeLitellm:
    fake = FakeLitellm()
    fake.use(TODAY, "gpt-4o-mini", prompt=1000, completion=2000, spend=0.0021, ok=10, failed=1)
    fake.use(
        TODAY - timedelta(days=2), "chat-large", prompt=5000, completion=1000, spend=0.03, ok=3
    )
    fake.use(
        TODAY - timedelta(days=3), "retired-model", prompt=100, completion=100, spend=0.001, ok=1
    )
    fake.use(
        TODAY - timedelta(days=10),
        "gpt-4o-mini",
        prompt=10_000,
        completion=20_000,
        spend=0.02,
        ok=20,
    )
    return fake


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "ai" / "litellm").mkdir(parents=True)
    (directory / "ai" / "litellm" / ".env").write_text(
        f"LITELLM_MASTER_KEY={MASTER_KEY}\n", encoding="utf-8"
    )
    return directory


@pytest.fixture
def app(config_dir: Path, litellm: FakeLitellm) -> Iterator[FastAPI]:
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"papaia_config_dir": str(config_dir)})
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_litellm_transport] = lambda: litellm.transport
    yield application
    get_settings.cache_clear()


def _browser(app: FastAPI, *roles: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        }
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    browser = TestClient(app, follow_redirects=False)
    browser.cookies.set("papaia_manager_session", signed)
    return browser


@pytest.fixture
def admin(app: FastAPI) -> TestClient:
    return _browser(app, "admin")


# ── who may ask ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [_BASE, _BASE + "/usage"])
def test_only_an_administrator_may_read_it(app: FastAPI, path: str) -> None:
    assert _browser(app, "user").get(path).status_code == 403
    assert TestClient(app, follow_redirects=False).get(path).status_code == 401


def test_there_is_nothing_to_change_through_it(admin: TestClient) -> None:
    for method in ("post", "put", "patch", "delete"):
        for path in (_BASE, _BASE + "/usage"):
            assert getattr(admin, method)(path).status_code in (403, 404, 405)


# ── the catalogue ───────────────────────────────────────────────────────────────────────────


def test_the_catalogue_lists_the_served_models_with_prices_per_million_tokens(
    admin: TestClient,
) -> None:
    response = admin.get(_BASE)

    assert response.status_code == 200
    models = {item["name"]: item for item in response.json()["models"]}
    assert sorted(models) == ["bge-m3", "chat-large", "gpt-4o-mini", "never-used", "qwen-local"]
    assert models["chat-large"] == {
        "name": "chat-large",
        "provider": "vendor",
        "host": "",
        "mode": "chat",
        "input_per_million": 3.0,
        "output_per_million": 15.0,
        "priced": True,
        "deployments": 1,
    }
    assert models["qwen-local"]["priced"] is False and models["qwen-local"]["host"] == "localai"
    assert models["bge-m3"]["mode"] == "embedding"


# ── the usage ───────────────────────────────────────────────────────────────────────────────


def test_the_usage_of_the_last_week_is_the_default(admin: TestClient) -> None:
    body = admin.get(_BASE + "/usage").json()

    assert (body["range"], body["start"], body["end"]) == ("7d", "2026-10-18", "2026-10-24")
    assert body["days"] == [f"2026-10-{day}" for day in range(18, 25)]
    assert body["complete"] is True
    assert body["totals"] == {
        "prompt_tokens": 6100,
        "completion_tokens": 3100,
        "total_tokens": 9200,
        "spend": pytest.approx(0.0331),
        "requests": 15,
        "failed_requests": 1,
    }
    assert [day["date"] for day in body["daily"]] == body["days"]
    assert body["daily"][-1]["total_tokens"] == 3000
    assert sum(day["total_tokens"] for day in body["daily"]) == 9200


def test_every_model_has_its_totals_and_one_figure_per_day(admin: TestClient) -> None:
    body = admin.get(_BASE + "/usage").json()

    models = {item["name"]: item for item in body["models"]}
    assert "never-used" in models and models["never-used"]["totals"]["requests"] == 0
    assert models["gpt-4o-mini"]["configured"] is True
    assert models["retired-model"]["configured"] is False
    assert all(len(item["daily"]) == 7 for item in body["models"])
    assert models["gpt-4o-mini"]["daily"][-1]["total_tokens"] == 3000
    assert models["chat-large"]["totals"]["spend"] == pytest.approx(0.03)


def test_a_longer_or_custom_window_is_read_when_asked(admin: TestClient) -> None:
    month = admin.get(_BASE + "/usage", params={"range": "30d"}).json()
    custom = admin.get(
        _BASE + "/usage", params={"range": "custom", "from": "2026-10-13", "to": "2026-10-15"}
    ).json()

    assert len(month["days"]) == 30 and month["totals"]["total_tokens"] == 9200 + 30_000
    assert custom["range"] == "custom"
    assert custom["days"] == ["2026-10-13", "2026-10-14", "2026-10-15"]
    assert custom["totals"]["total_tokens"] == 30_000


@pytest.mark.parametrize(
    "params",
    [
        {"range": "forever"},
        {"range": "custom"},
        {"range": "custom", "from": "2026-10-20", "to": "2026-10-10"},
        {"range": "custom", "from": "2026-10-20", "to": "2026-12-01"},
        {"range": "custom", "from": "2020-01-01", "to": "2026-10-24"},
        {"range": "custom", "from": "x", "to": "y"},
    ],
)
def test_a_window_that_makes_no_sense_is_a_422_that_says_why(
    admin: TestClient, params: dict[str, str]
) -> None:
    response = admin.get(_BASE + "/usage", params=params)

    assert response.status_code == 422
    assert response.json()["detail"]


# ── LiteLLM that cannot be read ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [_BASE, _BASE + "/usage"])
def test_a_litellm_that_is_down_is_a_503_that_says_so(
    admin: TestClient, litellm: FakeLitellm, path: str
) -> None:
    litellm.down = True

    response = admin.get(path)

    assert response.status_code == 503
    assert response.json()["detail"] == "LiteLLM is not reachable at litellm:4000 (ConnectError)."


def test_a_refused_key_and_a_missing_key_are_told_apart(
    admin: TestClient, litellm: FakeLitellm, config_dir: Path
) -> None:
    litellm.master_key = "sk-another-key"
    refused = admin.get(_BASE + "/usage")
    model_usage.reset_cache()
    (config_dir / "ai" / "litellm" / ".env").unlink()
    missing = admin.get(_BASE + "/usage")

    assert refused.status_code == missing.status_code == 503
    assert "refused the master key" in refused.json()["detail"]
    assert "LITELLM_MASTER_KEY is not set" in missing.json()["detail"]
    assert MASTER_KEY not in refused.text + missing.text


def test_the_api_only_reads(admin: TestClient, litellm: FakeLitellm) -> None:
    admin.get(_BASE)
    admin.get(_BASE + "/usage")

    assert litellm.methods == {"GET"}
