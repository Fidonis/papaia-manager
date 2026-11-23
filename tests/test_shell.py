"""The manager in the platform's shared frame (shell contract of papaia-kit, T-UI-01).

Every page of the manager is the content of `main` inside the kit's frame: brand, module
switcher from the platform catalog, navigation, footer with the account menu, page header.
The frame is display only: what the switcher or the navigation leaves out grants or withholds
nothing, the routes check the roles themselves (covered in test_access_control.py).
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-shell-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-shell-workspace-")

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

import papaia_kit  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402
from papaia_kit._resources import contract_dir  # noqa: E402
from papaia_kit.ui import normalize_shell  # noqa: E402

from app import shell  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import module_catalog, user_prefs  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.main import create_app  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SHELL = contract_dir() / "shell"
INPUTS = sorted((SHELL / "inputs").glob("*.json"))
_CSRF = "shell-test-csrf-token-0001"
KIT = papaia_kit.__version__


# -- fixtures ------------------------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    return path


@pytest.fixture
def client(config_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    get_settings.cache_clear()
    module_catalog.reset_readers()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config_dir), "papaia_workspace_dir": str(workspace)}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()
    module_catalog.reset_readers()


def _as(client: TestClient, *roles: str, **extra: Any) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-anna",
            "preferred_username": "anna",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
        **extra,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    client.cookies.clear()
    client.cookies.set("papaia_manager_session", signed)
    return client


def _reference_catalog(name: str = "rs-anna-de-light") -> dict[str, Any]:
    doc = json.loads((SHELL / "inputs" / f"{name}.json").read_text(encoding="utf-8"))
    catalog: dict[str, Any] = doc["catalog"]
    return catalog


def _install_catalog(config_dir: Path, name: str = "rs-anna-de-light") -> dict[str, Any]:
    platform = config_dir / "platform"
    platform.mkdir(exist_ok=True)
    catalog = _reference_catalog(name)
    (platform / "services.json").write_text(json.dumps(catalog), encoding="utf-8")
    shutil.copy(SHELL / "inputs" / "branding" / "logo.svg", platform / "brand-logo")
    return catalog


def _between(body: str, start: str, end: str) -> str:
    return body[body.index(start) : body.index(end, body.index(start))]


def _nav_groups(body: str) -> dict[str, list[str]]:
    """Heading -> the rows under it, in order; the fixed first row is under ''."""
    nav = _between(body, '<nav class="pk-nav"', "</nav>")
    parts = re.split(r'<h2 id="pk-nav-g\d+" class="pk-nav-group">([^<]+)</h2>', nav)
    labels = r'<span class="pk-label">([^<]+?)\s*(?:<|</span>)'
    groups = {"": [html.unescape(x) for x in re.findall(labels, parts[0])]}
    for heading, chunk in zip(parts[1::2], parts[2::2], strict=True):
        groups[html.unescape(heading)] = [html.unescape(x) for x in re.findall(labels, chunk)]
    return groups


def _audit_actions(config_dir: Path) -> list[str]:
    path = audit_path(str(config_dir))
    if not path.exists():
        return []
    return [json.loads(line)["action"] for line in path.read_text().splitlines() if line]


# -- K3: the markup contract ---------------------------------------------------------------


@pytest.mark.parametrize("path", INPUTS, ids=lambda p: p.stem)
def test_every_reference_input_renders_the_expected_frame(path: Path) -> None:
    """The manager renders each reference input as the checking module (shell.md 10)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    expected = (SHELL / "expected" / f"{path.stem}.html").read_text(encoding="utf-8")
    assert normalize_shell(shell.render_input(doc)) == expected


def test_the_module_command_prints_the_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = SHELL / "inputs" / "rs-dora-en-light.json"
    assert shell.main(["--input", str(source)]) == 0
    expected = (SHELL / "expected" / "rs-dora-en-light.html").read_text(encoding="utf-8")
    assert normalize_shell(capsys.readouterr().out) == expected


def test_the_module_command_refuses_a_broken_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = json.loads((SHELL / "inputs" / "rs-anna-de-light.json").read_text(encoding="utf-8"))
    doc["page"]["primary_action"]["href"] = "javascript:alert(1)"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(doc), encoding="utf-8")
    assert shell.main(["--input", str(bad)]) == 1
    assert "shell input rejected" in capsys.readouterr().err
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (1024 * 1024 + 1))
    assert shell.main(["--input", str(big)]) == 2


# -- the frame on every page ---------------------------------------------------------------


def test_a_page_is_the_content_of_the_kit_frame(client: TestClient) -> None:
    body = _as(client, "admin").get("/host").text

    assert f'<meta name="papaia-kit" content="{KIT}">' in body
    assert f'<link rel="stylesheet" href="/_kit/{KIT}/css/papaia-kit.css">' in body
    zones = re.findall(r'data-pk-zone="([a-z-]+)"', body)
    assert zones == ["brand", "switcher", "nav", "footer", "page-header"]
    assert body.count("<h1") == 1
    assert body.count('<main id="pk-main"') == 1
    # The skip link is the first focusable element.
    assert re.search(r"<(a|button|input|select|textarea|summary)\b", body).group(0) == "<a"  # type: ignore[union-attr]
    assert 'class="pk-skip" href="#pk-main"' in body
    # The manager's own frame is gone.
    for gone in ("app-sidebar", "sidebar-label", "data-sidebar", "papaia-theme", "toggleSidebar"):
        assert gone not in body


def test_the_title_is_page_module_installation(client: TestClient) -> None:
    body = _as(client, "admin").get("/upgrade").text
    assert "<title>Upgrade – Manager – papAIa manager</title>" in body
    assert "<h1>Upgrade</h1>" in body


def test_the_manager_styles_and_scripts_stay_inside_main(client: TestClient) -> None:
    """Head and frame are exactly the kit's; the manager's own files belong to the content."""
    body = _as(client, "admin").get("/").text
    head = _between(body, "<head>", "</head>")
    assert "app.css" not in head and "htmx" not in head and "alpine" not in head
    main = _between(body, '<main id="pk-main"', "</main>")
    assert "/static/app.css" in main
    assert "/static/htmx.min.js" in main
    assert 'hx-headers=' in main


def test_no_file_comes_from_another_host(client: TestClient) -> None:
    body = _as(client, "admin").get("/").text
    for url in re.findall(r'<(?:link|script|img)\b[^>]*(?:href|src)="([^"]+)"', body):
        assert url.startswith("/"), url
        assert not url.startswith("//"), url


def test_the_admin_navigation_keeps_its_groups(client: TestClient) -> None:
    groups = _nav_groups(_as(client, "admin").get("/host").text)

    assert list(groups) == ["", "Monitor", "Modules and add-ons", "System"]
    assert groups[""] == ["Overview"]
    assert groups["Monitor"] == ["Services", "Host", "Models & Usage", "Jobs"]
    assert groups["Modules and add-ons"] == ["Modules", "Add-Ons", "Add-on catalogs"]
    assert groups["System"] == ["Backup / Restore", "Upgrade", "Mail", "Audit log", "Settings"]


def test_a_user_without_the_admin_role_sees_only_the_overview(client: TestClient) -> None:
    assert _nav_groups(_as(client, "user").get("/").text) == {"": ["Overview"]}


def test_the_current_row_is_marked(client: TestClient) -> None:
    admin = _as(client, "admin")
    for path in ("/", "/host", "/jobs", "/settings"):
        row = path
        nav = _between(admin.get(path).text, '<nav class="pk-nav"', "</nav>")
        assert re.findall(r'<a href="([^"]+)" aria-current="page"', nav) == [row], path


def test_the_group_headings_carry_no_opacity(client: TestClient) -> None:
    body = _as(client, "admin").get("/host").text
    for heading in re.findall(r'<h2 [^>]*class="pk-nav-group"[^>]*>', body):
        assert "opacity" not in heading


def test_the_navigation_is_in_german_for_a_german_account(
    client: TestClient, config_dir: Path
) -> None:
    user_prefs.set_locale(str(config_dir), "u-anna", "de")
    body = _as(client, "admin").get("/host").text
    assert '<html lang="de"' in body
    groups = _nav_groups(body)
    assert groups[""] == ["Übersicht"]
    assert list(groups)[1] == "Überwachung"


def test_the_page_header_has_the_breadcrumb_of_a_sub_page(client: TestClient) -> None:
    body = _as(client, "admin").get("/jobs/0b5c4a1e-job").text
    crumbs = _between(body, '<nav class="pk-breadcrumb"', "</nav>")
    assert '<a href="/jobs">Jobs</a>' in crumbs
    # The jobs row stays the current one on a job's own page.
    nav = _between(body, '<nav class="pk-nav"', "</nav>")
    assert re.findall(r'<a href="([^"]+)" aria-current="page"', nav) == ["/jobs"]


# -- switcher, brand and account menu from the catalog ------------------------------------


def test_without_a_catalog_the_switcher_names_the_manager_only(client: TestClient) -> None:
    body = _as(client, "admin").get("/").text
    switcher = _between(body, 'data-pk-zone="switcher"', "</div>")
    assert "<details" not in switcher
    assert "Manager" in switcher
    brand = _between(body, 'data-pk-zone="brand"', "</div>")
    # The link is named by its visible text: the name and the second line (kit 0.2.2).
    assert '<a href="/" aria-label="papAIa manager by Fidonis, overview">' in brand


def test_the_switcher_lists_the_visible_modules_of_the_catalog(
    client: TestClient, config_dir: Path
) -> None:
    _install_catalog(config_dir)
    body = _as(client, "papaia-admin", "user", "admin").get("/").text
    switcher = _between(body, '<details class="pk-switcher"', "</details>")
    hrefs = re.findall(r'<a href="([^"]+)"', switcher)
    assert hrefs == [
        "https://chat.example.test/",
        "https://me.example.test/",
        "https://manager.example.test/",
    ]
    assert re.search(r'href="https://manager.example.test/" aria-current="page"', switcher)


def test_a_person_without_the_admin_role_does_not_see_the_manager_entry(
    client: TestClient, config_dir: Path
) -> None:
    _install_catalog(config_dir)
    switcher = _between(
        _as(client, "user").get("/").text, '<details class="pk-switcher"', "</details>"
    )
    assert "manager.example.test" not in switcher
    assert "chat.example.test" in switcher


def test_the_brand_and_my_area_lead_to_mein_bereich(
    client: TestClient, config_dir: Path
) -> None:
    _install_catalog(config_dir)
    body = _as(client, "admin").get("/").text
    brand = _between(body, 'data-pk-zone="brand"', "</div>")
    assert 'href="https://me.example.test/"' in brand
    assert 'src="/_kit/brand/logo?v=' in brand


def test_the_account_menu_has_four_entries_in_order(
    client: TestClient, config_dir: Path
) -> None:
    _install_catalog(config_dir)
    # With Mein Bereich in the catalog the language follows the platform's preference service,
    # which reads the browser when nothing is chosen (test_prefs.py).
    body = _as(client, "admin").get("/", headers={"accept-language": "en"}).text
    menu = _between(body, '<details class="pk-account">', '<a class="pk-drawer-close"')
    top = menu[menu.index("<ul>") :]
    entries = re.findall(
        r'<span class="pk-label">(Language)</span>|<a href="[^"]+">([^<]+)</a>'
        r'|<button type="submit">(Sign out)</button>',
        top,
    )
    assert ["".join(e) for e in entries] == ["Language", "My area", "Help", "Sign out"]
    assert 'action="/auth/logout"' in menu


def test_the_stored_language_is_marked_in_the_menu(client: TestClient, config_dir: Path) -> None:
    user_prefs.set_locale(str(config_dir), "u-anna", "en")
    body = _as(client, "admin").get("/").text
    assert re.search(r'value="en" aria-current="true"', body)
    user_prefs.set_locale(str(config_dir), "u-anna", None)
    assert re.search(r'value="browser" aria-current="true"', _as(client, "admin").get("/").text)


def test_a_catalog_the_shell_refuses_is_left_out_not_half_used(
    client: TestClient, config_dir: Path
) -> None:
    catalog = _install_catalog(config_dir)
    catalog["services"][0]["content_hash"] = "sha256:" + "0" * 64
    (config_dir / "platform" / "services.json").write_text(json.dumps(catalog), "utf-8")
    module_catalog.reset_readers()
    response = _as(client, "admin").get("/")
    assert response.status_code == 200
    assert "chat.example.test" not in response.text


# -- the frame's forms ---------------------------------------------------------------------


def test_the_sidebar_form_sets_the_cookie_and_returns(
    client: TestClient, config_dir: Path
) -> None:
    admin = _as(client, "admin")
    response = admin.post(
        "/_kit/sidebar",
        data={"sidebar": "collapsed", "pk_csrf": _CSRF},
        headers={"referer": "http://localhost:8120/host"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/host"
    assert "pk_sidebar=collapsed" in response.headers["set-cookie"]
    assert "httponly" in response.headers["set-cookie"].lower()
    assert _audit_actions(config_dir) == ["kit.sidebar"]
    client.cookies.set("pk_sidebar", "collapsed")
    assert 'data-pk-sidebar="collapsed"' in client.get("/").text


def test_the_appearance_form_switches_the_theme(client: TestClient, config_dir: Path) -> None:
    admin = _as(client, "admin")
    assert "data-theme" not in _between(admin.get("/").text, "<html", ">")
    response = admin.post("/_kit/appearance", data={"appearance": "dark", "pk_csrf": _CSRF})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert '<html lang="en" data-theme="dark">' in client.get("/").text
    assert _audit_actions(config_dir) == ["kit.appearance"]


def test_the_language_form_stores_the_choice(client: TestClient, config_dir: Path) -> None:
    admin = _as(client, "admin")
    response = admin.post("/_kit/language", data={"language": "de", "pk_csrf": _CSRF})
    assert response.status_code == 303
    assert user_prefs.stored_locale(str(config_dir), "u-anna") == "de"
    assert '<html lang="de"' in client.get("/").text
    admin.post("/_kit/language", data={"language": "browser", "pk_csrf": _CSRF})
    assert user_prefs.stored_locale(str(config_dir), "u-anna") is None
    assert _audit_actions(config_dir) == ["kit.language", "kit.language"]


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/_kit/sidebar", {"sidebar": "wide"}),
        ("/_kit/sidebar", {"sidebar": "pinned", "extra": "1"}),
        ("/_kit/sidebar", {}),
        ("/_kit/appearance", {"appearance": "system"}),
        ("/_kit/appearance", {"appearance": "x" * 5000}),
        ("/_kit/language", {"language": "fr"}),
        ("/_kit/language", {"language": "de", "next": "https://evil.test/"}),
    ],
)
def test_the_forms_refuse_anything_but_their_one_value(
    client: TestClient, config_dir: Path, path: str, data: dict[str, str]
) -> None:
    response = _as(client, "admin").post(path, data={**data, "pk_csrf": _CSRF})
    assert response.status_code == 400
    assert _audit_actions(config_dir) == []


@pytest.mark.parametrize("path", ["/_kit/sidebar", "/_kit/appearance", "/_kit/language"])
def test_the_forms_need_the_csrf_token(client: TestClient, config_dir: Path, path: str) -> None:
    field = path.rsplit("/", 1)[1]
    value = {"sidebar": "pinned", "appearance": "dark", "language": "de"}[field]
    admin = _as(client, "admin")
    assert admin.post(path, data={field: value}).status_code == 403
    wrong = {field: value, "pk_csrf": "wrong-token-value-00"}
    assert admin.post(path, data=wrong).status_code == 403
    assert _audit_actions(config_dir) == []


@pytest.mark.parametrize("path", ["/_kit/sidebar", "/_kit/appearance", "/_kit/language"])
def test_the_forms_need_a_session(client: TestClient, path: str) -> None:
    client.cookies.clear()
    response = client.post(path, data={"pk_csrf": _CSRF})
    assert response.status_code in (303, 307, 401)
    assert "pk_sidebar" not in response.headers.get("set-cookie", "")


def test_a_foreign_referer_returns_to_the_overview(client: TestClient) -> None:
    response = _as(client, "admin").post(
        "/_kit/sidebar",
        data={"sidebar": "pinned", "pk_csrf": _CSRF},
        headers={"referer": "https://evil.test/host"},
    )
    assert response.headers["location"] == "/"


def test_sign_out_accepts_the_frame_token(client: TestClient) -> None:
    response = _as(client, "admin").post("/auth/logout", data={"pk_csrf": _CSRF})
    assert response.status_code == 303


# -- the kit's files and the logo ----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "media"),
    [
        ("css/papaia-kit.css", "text/css"),
        ("js/shell.js", "text/javascript"),
        ("icons/sprite.svg", "image/svg+xml"),
        ("fonts/manrope-latin-800-normal.woff2", "font/woff2"),
    ],
)
def test_the_kit_files_are_served_from_the_vendored_wheel(
    client: TestClient, path: str, media: str
) -> None:
    response = client.get(f"/_kit/{KIT}/{path}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media)
    assert "immutable" in response.headers["cache-control"]
    source = papaia_kit.ui_dist_dir() / path
    assert response.content == source.read_bytes()


@pytest.mark.parametrize(
    "path",
    [
        "/_kit/0.0.1/css/papaia-kit.css",
        f"/_kit/{KIT}/tailwind/preset.cjs",
        f"/_kit/{KIT}/css/..%2F..%2Fglossary%2Fglossary.yaml",
        f"/_kit/{KIT}/css/.hidden.css",
        f"/_kit/{KIT}/css/missing.css",
        f"/_kit/{KIT}/fonts/LICENSE-OFL-1.1-Manrope.txt",
    ],
)
def test_nothing_else_is_served_under_the_kit_path(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_the_logo_is_served_only_while_its_digest_matches(
    client: TestClient, config_dir: Path
) -> None:
    _install_catalog(config_dir)
    response = client.get("/_kit/brand/logo")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "default-src 'none'" in response.headers["content-security-policy"]
    (config_dir / "platform" / "brand-logo").write_text("<svg>changed</svg>", "utf-8")
    assert client.get("/_kit/brand/logo").status_code == 404


def test_without_a_catalog_there_is_no_logo(client: TestClient) -> None:
    assert client.get("/_kit/brand/logo").status_code == 404


# -- help and my area ----------------------------------------------------------------------


def test_help_is_open_to_every_role(client: TestClient) -> None:
    body = _as(client, "user").get("/help").text
    assert "<h1>Help</h1>" in body
    assert "Core feature" in body and "Add-on catalog" in body


def test_my_area_without_a_catalog_is_the_account_page(client: TestClient) -> None:
    body = _as(client, "user").get("/me").text
    assert "<h1>My area</h1>" in body
    assert "anna" in body


def test_help_needs_a_session(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/help").status_code == 307


# -- design tokens --------------------------------------------------------------------------


def test_the_colours_come_from_the_kit_preset() -> None:
    """MN-10: one source for the values; the manager keeps no colour of its own."""
    config = (REPO / "docker" / "tailwind.config.js").read_text(encoding="utf-8")
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", config) is None
    assert "preset.cjs" in config
    dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "papaia_kit/_data/dist/tailwind/preset.cjs" in dockerfile


def test_the_themes_follow_the_frame_theme_attribute() -> None:
    """The kit sets data-theme=light|dark, or nothing for the system setting."""
    config = (REPO / "docker" / "tailwind.config.js").read_text(encoding="utf-8")
    assert re.search(r'\blight:\s*kit\("fidonis-light"\)', config)
    assert re.search(r'\bdark:\s*kit\("fidonis-dark"\)', config)
    assert 'darkTheme: "dark"' in config
    css = (REPO / "docker" / "tailwind.app.css").read_text(encoding="utf-8")
    assert "fidonis-dark" not in css and "fidonis-light" not in css


def test_no_rail_is_forced_below_1024_px() -> None:
    css = (REPO / "docker" / "tailwind.app.css").read_text(encoding="utf-8")
    assert "max-width: 1023px" not in css
    assert ".app-sidebar" not in css and "data-sidebar" not in css


def test_no_font_of_its_own_and_no_global_reset() -> None:
    """Fonts come from the kit; a global reset would override the frame (unlayered wins)."""
    config = (REPO / "docker" / "tailwind.config.js").read_text(encoding="utf-8")
    assert "preflight: false" in config
    assert "base: false" in config
    brand = (REPO / "docker" / "tailwind.brand.css").read_text(encoding="utf-8")
    assert "@font-face" not in brand
    dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "@fontsource" not in dockerfile


def test_no_caption_in_the_content_is_dimmed_with_opacity() -> None:
    """Captions use the muted text colour of the tokens, which keeps the contrast pairs."""
    templates = REPO / "src" / "app" / "templates"
    for path in templates.rglob("*.html"):
        for classes in re.findall(r"""["']([^"'\n]*\buppercase\b[^"'\n]*)["']""", path.read_text()):
            assert not re.search(r"\bopacity-\d+\b", classes), (path.name, classes)
