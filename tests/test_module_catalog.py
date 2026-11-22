"""The module catalog: versions from the suite bill of materials, state from the platform catalog.

The bill of materials (`compat/suite-<VERSION>.yaml` of the core checkout) says which modules
the suite ships and in which version; the platform catalog (`platform/services.json`, read with
the kit's reader) says which of them are installed and whether they run. A module of the bill
of materials without a catalog entry is "available": it can be installed from the catalog,
nothing has to be copied out of a module repository (PLT-31-A1, FLO-49-A1).
"""
from __future__ import annotations

import base64
import copy
import json
import os
import tempfile
import time
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-modules-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-modules-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402
from papaia_kit.catalog import content_hash  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core import module_catalog  # noqa: E402
from app.main import create_app  # noqa: E402

_KEY = ec.generate_private_key(ec.SECP256R1())
_OTHER_KEY = ec.generate_private_key(ec.SECP256R1())
_SUITE = "1.10.0"
_MODULES = {"papaia-studio": "0.2.0", "papaia-data": "0.1.0", "papaia-flow": "0.1.0",
            "papaia-assist": "0.1.0"}


def _repo(name: str, role: str, version: str) -> dict[str, Any]:
    return {
        "name": name,
        "role": role,
        "version": version,
        "tag": f"v{version}",
        "commit": "0" * 40,
        "containers": 1,
        "images": [],
    }


def _bom(modules: dict[str, str] = _MODULES) -> dict[str, Any]:
    public = _KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return {
        "schema": "papaia.suite-bom/1",
        "suite": _SUITE,
        "scope": "delivery",
        "created": "2026-11-21T10:00:00Z",
        "contract_generation": {"min": 1, "current": 13},
        "signing_key": {
            "algorithm": "ecdsa-p256-sha256",
            "fingerprint": "sha256:" + "0" * 64,
            "public_key": public,
        },
        "repos": [
            _repo("papaia", "core", _SUITE),
            _repo("papaia-manager", "manager", "1.8.0"),
            *(_repo(name, "module", version) for name, version in modules.items()),
            _repo("papaia-kit", "kit", "0.1.3"),
        ],
        "totals": {"containers": 7, "images": 0},
    }


def write_bom(
    workspace: Path,
    bom: dict[str, Any] | None = None,
    *,
    key: ec.EllipticCurvePrivateKey = _KEY,
    pin_key: bool = True,
    sign: bool = True,
) -> Path:
    core = workspace / "papaia"
    (core / "compat").mkdir(parents=True, exist_ok=True)
    (core / "VERSION").write_text(_SUITE + "\n", encoding="utf-8")
    raw = yaml.safe_dump(bom if bom is not None else _bom(), sort_keys=False).encode()
    path = core / "compat" / f"suite-{_SUITE}.yaml"
    path.write_bytes(raw)
    if sign:
        signature = key.sign(raw, ec.ECDSA(hashes.SHA256()))
        (core / "compat" / f"suite-{_SUITE}.yaml.sig").write_text(
            base64.b64encode(signature).decode() + "\n", encoding="utf-8"
        )
    if pin_key:
        (core / "compat" / "release-key.pub").write_bytes(
            _KEY.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
    return path


def _example_catalog() -> dict[str, Any]:
    source = resources.files("papaia_kit").joinpath(
        "_data/contract/examples/platform-services.example.json"
    )
    document: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
    return document


def write_catalog(config: Path, states: dict[str, str]) -> Path:
    """The example catalog with only the given providers' ui entries, in the given state."""
    document = _example_catalog()
    services = []
    for entry in document["services"]:
        if entry["kind"] == "ui" and entry["provider"] in states:
            entry = copy.deepcopy(entry)
            entry["state"] = states[entry["provider"]]
            entry.pop("content_hash", None)
            entry["content_hash"] = content_hash(entry)
            services.append(entry)
        elif not entry["provider"].startswith("papaia-"):
            services.append(entry)
    document["services"] = services
    path = config / "platform" / "services.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    workspace, config = tmp_path / "workspace", tmp_path / "config"
    (config / "manager").mkdir(parents=True)
    workspace.mkdir()
    module_catalog.reset_readers()
    return workspace, config


def _by_name(view: module_catalog.ModuleCatalog) -> dict[str, module_catalog.Module]:
    return {m.name: m for m in view.modules}


# -- the four modules ------------------------------------------------------------------


def test_nothing_installed_shows_every_module_of_the_suite_as_available(
    dirs: tuple[Path, Path],
) -> None:
    workspace, config = dirs
    write_bom(workspace)
    view = module_catalog.read_catalog(str(workspace), str(config))
    assert view.bom.suite == _SUITE
    assert view.bom.verified is True
    assert view.bom.error is None
    found = _by_name(view)
    assert list(found) == ["papaia-studio", "papaia-data", "papaia-flow", "papaia-assist"]
    assert {m.state for m in found.values()} == {"available"}
    assert {n: m.version for n, m in found.items()} == _MODULES
    assert [m.title["en"] for m in found.values()] == ["Studio", "Data", "Flow", "Assistant"]
    assert all(m.url is None for m in found.values())


def test_installed_modules_show_their_state_and_the_running_ones_a_link(
    dirs: tuple[Path, Path],
) -> None:
    workspace, config = dirs
    write_bom(workspace)
    write_catalog(config, {"papaia-flow": "running", "papaia-data": "stopped"})
    found = _by_name(module_catalog.read_catalog(str(workspace), str(config)))
    assert found["papaia-flow"].state == "running"
    assert found["papaia-flow"].url == "https://flow.example.org/"
    assert found["papaia-flow"].installed_version == "2.0.0"
    assert found["papaia-flow"].version == "0.1.0"
    assert found["papaia-data"].state == "stopped"
    assert found["papaia-data"].url is None
    assert found["papaia-studio"].state == "available"
    assert found["papaia-assist"].state == "available"


def test_a_removed_bundle_goes_back_to_available(dirs: tuple[Path, Path]) -> None:
    workspace, config = dirs
    write_bom(workspace)
    path = write_catalog(config, {"papaia-flow": "running"})
    assert _by_name(module_catalog.read_catalog(str(workspace), str(config)))[
        "papaia-flow"
    ].state == "running"
    write_catalog(config, {})
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    module_catalog.reset_readers()
    assert _by_name(module_catalog.read_catalog(str(workspace), str(config)))[
        "papaia-flow"
    ].state == "available"


def test_an_invalid_catalog_keeps_the_last_valid_one(dirs: tuple[Path, Path]) -> None:
    workspace, config = dirs
    write_bom(workspace)
    path = write_catalog(config, {"papaia-flow": "running"})
    module_catalog.read_catalog(str(workspace), str(config))
    path.write_text('{"format": "papaia.platform-services/1", "unknown": 1}', encoding="utf-8")
    view = module_catalog.read_catalog(str(workspace), str(config), refresh=True)
    assert _by_name(view)["papaia-flow"].state == "running"
    assert view.catalog_error is not None


# -- the bill of materials -------------------------------------------------------------


def test_a_changed_bill_of_materials_is_refused(dirs: tuple[Path, Path]) -> None:
    workspace, config = dirs
    path = write_bom(workspace)
    path.write_bytes(path.read_bytes().replace(b"0.2.0", b"9.9.9"))
    view = module_catalog.read_catalog(str(workspace), str(config))
    assert view.modules == ()
    assert view.bom.verified is False
    assert view.bom.error == "signature"


def test_a_signature_of_another_key_is_refused(dirs: tuple[Path, Path]) -> None:
    workspace, config = dirs
    write_bom(workspace, key=_OTHER_KEY)
    view = module_catalog.read_catalog(str(workspace), str(config))
    assert view.modules == ()
    assert view.bom.error == "signature"


def test_without_a_pinned_key_versions_show_as_not_verified(dirs: tuple[Path, Path]) -> None:
    workspace, config = dirs
    write_bom(workspace, pin_key=False)
    view = module_catalog.read_catalog(str(workspace), str(config))
    assert view.bom.verified is False
    assert view.bom.error is None
    assert len(view.modules) == 4


def test_without_a_bill_of_materials_there_is_no_module(dirs: tuple[Path, Path]) -> None:
    workspace, config = dirs
    view = module_catalog.read_catalog(str(workspace), str(config))
    assert view.modules == ()
    assert view.bom.error == "missing"


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b.update(schema="papaia.suite-bom/2"),
        lambda b: b.update(suite="1.9.0"),
        lambda b: b.update(extra=True),
        lambda b: b["repos"].append({"name": "Bad Name", "role": "module", "version": "1"}),
        lambda b: b["repos"][2].update(version="1.0; rm"),
        lambda b: b.update(repos="papaia-flow"),
    ],
)
def test_a_malformed_bill_of_materials_is_refused(dirs: tuple[Path, Path], change: Any) -> None:
    workspace, config = dirs
    bom = _bom()
    change(bom)
    write_bom(workspace, bom)
    view = module_catalog.read_catalog(str(workspace), str(config))
    assert view.modules == ()
    assert view.bom.error == "invalid"


def test_an_oversized_bill_of_materials_is_refused(dirs: tuple[Path, Path]) -> None:
    workspace, config = dirs
    path = write_bom(workspace, sign=False, pin_key=False)
    path.write_bytes(b"#" * (module_catalog.MAX_BOM_BYTES + 1))
    view = module_catalog.read_catalog(str(workspace), str(config))
    assert view.bom.error == "invalid"


def test_a_version_file_cannot_point_outside_compat(dirs: tuple[Path, Path]) -> None:
    workspace, config = dirs
    write_bom(workspace)
    (workspace / "papaia" / "VERSION").write_text("../../etc/passwd\n", encoding="utf-8")
    view = module_catalog.read_catalog(str(workspace), str(config))
    assert view.bom.error == "missing"
    assert view.modules == ()


# -- HTTP ------------------------------------------------------------------------------


_CSRF = "test-csrf-token-value"


@pytest.fixture
def client(dirs: tuple[Path, Path]) -> Iterator[TestClient]:
    workspace, config = dirs
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config), "papaia_workspace_dir": str(workspace)}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


def _as(client: TestClient, *roles: str) -> TestClient:
    session = {
        "user": {
            "sub": "u-1",
            "preferred_username": "dora",
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


def test_the_api_lists_the_modules_for_an_administrator(
    client: TestClient, dirs: tuple[Path, Path]
) -> None:
    workspace, config = dirs
    write_bom(workspace)
    write_catalog(config, {"papaia-flow": "running"})
    response = _as(client, "admin").get("/api/v1/modules")
    assert response.status_code == 200
    body = response.json()
    assert body["bom"] == {"suite": _SUITE, "verified": True, "error": None}
    flow = next(m for m in body["modules"] if m["name"] == "papaia-flow")
    assert flow == {
        "name": "papaia-flow",
        "title": "Flow",
        "description": "",
        "version": "0.1.0",
        "installed_version": "2.0.0",
        "state": "running",
        "url": "https://flow.example.org/",
    }


def test_the_api_refuses_a_dashboard_user(client: TestClient, dirs: tuple[Path, Path]) -> None:
    write_bom(dirs[0])
    assert _as(client, "user").get("/api/v1/modules").status_code == 403


def test_the_page_renders_the_catalog_in_german(
    client: TestClient, dirs: tuple[Path, Path]
) -> None:
    workspace, config = dirs
    write_bom(workspace)
    write_catalog(config, {"papaia-data": "stopped"})
    response = _as(client, "admin").get("/modules", headers={"Accept-Language": "de"})
    assert response.status_code == 200
    text = response.text
    assert "Modulkatalog" in text
    for title in ("Studio", "Data", "Flow", "Assistant"):
        assert title in text
    assert "verfügbar" in text
    assert "nicht erreichbar" in text
    assert "0.2.0" in text
