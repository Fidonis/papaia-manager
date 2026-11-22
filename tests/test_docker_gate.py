"""The manager's client of papaia-docker-gate: what it sends, what it accepts back."""
from __future__ import annotations

import httpx
import pytest

from app.core import docker_gate
from app.core.docker_gate import GateClient, GateRefusedError, GateUnavailableError
from tests.fake_docker_gate import Container, FakeGate


def _client(gate: FakeGate) -> GateClient:
    return GateClient("http://papaia-docker-gate:2375", transport=gate.transport())


@pytest.fixture
def gate() -> FakeGate:
    return FakeGate(
        containers=[
            Container("papaia-keycloak-1", "papaia", "keycloak", module="papaia-keycloak"),
            Container("papaia-litellm-1", "papaia", "litellm", state="exited",
                      status="Exited (0) 1 hour ago"),
            Container("other-db-1", "other", "db"),
        ]
    )


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------


def test_the_default_address_is_the_gate_on_its_internal_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(docker_gate.URL_ENV, raising=False)
    assert docker_gate.gate_url() == "http://papaia-docker-gate:2375"


def test_the_address_can_be_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(docker_gate.URL_ENV, "http://127.0.0.1:64600/")
    assert docker_gate.gate_url() == "http://127.0.0.1:64600"


@pytest.mark.parametrize(
    "value",
    [
        "unix:///var/run/docker.sock",
        "https://papaia-docker-gate:2375",
        "http://user:pw@papaia-docker-gate:2375",
        "http://papaia-docker-gate:2375/v1.47",
        "http://papaia-docker-gate:2375?x=1",
        "http://:2375",
        "papaia-docker-gate:2375",
        "http://papaia-docker-gate:notaport",
        "http://" + "a" * 300,
    ],
)
def test_an_address_that_is_not_a_plain_http_origin_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        docker_gate.validate_url(value)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_the_list_asks_for_every_container_and_keeps_what_the_gate_lets_through(
    gate: FakeGate,
) -> None:
    listed = _client(gate).list_containers()

    assert [c.name for c in listed] == ["papaia-keycloak-1", "papaia-litellm-1"]
    keycloak = listed[0]
    assert keycloak.project == "papaia"
    assert keycloak.labels["de.fidonis.module"] == "papaia-keycloak"
    assert keycloak.state == "running"
    assert gate.calls() == [("GET", "/containers/json")]
    assert gate.requests[0].url.params.get("all") == "1"


def test_an_entry_of_the_wrong_shape_is_dropped_not_trusted() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"Id": "x", "Names": "/not-a-list", "State": "running", "Status": "", "Labels": {}},
                {"Id": "y", "Names": ["/ok-1"], "State": "running", "Status": "Up",
                 "Labels": {"com.docker.compose.project": "papaia", "bad": 3}},
                "not an object",
            ],
        )

    client = GateClient("http://gate:2375", transport=httpx.MockTransport(answer))
    listed = client.list_containers()

    assert [c.name for c in listed] == ["ok-1"]
    # A label that is not a string is left out rather than passed on as something else.
    assert listed[0].labels == {"com.docker.compose.project": "papaia"}


def test_an_answer_that_is_not_a_list_is_an_error() -> None:
    client = GateClient(
        "http://gate:2375", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(docker_gate.GateError):
        client.list_containers()


def test_a_gate_that_does_not_answer_says_that_running_without_it_is_not_supported(
    gate: FakeGate,
) -> None:
    gate.down = True
    with pytest.raises(GateUnavailableError) as caught:
        _client(gate).list_containers()

    assert "not supported" in str(caught.value)
    assert "papaia-docker-gate" in str(caught.value)


def test_a_failing_gate_is_an_error_with_its_code(gate: FakeGate) -> None:
    gate.list_status = 502
    with pytest.raises(GateRefusedError) as caught:
        _client(gate).list_containers()
    assert caught.value.status == 502
    assert caught.value.code == "docker_gate.upstream_failed"


def test_ping(gate: FakeGate) -> None:
    _client(gate).ping()
    assert gate.calls() == [("GET", "/_ping")]


# ---------------------------------------------------------------------------
# Start and stop
# ---------------------------------------------------------------------------


def test_stop_names_the_container_the_timeout_and_the_correlation_id(gate: FakeGate) -> None:
    changed = _client(gate).stop_container(
        "papaia-keycloak-1", timeout=30, correlation_id="0190f0e0-aaaa-7bbb-8ccc-123456789abc"
    )

    assert changed is True
    request = gate.requests[-1]
    assert (request.method, request.url.path) == ("POST", "/containers/papaia-keycloak-1/stop")
    assert request.url.params.get("t") == "30"
    assert request.headers["X-Papaia-Correlation-Id"] == "0190f0e0-aaaa-7bbb-8ccc-123456789abc"
    # The gate refuses any body; none is sent.
    assert request.content == b""


def test_start_of_a_running_container_changes_nothing(gate: FakeGate) -> None:
    assert _client(gate).start_container("papaia-keycloak-1") is False
    assert _client(gate).start_container("papaia-litellm-1") is True


def test_a_foreign_container_is_refused_by_the_gate(gate: FakeGate) -> None:
    with pytest.raises(GateRefusedError) as caught:
        _client(gate).stop_container("other-db-1")
    assert caught.value.status == 403
    assert caught.value.code == "docker_gate.foreign_project"


@pytest.mark.parametrize("ref", ["", "../../exec", "a/b", "-x", "a" * 129, "x y", "abc?all=1"])
def test_a_container_reference_that_is_not_a_docker_name_never_leaves_the_manager(
    gate: FakeGate, ref: str
) -> None:
    with pytest.raises(ValueError):
        _client(gate).stop_container(ref)
    assert gate.requests == []


@pytest.mark.parametrize("timeout", [-1, 601])
def test_a_stop_timeout_outside_the_gates_range_is_refused(gate: FakeGate, timeout: int) -> None:
    with pytest.raises(ValueError):
        _client(gate).stop_container("papaia-keycloak-1", timeout=timeout)
    assert gate.requests == []


def test_an_unusable_correlation_id_is_not_forwarded(gate: FakeGate) -> None:
    _client(gate).start_container("papaia-litellm-1", correlation_id="not usable\r\nX: y")
    assert "X-Papaia-Correlation-Id" not in gate.requests[-1].headers
