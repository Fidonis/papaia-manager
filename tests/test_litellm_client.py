"""The LiteLLM client: what it sends, what it refuses to send, and what it says when it fails.

The master key is the strongest credential of the stack, so most of this file is about what
the client does not do: it sends nothing but reads to the three routes the Models page needs,
and neither the key nor an address ends up in a message that is shown on a page.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from app.core import litellm
from app.core.litellm import (
    KEY_REFUSED,
    NO_KEY,
    READ_PATHS,
    LitellmClient,
    LitellmRejected,
    LitellmSecrets,
    LitellmUnavailable,
    litellm_secrets,
    public_ui_url,
)
from tests.fake_litellm import MASTER_KEY, FakeLitellm

_URL = "http://litellm:4000"
_START = date(2026, 10, 18)
_END = date(2026, 10, 24)


def _client(fake: FakeLitellm, key: str = MASTER_KEY, url: str = _URL) -> LitellmClient:
    return LitellmClient(url, key, transport=fake.transport)


# ── what it sends ───────────────────────────────────────────────────────────────────────────


async def test_the_three_calls_are_gets_to_the_routes_on_the_list() -> None:
    fake = FakeLitellm()
    fake.use(_END, "gpt-4o-mini", prompt=1, completion=1, ok=1)

    async with _client(fake) as client:
        health = await client.health()
        info = await client.model_info()
        results, complete = await client.daily_activity(_START, _END)

    assert health == {"status": "healthy", "db": "connected"}
    assert {item["model_name"] for item in info} >= {"gpt-4o-mini", "never-used"}
    assert complete and len(results) == 1
    assert fake.methods == {"GET"}
    assert {path for _, path in fake.requests} == READ_PATHS


async def test_a_route_that_is_not_on_the_list_is_never_sent() -> None:
    fake = FakeLitellm()
    client = _client(fake)

    for path in ("/key/generate", "/model/new", "/user/new", "/health", "/v1/chat/completions"):
        with pytest.raises(ValueError, match="not a read route"):
            await client._get(path)
    await client.aclose()

    assert fake.requests == []


async def test_the_key_goes_in_one_header_and_the_open_route_gets_none() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={"status": "healthy", "db": "connected", "data": []})

    client = LitellmClient(_URL, "sk-one-key", transport=httpx.MockTransport(handler))
    await client.health()
    await client.model_info()
    await client.aclose()

    assert seen == [("/health/readiness", None), ("/model/info", "Bearer sk-one-key")]


async def test_the_dates_are_sent_as_utc_days_and_today_is_included() -> None:
    fake = FakeLitellm()

    async with _client(fake) as client:
        await client.daily_activity(_START, _END)

    assert fake.params[0] == {
        "start_date": "2026-10-18",
        "end_date": "2026-10-24",
        "include_current_utc_day": "true",
        "page": "1",
        "page_size": str(litellm.PAGE_SIZE),
    }


# ── a failure, in words ─────────────────────────────────────────────────────────────────────


async def test_without_a_key_nothing_that_needs_one_is_sent() -> None:
    fake = FakeLitellm()

    async with _client(fake, key="") as client:
        with pytest.raises(LitellmUnavailable) as raised:
            await client.model_info()
        with pytest.raises(LitellmUnavailable):
            await client.daily_activity(_START, _END)
        # The readiness route needs no key and is the one that may be asked.
        await client.health()

    assert raised.value.problem.msgid == NO_KEY
    assert fake.requests == [("GET", "/health/readiness")]


@pytest.mark.parametrize("status", [401, 403])
async def test_a_key_litellm_refuses_is_unavailable_and_the_text_does_not_hold_it(
    status: int,
) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(status, json={"error": "no"}))
    client = LitellmClient(_URL, "sk-the-refused-key", transport=transport)

    with pytest.raises(LitellmUnavailable) as raised:
        await client.model_info()
    await client.aclose()

    assert raised.value.problem.msgid == KEY_REFUSED
    assert "sk-the-refused-key" not in str(raised.value)


async def test_an_error_status_is_a_rejection_with_litellms_own_words() -> None:
    fake = FakeLitellm()
    fake.fail = True

    async with _client(fake) as client:
        with pytest.raises(LitellmRejected) as raised:
            await client.daily_activity(_START, _END)

    assert str(raised.value) == "LiteLLM answered with an error: database is gone"


async def test_an_error_without_a_message_is_shown_as_its_status() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(502, text="Bad gateway"))
    client = LitellmClient(_URL, MASTER_KEY, transport=transport)

    with pytest.raises(LitellmRejected) as raised:
        await client.model_info()
    await client.aclose()

    assert str(raised.value) == "LiteLLM answered with an error: HTTP 502"


async def test_a_long_error_message_is_cut() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(500, json={"error": {"message": "x" * 5000}})
    )
    client = LitellmClient(_URL, MASTER_KEY, transport=transport)

    with pytest.raises(LitellmRejected) as raised:
        await client.model_info()
    await client.aclose()

    assert len(str(raised.value)) < 400


async def test_a_litellm_that_cannot_be_reached_is_named_by_host_and_port_only() -> None:
    fake = FakeLitellm()
    fake.down = True

    async with _client(fake, url="https://user:secret@litellm.internal:4000/base/") as client:
        with pytest.raises(LitellmUnavailable) as raised:
            await client.model_info()

    message = str(raised.value)
    assert message == "LiteLLM is not reachable at litellm.internal:4000 (ConnectError)."
    for leak in ("secret", "user", "/base", "https", MASTER_KEY):
        assert leak not in message


@pytest.mark.parametrize(
    "body", [b"<html>nope</html>", b"[]", b'{"data": "not a list"}', b'{"results": 3}']
)
async def test_an_answer_the_manager_cannot_use_is_a_rejection_not_a_crash(body: bytes) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    client = LitellmClient(_URL, MASTER_KEY, transport=transport)

    with pytest.raises(LitellmRejected):
        await client.model_info()
    with pytest.raises(LitellmRejected):
        await client.daily_activity(_START, _END)
    await client.aclose()


# ── pages ───────────────────────────────────────────────────────────────────────────────────


async def test_pages_are_read_until_litellm_says_there_are_no_more_and_a_day_may_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(litellm, "PAGE_SIZE", 2)
    fake = FakeLitellm()
    fake.split_days = True
    for model in ("gpt-4o-mini", "chat-large", "qwen-local"):
        fake.use(_END, model, prompt=10, completion=10, ok=1)

    async with _client(fake) as client:
        results, complete = await client.daily_activity(_START, _END)

    assert complete
    assert fake.calls("/user/daily/activity") == 2
    assert [item["date"] for item in results] == ["2026-10-24"] * 3


async def test_a_runaway_is_stopped_and_said_to_be_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(litellm, "PAGE_SIZE", 1)
    monkeypatch.setattr(litellm, "MAX_PAGES", 2)
    fake = FakeLitellm()
    fake.split_days = True
    for model in ("gpt-4o-mini", "chat-large", "qwen-local"):
        fake.use(_END, model, prompt=10, completion=10, ok=1)

    async with _client(fake) as client:
        results, complete = await client.daily_activity(_START, _END)

    assert not complete
    assert len(results) == 2
    assert fake.calls("/user/daily/activity") == 2


# ── the key and the address ─────────────────────────────────────────────────────────────────


def test_the_key_is_read_from_the_modules_env_and_hidden_from_a_repr(tmp_path: Path) -> None:
    env = tmp_path / "ai" / "litellm" / ".env"
    env.parent.mkdir(parents=True)
    env.write_text("OTHER=1\nLITELLM_MASTER_KEY=  sk-from-file  \n", encoding="utf-8")

    secrets = litellm_secrets(str(tmp_path))

    assert secrets.master_key == "sk-from-file"
    assert "sk-from-file" not in repr(secrets)
    assert "sk-from-file" not in repr(LitellmSecrets(master_key="sk-from-file"))


def test_a_stack_without_the_file_has_no_key(tmp_path: Path) -> None:
    assert litellm_secrets(str(tmp_path)).master_key == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://ai.example.com", "https://ai.example.com/ui"),
        ("https://ai.example.com/", "https://ai.example.com/ui"),
        ("http://localhost:4000", "http://localhost:4000/ui"),
        ("", ""),
        ("javascript:alert(1)", ""),
        ("ai.example.com", ""),
        ('https://ai.example.com/" onmouseover="x', ""),
        ("https://ai.example.com/a b", ""),
    ],
)
def test_the_link_to_litellm_is_a_plain_web_address_or_nothing(
    tmp_path: Path, value: str, expected: str
) -> None:
    (tmp_path / ".env").write_text(f"LITELLM_PUBLIC_URL={value}\n", encoding="utf-8")

    assert public_ui_url(str(tmp_path)) == expected
