"""The Models & Usage page: the shell, its body in every state, and who may open it.

LiteLLM is the in-memory fake of `tests/fake_litellm.py` behind `httpx.MockTransport`; the
manager reads its master key from the configuration directory exactly as it does on a stack.
"The last 7 days" are counted from a fixed day, so the figures below are the sums of what each
test puts into the fake.
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from datetime import date, timedelta
from html import unescape
from pathlib import Path
from typing import Any

import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-models-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-models-workspace-")

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
from app.core import model_usage, usage_view  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers.models_deps import get_litellm_transport  # noqa: E402
from tests.fake_litellm import MASTER_KEY, FakeLitellm  # noqa: E402

TODAY = date(2026, 10, 24)  # a Saturday
_CSRF = "test-csrf-token-value"
_ALICE = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    model_usage.reset_cache()
    monkeypatch.setattr(model_usage, "utc_today", lambda: TODAY)
    monkeypatch.setattr(usage_view, "utc_today", lambda: TODAY)
    yield
    model_usage.reset_cache()


@pytest.fixture
def litellm() -> FakeLitellm:
    """A LiteLLM with a week of traffic and one older day, as the tests below add up."""
    fake = FakeLitellm()
    fake.use(TODAY, "gpt-4o-mini", prompt=1000, completion=2000, spend=0.0021, ok=10, failed=1)
    fake.use(TODAY - timedelta(days=1), "qwen-local", prompt=400, completion=800, ok=4)
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
    (directory / ".env").write_text("LITELLM_PUBLIC_URL=https://ai.test\n", encoding="utf-8")
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


def _browser(app: FastAPI, *roles: str, sub: str = _ALICE) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": sub,
            "preferred_username": "tester",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    browser = TestClient(app, follow_redirects=False)
    browser.cookies.set("papaia_manager_session", signed)
    return browser


@pytest.fixture
def admin(app: FastAPI) -> TestClient:
    return _browser(app, "admin")


def _text(html: str) -> str:
    """What a reader sees, roughly: markup dropped, entities decoded, white space folded."""
    without_scripts = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S)
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", without_scripts)).split())


def _tips(html: str) -> list[dict[str, Any]]:
    """The tooltips of the first chart, as the page hands them to the browser."""
    raw = re.search(r'x-data="\{ i: -1, tips: (.*?) \}"', html, flags=re.S)
    assert raw, "the chart has no tooltips"
    tips: list[dict[str, Any]] = json.loads(unescape(raw.group(1)))
    return tips


# ── the shell ───────────────────────────────────────────────────────────────────────────────


def test_the_shell_loads_its_body_from_the_asked_query(admin: TestClient) -> None:
    response = admin.get("/models?range=30d&metric=usd&view=models")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'id="models-body"' in response.text
    assert 'hx-get="/partials/models?range=30d&amp;metric=usd&amp;view=models"' in response.text
    assert 'hx-trigger="load"' in response.text


def test_the_shell_names_the_page_and_links_to_litellm_when_the_stack_says_where(
    admin: TestClient,
) -> None:
    text = _text(admin.get("/models").text)

    assert "Models & Usage" in text
    assert "Open LiteLLM" in text
    assert 'href="https://ai.test/ui"' in admin.get("/models").text


def test_without_a_public_address_there_is_no_link_to_litellm(
    admin: TestClient, config_dir: Path
) -> None:
    (config_dir / ".env").unlink()

    assert "Open LiteLLM" not in admin.get("/models").text


def test_the_sidebar_lists_the_page_in_the_monitor_group_for_an_administrator(
    admin: TestClient,
) -> None:
    html = admin.get("/models").text

    entries = re.findall(r'<a href="(/[a-z]*)" aria-label="([^"]+)"', html)
    monitor = [href for href, _ in entries[: entries.index(("/jobs", "Jobs")) + 1]]
    assert monitor[-3:] == ["/host", "/models", "/jobs"]
    assert ("/models", "Models & Usage") in entries


# ── who may open it ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/models", "/partials/models"])
def test_an_account_without_the_admin_role_is_turned_away(app: FastAPI, path: str) -> None:
    assert _browser(app, "user").get(path).status_code == 403


@pytest.mark.parametrize("path", ["/models", "/partials/models"])
def test_a_visitor_without_a_session_is_sent_to_log_in(app: FastAPI, path: str) -> None:
    anonymous = TestClient(app, follow_redirects=False)

    assert anonymous.get(path).status_code in (307, 401)


# ── the overview ────────────────────────────────────────────────────────────────────────────


def test_the_default_window_is_the_last_seven_days_and_adds_up(admin: TestClient) -> None:
    response = admin.get("/partials/models")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    text = _text(response.text)
    assert "2026-10-18 to 2026-10-24 · 7 days" in text
    # 3000 + 1200 + 6000 + 200 tokens, of which 6500 input; 0.0021 + 0.03 + 0.001 dollars.
    assert "Tokens 10k 6.5k in · 3.9k out" in text
    assert "Spend $0.03" in text
    assert "Requests 19 1 failed" in text
    # Three of the five models LiteLLM serves were used; the old day is outside the window.
    assert "Models used 3 of 5 2 not used" in text


def test_the_window_buttons_keep_the_chosen_view_and_the_active_one_is_marked(
    admin: TestClient,
) -> None:
    html = admin.get("/partials/models?range=30d&metric=usd").text

    assert re.search(r'href="/models\?metric=usd"[^>]*>\s*Last 7 days', html)
    assert re.search(
        r'href="/models\?range=30d&amp;metric=usd"[^>]*class="btn btn-sm join-item btn-primary"'
        r'[^>]*aria-current="true">Last 30 days',
        html,
    )
    assert "2026-09-25 to 2026-10-24 · 30 days" in _text(html)


def test_a_longer_window_counts_the_older_day_in(admin: TestClient) -> None:
    text = _text(admin.get("/partials/models?range=30d").text)

    assert "Tokens 40k" in text  # the 10 400 of the week and the 30 000 of the old day


def test_this_month_runs_from_the_first_to_today(admin: TestClient) -> None:
    text = _text(admin.get("/partials/models?range=month").text)
    assert "2026-10-01 to 2026-10-24 · 24 days" in text


def test_the_chart_has_a_tooltip_per_day_that_names_what_each_model_consumed(
    admin: TestClient,
) -> None:
    tips = _tips(admin.get("/partials/models").text)

    assert [tip["title"] for tip in tips][-1] == "Sat, Oct 24"
    assert len(tips) == 7
    today = tips[-1]
    assert today["total"] == "3k"
    assert [(row["name"], row["value"]) for row in today["rows"]] == [("gpt-4o-mini", "3k")]
    assert tips[0]["rows"] == [] and tips[0]["total"] == "0"


def test_the_axis_names_the_days_in_full_and_in_short_for_a_phone(admin: TestClient) -> None:
    html = admin.get("/partials/models").text

    assert '<span class="hidden sm:inline">Oct 24</span><span class="sm:hidden">24</span>' in html
    assert '<span class="hidden sm:inline">Oct 18</span><span class="sm:hidden">18</span>' in html


def test_the_metric_switch_redraws_the_same_days_in_dollars_or_requests(admin: TestClient) -> None:
    dollars = _tips(admin.get("/partials/models?metric=usd").text)
    requests_ = _tips(admin.get("/partials/models?metric=requests").text)

    assert dollars[-1]["total"] == "$0.0021"
    assert dollars[-3]["total"] == "$0.03"
    assert requests_[-1]["total"] == "11"


def test_a_model_keeps_its_colour_from_one_window_to_the_next(admin: TestClient) -> None:
    def slot_of(html: str, name: str) -> str:
        found = re.search(
            rf'chart-s(\d)[^"]*" aria-hidden="true"></span><span class="truncate">{name}<', html
        )
        assert found, name
        return found.group(1)

    week = admin.get("/partials/models").text
    month = admin.get("/partials/models?range=30d").text

    assert slot_of(week, "gpt-4o-mini") == slot_of(month, "gpt-4o-mini")
    assert slot_of(week, "chat-large") != slot_of(week, "gpt-4o-mini")


def test_the_legend_lists_every_model_that_has_a_bar_and_none_that_has_not(
    admin: TestClient,
) -> None:
    html = admin.get("/partials/models").text

    legend = re.search(r'<ul class="mt-3 flex[^>]*aria-label="Legend">(.*?)</ul>', html, flags=re.S)
    assert legend
    names = re.findall(r'<span class="truncate">([^<]+)</span>', legend.group(1))
    assert sorted(names) == ["chat-large", "gpt-4o-mini", "qwen-local", "retired-model"]


def test_the_share_list_orders_the_models_by_the_chosen_measure(admin: TestClient) -> None:
    html = admin.get("/partials/models?metric=tokens").text

    section = html[html.index("Share by model") :]
    names = re.findall(
        r'class="link link-hover min-w-0 grow truncate text-sm font-medium">([^<]+)<', section
    )
    assert names == ["chat-large", "gpt-4o-mini", "qwen-local", "retired-model"]


def test_a_window_without_any_use_says_so_and_still_offers_the_controls(
    admin: TestClient, litellm: FakeLitellm
) -> None:
    litellm.usage.clear()

    response = admin.get("/partials/models")

    assert response.status_code == 200
    text = _text(response.text)
    assert "Nothing was used in this period." in text
    assert "Last 30 days" in text


# ── the per-model views ─────────────────────────────────────────────────────────────────────


def test_the_table_lists_every_model_including_the_unused_and_the_removed(
    admin: TestClient,
) -> None:
    html = admin.get("/partials/models?view=models").text
    text = _text(html)

    names = ("bge-m3", "chat-large", "gpt-4o-mini", "never-used", "qwen-local", "retired-model")
    for name in names:
        assert name in text
    assert "no longer served by LiteLLM" in text
    # The prices are per million tokens: 0.00000015 and 0.0000006 dollars per token.
    assert "$0.15 / $0.60" in text
    # A used model without a price has tokens but no spend to show; an unused one is marked.
    assert "no price set" in text
    assert "not used" in text
    assert "A model without a price shows no spend however much it is used." in text


def test_the_table_orders_the_busiest_model_first(admin: TestClient) -> None:
    html = admin.get("/partials/models?view=models").text

    names = re.findall(r'class="link link-hover truncate font-semibold">([^<]+)<', html)
    assert names[:4] == ["chat-large", "gpt-4o-mini", "qwen-local", "retired-model"]


def test_a_model_has_a_page_of_its_own_with_input_and_output_apart(admin: TestClient) -> None:
    html = admin.get("/partials/models?model=chat-large").text
    text = _text(html)

    assert "chat-large" in text
    assert "Price per million tokens, input / output: $3 / $15" in text
    assert "All models" in text
    assert "Input tokens" in text and "Output tokens" in text
    assert "Tokens 6k 5k in · 1k out" in text
    tips = _tips(html)
    assert [row["name"] for row in tips[-3]["rows"]] == ["Input tokens", "Output tokens"]


def test_a_model_that_was_not_used_has_an_empty_chart_and_not_an_error(admin: TestClient) -> None:
    response = admin.get("/partials/models?model=never-used")

    assert response.status_code == 200
    assert "Nothing was used in this period." in _text(response.text)


def test_a_model_without_a_price_says_why_its_spend_is_zero(admin: TestClient) -> None:
    text = _text(admin.get("/partials/models?model=qwen-local").text)

    assert "LiteLLM has no price for this model" in text


def test_a_model_litellm_no_longer_serves_still_shows_what_it_consumed(admin: TestClient) -> None:
    text = _text(admin.get("/partials/models?model=retired-model").text)

    assert "LiteLLM does not serve this model any more" in text
    assert "Tokens 200" in text


def test_an_unknown_model_is_a_notice_above_the_table_not_a_failure(admin: TestClient) -> None:
    response = admin.get("/partials/models?model=nope")

    assert response.status_code == 200
    text = _text(response.text)
    assert "LiteLLM has no model called nope" in text
    assert "chat-large" in text  # the table of models is shown instead


def test_a_model_name_is_not_taken_for_markup(admin: TestClient, litellm: FakeLitellm) -> None:
    hostile = '<img src=x onerror="alert(1)">'
    litellm.use(TODAY, hostile, prompt=1, completion=1, ok=1)

    for query in ("", "?view=models", "?model=" + hostile):
        html = admin.get("/partials/models" + query).text
        assert "<img src=x" not in html
        assert 'onerror="alert(1)"' not in html


def test_the_figures_per_day_are_there_as_a_table(admin: TestClient) -> None:
    html = admin.get("/partials/models").text

    table = html[html.index("Figures per day") :]
    days = re.findall(r'<td class="whitespace-nowrap">([^<]+)</td>', table)
    assert days == [
        "Sun, Oct 18",
        "Mon, Oct 19",
        "Tue, Oct 20",
        "Wed, Oct 21",
        "Thu, Oct 22",
        "Fri, Oct 23",
        "Sat, Oct 24",
    ]


# ── a window that cannot be used ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("range=custom&from=2026-10-20&to=2026-10-10", "The start date is after the end date."),
        ("range=custom&from=2026-10-20&to=2026-10-30", "The end date can't be in the future."),
        ("range=custom&from=2026-10-20", "Enter both dates."),
        ("range=custom&from=yesterday&to=today", "Dates must look like 2026-10-24."),
        ("range=custom&from=2025-01-01&to=2026-10-24", "A window can't be longer than 365 days."),
        ("range=forever", "Unknown time window."),
    ],
)
def test_a_window_that_cannot_be_used_is_told_and_replaced_by_the_default(
    admin: TestClient, query: str, message: str
) -> None:
    response = admin.get("/partials/models?" + query)

    assert response.status_code == 200
    text = _text(response.text)
    assert message in text
    assert "The last 7 days are shown instead." in text
    assert "2026-10-18 to 2026-10-24 · 7 days" in text


def test_a_custom_window_is_applied_and_its_form_keeps_the_dates(admin: TestClient) -> None:
    html = admin.get("/partials/models?range=custom&from=2026-10-14&to=2026-10-16").text

    assert "2026-10-14 to 2026-10-16 · 3 days" in _text(html)
    assert 'name="from" value="2026-10-14"' in html
    assert 'name="to" value="2026-10-16"' in html
    assert 'max="2026-10-24"' in html
    assert "The last 7 days are shown instead." not in html


def test_choosing_custom_asks_for_the_dates_without_calling_it_a_mistake(
    admin: TestClient,
) -> None:
    html = admin.get("/partials/models?range=custom").text

    assert 'name="from"' in html and 'name="to"' in html
    assert "The last 7 days are shown instead." not in html
    assert "Enter both dates." not in html


# ── LiteLLM that cannot be read ─────────────────────────────────────────────────────────────


def test_litellm_that_is_down_is_a_page_that_says_so_and_offers_to_try_again(
    admin: TestClient, litellm: FakeLitellm
) -> None:
    litellm.down = True

    response = admin.get("/partials/models")

    assert response.status_code == 200
    text = _text(response.text)
    assert "Models and usage are not available" in text
    assert "LiteLLM is not reachable at litellm:4000 (ConnectError)." in text
    assert "Try again" in text
    assert "fresh=true" in response.text


def test_a_master_key_litellm_refuses_is_told_without_the_key(
    admin: TestClient, litellm: FakeLitellm
) -> None:
    litellm.master_key = "sk-another-key"

    response = admin.get("/partials/models")

    assert "LiteLLM refused the master key" in _text(response.text)
    assert MASTER_KEY not in response.text


def test_a_stack_without_a_master_key_is_told_where_to_set_it_and_nobody_is_asked(
    admin: TestClient, litellm: FakeLitellm, config_dir: Path
) -> None:
    (config_dir / "ai" / "litellm" / ".env").unlink()

    response = admin.get("/partials/models")

    assert "LITELLM_MASTER_KEY is not set in ai/litellm/.env" in _text(response.text)
    assert litellm.requests == []


def test_an_error_litellm_answers_with_is_shown_in_its_own_words(
    admin: TestClient, litellm: FakeLitellm
) -> None:
    litellm.fail = True

    text = _text(admin.get("/partials/models").text)

    assert "LiteLLM answered with an error: database is gone" in text


def test_an_answer_that_is_not_json_is_unreadable_not_a_crash(
    admin: TestClient, litellm: FakeLitellm
) -> None:
    litellm.garbage = True

    response = admin.get("/partials/models")

    assert response.status_code == 200
    assert "LiteLLM sent an answer the manager could not read." in _text(response.text)


def test_the_master_key_is_on_no_page_in_any_state(
    admin: TestClient, litellm: FakeLitellm
) -> None:
    pages = [
        "/models",
        "/partials/models",
        "/partials/models?view=models",
        "/partials/models?model=chat-large",
    ]
    bodies = [admin.get(page).text for page in pages]
    litellm.down = True
    model_usage.reset_cache()
    bodies.append(admin.get("/partials/models").text)

    assert all(MASTER_KEY not in body for body in bodies)


# ── what the manager sends ──────────────────────────────────────────────────────────────────


def test_the_manager_only_reads_and_only_from_the_routes_it_is_allowed(
    admin: TestClient, litellm: FakeLitellm
) -> None:
    for query in ("", "?view=models", "?model=gpt-4o-mini", "?range=90d", "?fresh=true"):
        admin.get("/partials/models" + query)
    admin.get("/models")

    assert litellm.methods == {"GET"}
    assert {path for _, path in litellm.requests} <= {
        "/health/readiness",
        "/model/info",
        "/user/daily/activity",
    }


def test_a_reload_inside_a_few_seconds_does_not_ask_litellm_again(
    admin: TestClient, litellm: FakeLitellm
) -> None:
    admin.get("/partials/models")
    admin.get("/partials/models?metric=usd")
    admin.get("/partials/models?view=models&fresh=true")

    assert litellm.calls("/user/daily/activity") == 1


# ── another language ────────────────────────────────────────────────────────────────────────


def _german(app: FastAPI) -> TestClient:
    browser = _browser(app, "admin", sub="22222222-2222-2222-2222-222222222222")
    form = {"locale": "de", "csrf_token": _CSRF}
    assert browser.post("/account/language", data=form).status_code in (200, 303)
    return browser


def test_the_page_and_its_body_are_in_the_language_of_the_account(app: FastAPI) -> None:
    german = _german(app)

    shell = _text(german.get("/models").text)
    body = _text(german.get("/partials/models").text)

    assert "Modelle & Verbrauch" in shell
    assert "Letzte 7 Tage" in body
    assert "Last 7 days" not in body


def test_numbers_dates_and_the_state_of_a_litellm_that_is_down_follow_the_language(
    app: FastAPI, litellm: FakeLitellm
) -> None:
    german = _german(app)

    ok = german.get("/partials/models").text
    assert "Sa., 24. Okt." in unescape(ok)
    litellm.down = True
    model_usage.reset_cache()
    down = _text(german.get("/partials/models").text)

    assert "Modelle und Verbrauch sind nicht verfügbar" in down
    assert "nicht erreichbar" in down
