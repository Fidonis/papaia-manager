"""Settings store and the settings API: branding name, tagline and logo.

The defaults are the contract that matters most -- an installation that never
opens the Settings page must render exactly what it rendered before. Each test
gets its own configuration directory, injected through the settings dependency.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-settings-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-settings-workspace-")

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

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core import settings_store  # noqa: E402
from app.main import create_app  # noqa: E402

_CSRF = "test-csrf-token-value"

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    b'<rect width="10" height="10"/></svg>'
)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    return directory


@pytest.fixture
def client(config_dir: Path) -> Iterator[TestClient]:
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"papaia_config_dir": str(config_dir)})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


def _as(client: TestClient, *roles: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    client.cookies.clear()
    client.cookies.set("papaia_manager_session", signed)
    return client


def _admin(client: TestClient) -> TestClient:
    return _as(client, "admin")


def _headers() -> dict[str, str]:
    return {"X-CSRF-Token": _CSRF}


def _revision(client: TestClient) -> str:
    return str(_admin(client).get("/api/v1/settings").json()["revision"])


def _put_branding(client: TestClient, **fields: Any) -> Any:
    body = {"revision": _revision(client), **fields}
    return _admin(client).put("/api/v1/settings/branding", json=body, headers=_headers())


def _upload(client: TestClient, data: bytes, name: str = "logo.bin") -> Any:
    return _admin(client).post(
        "/api/v1/settings/branding/logo", files={"file": (name, data)}, headers=_headers()
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_defaults_apply_without_a_settings_file(config_dir: Path) -> None:
    branding = settings_store.effective_branding(str(config_dir))

    assert branding.name == "papAIa manager"
    assert branding.tagline == "by Fidonis"
    assert branding.logo_url is None


def test_a_broken_settings_file_falls_back_to_defaults(config_dir: Path) -> None:
    (config_dir / "manager" / "settings.yaml").write_text("branding: [unclosed", encoding="utf-8")

    assert settings_store.effective_branding(str(config_dir)).name == "papAIa manager"


def test_unknown_sections_are_tolerated(config_dir: Path) -> None:
    (config_dir / "manager" / "settings.yaml").write_text(
        "smtp:\n  host: mail.test\nbranding:\n  name: Acme\n", encoding="utf-8"
    )

    assert settings_store.effective_branding(str(config_dir)).name == "Acme"


def test_empty_tagline_hides_the_line_but_none_keeps_the_default(config_dir: Path) -> None:
    stored = settings_store.ManagerSettings()
    stored.branding.tagline = ""
    settings_store.save_settings(str(config_dir), stored)
    assert settings_store.effective_branding(str(config_dir)).tagline == ""

    settings_store.save_settings(str(config_dir), settings_store.ManagerSettings())
    assert settings_store.effective_branding(str(config_dir)).tagline == "by Fidonis"


@pytest.mark.parametrize(
    "data",
    [b"", b"not an image", b"GIF89a....", b"<html><body>hi</body></html>"],
)
def test_unsupported_logo_bytes_are_refused(data: bytes) -> None:
    with pytest.raises(settings_store.SettingsError):
        settings_store.detect_logo_type(data)


@pytest.mark.parametrize(
    "payload",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>',
    ],
)
def test_active_svg_content_is_refused(payload: bytes) -> None:
    with pytest.raises(settings_store.SettingsError):
        settings_store.detect_logo_type(payload)


def test_oversized_logo_is_refused() -> None:
    with pytest.raises(settings_store.SettingsError):
        settings_store.detect_logo_type(_PNG + b"\x00" * settings_store.MAX_LOGO_BYTES)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_default_rendering_is_unchanged(client: TestClient) -> None:
    html = _admin(client).get("/settings").text

    assert ">papAIa manager</p>" in html
    assert ">by Fidonis</p>" in html
    assert "brand-mark" in html
    assert "<title>Settings — papAIa manager</title>" in html


def test_reset_asks_through_a_dialog_not_the_browser(client: TestClient) -> None:
    html = _admin(client).get("/settings").text

    assert 'id="reset-branding-modal"' in html
    assert "confirm(" not in html


def test_custom_branding_reaches_the_sidebar_and_titles(client: TestClient) -> None:
    assert _put_branding(client, name="Acme Hub", tagline="for Acme").status_code == 200

    html = _admin(client).get("/settings").text
    assert ">Acme Hub</p>" in html
    assert ">for Acme</p>" in html
    assert "<title>Settings — Acme Hub</title>" in html
    assert "by Fidonis" not in html.split("</aside>")[0]


def test_blank_tagline_removes_the_second_line(client: TestClient) -> None:
    assert _put_branding(client, name="Acme", tagline="").status_code == 200

    sidebar = _admin(client).get("/settings").text.split("</aside>")[0]
    assert "by Fidonis" not in sidebar
    assert ">Acme</p>" in sidebar


def test_name_is_escaped(client: TestClient) -> None:
    assert _put_branding(client, name="<b>x</b>", tagline=None).status_code == 200

    html = _admin(client).get("/settings").text
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert "<b>x</b>" not in html


def test_logo_replaces_the_builtin_mark(client: TestClient) -> None:
    assert _upload(client, _PNG).status_code == 200

    sidebar = _admin(client).get("/settings").text.split("</aside>")[0]
    assert 'class="brand-logo' in sidebar
    assert "brand-mark" not in sidebar


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_non_admins_cannot_read_or_write(client: TestClient) -> None:
    _as(client, "user")
    assert client.get("/api/v1/settings").status_code == 403
    assert client.get("/settings").status_code == 403
    assert (
        client.put(
            "/api/v1/settings/branding",
            json={"revision": "", "name": "x"},
            headers=_headers(),
        ).status_code
        == 403
    )


def test_a_write_without_csrf_is_refused(client: TestClient) -> None:
    response = _admin(client).put(
        "/api/v1/settings/branding", json={"revision": "", "name": "x"}
    )
    assert response.status_code == 403


def test_a_stale_revision_is_refused(client: TestClient) -> None:
    assert _put_branding(client, name="One").status_code == 200

    response = _admin(client).put(
        "/api/v1/settings/branding", json={"revision": "stale", "name": "Two"}, headers=_headers()
    )
    assert response.status_code == 409


def test_overlong_values_are_refused(client: TestClient) -> None:
    assert _put_branding(client, name="x" * 41).status_code == 422
    assert _put_branding(client, name="ok", tagline="y" * 61).status_code == 422


def test_branding_is_persisted_in_the_config_dir(client: TestClient, config_dir: Path) -> None:
    assert _put_branding(client, name="Acme", tagline="for Acme").status_code == 200

    text = (config_dir / "manager" / "settings.yaml").read_text(encoding="utf-8")
    assert "name: Acme" in text
    assert "tagline: for Acme" in text


def test_logo_upload_serve_and_delete(client: TestClient, config_dir: Path) -> None:
    body = _upload(client, _PNG).json()
    assert body["effective"]["logo_url"].startswith("/brand/logo")

    served = _admin(client).get("/brand/logo")
    assert served.status_code == 200
    assert served.content == _PNG
    assert served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert (config_dir / "manager" / "branding").is_dir()

    deleted = _admin(client).delete("/api/v1/settings/branding/logo", headers=_headers())
    assert deleted.json()["effective"]["logo_url"] is None
    assert _admin(client).get("/brand/logo").status_code == 404
    assert list((config_dir / "manager" / "branding").iterdir()) == []


def test_svg_logo_is_served_sandboxed(client: TestClient) -> None:
    assert _upload(client, _SVG).status_code == 200

    served = _admin(client).get("/brand/logo")
    assert served.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in served.headers["content-security-policy"]


def test_a_new_logo_replaces_the_old_file(client: TestClient, config_dir: Path) -> None:
    assert _upload(client, _PNG).status_code == 200
    assert _upload(client, _SVG).status_code == 200

    assert len(list((config_dir / "manager" / "branding").iterdir())) == 1


def test_bad_uploads_are_refused(client: TestClient) -> None:
    assert _upload(client, b"plain text").status_code == 422
    assert _upload(client, b"<svg><script>1</script></svg>").status_code == 422
    assert _upload(client, _PNG + b"\x00" * settings_store.MAX_LOGO_BYTES).status_code == 422


def test_reset_restores_defaults_and_removes_the_logo(client: TestClient, config_dir: Path) -> None:
    assert _put_branding(client, name="Acme", tagline="").status_code == 200
    assert _upload(client, _PNG).status_code == 200

    response = _admin(client).post(
        "/api/v1/settings/branding/reset",
        json={"revision": _revision(client)},
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["effective"] == {
        "name": "papAIa manager",
        "tagline": "by Fidonis",
        "logo_url": None,
    }
    assert list((config_dir / "manager" / "branding").iterdir()) == []


def test_changes_are_audited(client: TestClient, config_dir: Path) -> None:
    assert _put_branding(client, name="Acme").status_code == 200

    log = (config_dir / "manager" / "audit.log").read_text(encoding="utf-8")
    assert "settings.branding.update" in log
