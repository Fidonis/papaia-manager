"""Unit tests for service status derivation.

All of these run against `docker ps` output as text -- no Docker required,
which is also the state CI runs in.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core import services
from app.core.inventory import ExpectedService
from app.core.services import (
    ServiceHealth,
    ServiceModule,
    apply_restart_policies,
    build_snapshot,
    count_by_health,
    derive_health,
    merge_expected,
    overall_health,
    parse_inspect_output,
    parse_ps_line,
    parse_ps_output,
    worst,
)

CORE = "papaia"


def _line(
    name: str,
    state: str,
    status: str,
    project: str = CORE,
    service: str = "",
    module: str = "",
    role: str = "",
    ports: str = "",
) -> str:
    return "\t".join((name, state, status, project, service or name, module, role, ports))


def _modules(output: str, project: str = CORE) -> list[ServiceModule]:
    """The modules of one project, the way the old flat parser returned them."""
    return parse_ps_output(output).get(project, [])


# ---------------------------------------------------------------------------
# Health derivation
# ---------------------------------------------------------------------------


def test_running_with_passing_healthcheck() -> None:
    assert derive_health("running", "Up 6 days (healthy)") == ServiceHealth.HEALTHY


def test_running_without_a_healthcheck_counts_as_healthy() -> None:
    # Most of the core stack defines one, but librechat-ragapi and friends do
    # not, and add-ons largely do not either; the absence is not a fault.
    assert derive_health("running", "Up 3 days") == ServiceHealth.HEALTHY


def test_running_while_the_healthcheck_still_starts() -> None:
    assert derive_health("running", "Up 12 seconds (health: starting)") == ServiceHealth.STARTING


def test_running_with_a_failing_healthcheck() -> None:
    assert derive_health("running", "Up 2 hours (unhealthy)") == ServiceHealth.UNHEALTHY


def test_clean_exit_is_a_completed_one_shot() -> None:
    assert derive_health("exited", "Exited (0) 2 days ago") == ServiceHealth.COMPLETED


def test_nonzero_exit_is_stopped() -> None:
    assert derive_health("exited", "Exited (1) 3 minutes ago") == ServiceHealth.STOPPED


def test_created_but_never_started() -> None:
    assert derive_health("created", "Created") == ServiceHealth.STARTING


def test_restart_loop_is_not_reported_as_starting() -> None:
    assert derive_health("restarting", "Restarting (1) 5 seconds ago") == ServiceHealth.UNHEALTHY


def test_paused_and_dead_are_stopped() -> None:
    assert derive_health("paused", "Paused") == ServiceHealth.STOPPED
    assert derive_health("dead", "Dead") == ServiceHealth.STOPPED


def test_unrecognised_state_is_unknown() -> None:
    assert derive_health("teleported", "") == ServiceHealth.UNKNOWN


# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------


def test_parses_all_eight_fields() -> None:
    parsed = parse_ps_line(
        _line(
            "papaia-librechat-1",
            "running",
            "Up 6 days (healthy)",
            service="librechat",
            module="papaia-librechat",
            role="chat-interface",
            ports="0.0.0.0:8000->3080/tcp, :::8000->3080/tcp",
        )
    )
    assert parsed is not None
    project, module, container = parsed
    assert project == CORE
    assert module == "librechat"
    assert container.service == "librechat"
    assert container.role == "chat-interface"
    assert container.health == ServiceHealth.HEALTHY


def test_duplicate_ipv4_and_ipv6_publish_collapses_to_one_port() -> None:
    parsed = parse_ps_line(
        _line("c", "running", "Up 1 day", ports="0.0.0.0:8000->3080/tcp, :::8000->3080/tcp")
    )
    assert parsed is not None
    assert parsed[2].ports == ["8000"]


def test_container_without_published_ports() -> None:
    parsed = parse_ps_line(_line("c", "running", "Up 1 day"))
    assert parsed is not None
    assert parsed[2].ports == []


def test_container_without_a_module_label_is_grouped_separately() -> None:
    # A core service whose label was dropped in a local edit, or an add-on that
    # never set one. Dropping it would understate what is running on the host,
    # which is why the ps query carries no label filter.
    parsed = parse_ps_line(_line("stray", "running", "Up 1 day"))
    assert parsed is not None
    assert parsed[1] == "other"


def test_short_line_is_skipped() -> None:
    assert parse_ps_line("only\ttwo") is None


def test_line_without_a_name_is_skipped() -> None:
    assert parse_ps_line(_line("", "running", "Up 1 day")) is None


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_groups_containers_by_module_and_puts_the_worst_first() -> None:
    output = "\n".join(
        (
            _line("lc", "running", "Up 6 days (healthy)", module="papaia-librechat"),
            _line("lc-db", "running", "Up 6 days (healthy)", module="papaia-librechat"),
            _line("kc", "exited", "Exited (1) 3 minutes ago", module="papaia-keycloak"),
            _line("kc-db", "running", "Up 6 days (healthy)", module="papaia-keycloak"),
            _line("ll", "running", "Up 12 seconds (health: starting)", module="papaia-litellm"),
        )
    )
    modules = _modules(output)

    assert [m.name for m in modules] == ["keycloak", "litellm", "librechat"]
    assert modules[0].health == ServiceHealth.STOPPED
    assert modules[0].summary == "1 of 2 containers stopped"
    assert modules[1].health == ServiceHealth.STARTING
    assert modules[2].health == ServiceHealth.HEALTHY
    assert [c.name for c in modules[2].containers] == ["lc", "lc-db"]


def test_containers_are_grouped_per_compose_project() -> None:
    # An add-on runs in its own project. Grouping by module alone would merge a
    # customer add-on into the core stack -- or, worse, merge two papAIa
    # environments sharing one host.
    output = "\n".join(
        (
            _line("lc", "running", "Up 1 day", module="papaia-librechat"),
            _line("pl", "running", "Up 1 day", project="paperless", module="papaia-paperless"),
            _line("other-lc", "running", "Up 1 day", project="papaia-demo",
                  module="papaia-librechat"),
        )
    )
    by_project = parse_ps_output(output)

    assert set(by_project) == {CORE, "paperless", "papaia-demo"}
    assert [m.name for m in by_project["paperless"]] == ["paperless"]


def test_blank_lines_are_ignored() -> None:
    assert parse_ps_output("\n\n") == {}


def test_empty_output_yields_no_projects() -> None:
    assert parse_ps_output("") == {}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_worst_wins() -> None:
    assert (
        worst([ServiceHealth.HEALTHY, ServiceHealth.STARTING, ServiceHealth.STOPPED])
        == ServiceHealth.STOPPED
    )
    assert worst([ServiceHealth.HEALTHY, ServiceHealth.STARTING]) == ServiceHealth.STARTING


def test_a_service_that_was_never_deployed_outranks_one_that_stopped() -> None:
    # A container that exited at least got as far as being created, and its logs
    # are still there. Nothing was ever created for a missing one.
    assert worst([ServiceHealth.STOPPED, ServiceHealth.MISSING]) == ServiceHealth.MISSING


def test_nothing_to_report_is_unknown_not_healthy() -> None:
    assert worst([]) == ServiceHealth.UNKNOWN


def test_a_finished_one_shot_does_not_drag_its_module_down() -> None:
    # localai is model-init (exits 0) next to the inference engine; only the
    # second one says anything about whether the module is serving.
    assert worst([ServiceHealth.COMPLETED, ServiceHealth.HEALTHY]) == ServiceHealth.HEALTHY


def test_a_module_of_nothing_but_one_shots_reports_completed() -> None:
    assert worst([ServiceHealth.COMPLETED, ServiceHealth.COMPLETED]) == ServiceHealth.COMPLETED


def test_model_init_does_not_make_localai_look_broken() -> None:
    output = "\n".join(
        (
            _line("li", "running", "Up 2 days", module="papaia-localai", role="inference-engine"),
            _line(
                "li-init",
                "exited",
                "Exited (0) 2 days ago",
                module="papaia-localai",
                role="model-init",
            ),
        )
    )
    modules = _modules(output)
    assert len(modules) == 1
    assert modules[0].health == ServiceHealth.HEALTHY


def test_overall_health_across_modules() -> None:
    output = "\n".join(
        (
            _line("lc", "running", "Up 6 days (healthy)", module="papaia-librechat"),
            _line("ll", "running", "Up 2 hours (unhealthy)", module="papaia-litellm"),
        )
    )
    assert overall_health(_modules(output)) == ServiceHealth.UNHEALTHY


def test_overall_health_of_an_empty_stack_is_unknown() -> None:
    assert overall_health([]) == ServiceHealth.UNKNOWN


def test_counts_cover_every_health_value() -> None:
    output = "\n".join(
        (
            _line("lc", "running", "Up 6 days (healthy)", module="papaia-librechat"),
            _line("kc", "exited", "Exited (1) 1 minute ago", module="papaia-keycloak"),
        )
    )
    counts = count_by_health(_modules(output))

    assert counts[ServiceHealth.HEALTHY] == 1
    assert counts[ServiceHealth.STOPPED] == 1
    assert counts[ServiceHealth.STARTING] == 0
    # Every member is present, so the routers can index it without a default.
    assert set(counts) == set(ServiceHealth)


# ---------------------------------------------------------------------------
# Declared vs. live
# ---------------------------------------------------------------------------


def _expected(*items: tuple[str, str, str]) -> list[ExpectedService]:
    return [ExpectedService(service=s, module=m, role=r) for s, m, r in items]


def test_a_declared_service_with_no_container_is_missing() -> None:
    modules = merge_expected(
        _modules(_line("lc", "running", "Up 1 day", service="librechat",
                       module="papaia-librechat")),
        _expected(
            ("librechat", "librechat", "chat-interface"),
            ("librechat-mongodb", "librechat", "database"),
        ),
    )

    assert len(modules) == 1
    placeholder = modules[0].containers[1]
    assert placeholder.service == "librechat-mongodb"
    assert placeholder.role == "database"
    assert placeholder.name == ""
    assert placeholder.health == ServiceHealth.MISSING
    assert placeholder.status_text == "not deployed"
    assert modules[0].health == ServiceHealth.MISSING
    assert modules[0].summary == "1 of 2 containers not deployed"


def test_a_module_that_exists_only_in_the_target_state_is_created() -> None:
    # An enabled profile nobody ever started. Before the target state existed
    # this module was simply absent from the page.
    modules = merge_expected([], _expected(("localai", "localai", "inference-engine")))

    assert [m.name for m in modules] == ["localai"]
    assert modules[0].summary == "not deployed"


def test_a_torn_down_stack_reports_every_module_rather_than_nothing() -> None:
    # `papaia-ctl down` removes containers, it does not stop them, so the live
    # view is empty and only the target state can say what is gone.
    modules = merge_expected(
        [],
        _expected(
            ("keycloak", "keycloak", "identity-provider"),
            ("keycloak-postgres", "keycloak", "database"),
            ("librechat", "librechat", "chat-interface"),
        ),
    )

    assert [m.name for m in modules] == ["keycloak", "librechat"]
    assert overall_health(modules) == ServiceHealth.MISSING
    assert count_by_health(modules)[ServiceHealth.MISSING] == 2


def test_a_running_container_outside_the_target_state_is_kept() -> None:
    # A profile removed from COMPOSE_PROFILES while its containers are still up.
    # It is running, which the page has no business hiding.
    modules = merge_expected(
        _modules(_line("sx", "running", "Up 1 day", service="searxng", module="papaia-searxng")),
        _expected(("librechat", "librechat", "chat-interface")),
    )

    assert [m.name for m in modules] == ["librechat", "searxng"]


def test_missing_and_stopped_are_summarised_together() -> None:
    modules = merge_expected(
        _modules(
            "\n".join(
                (
                    _line("lc", "exited", "Exited (1) 1 minute ago", service="librechat",
                          module="papaia-librechat"),
                    _line("lc-db", "running", "Up 1 day", service="librechat-mongodb",
                          module="papaia-librechat"),
                )
            )
        ),
        _expected(
            ("librechat", "librechat", "chat-interface"),
            ("librechat-mongodb", "librechat", "database"),
            ("librechat-ragapi", "librechat", "rag-api"),
        ),
    )

    assert modules[0].summary == "2 of 3 containers missing or stopped"


def test_placeholders_sort_by_service_name_not_to_the_front() -> None:
    modules = merge_expected(
        _modules(_line("a-container", "running", "Up 1 day", service="a-service",
                       module="papaia-librechat")),
        _expected(("z-service", "librechat", "extra")),
    )

    assert [c.sort_key for c in modules[0].containers] == ["a-container", "z-service"]


def test_matching_is_by_compose_service_not_container_name() -> None:
    # Compose names containers `<project>-<service>-<n>`, and `container_name`
    # can override that entirely, so only the service label is reliable.
    modules = merge_expected(
        _modules(_line("papaia-librechat-1", "running", "Up 1 day", service="librechat",
                       module="papaia-librechat")),
        _expected(("librechat", "librechat", "chat-interface")),
    )

    assert len(modules[0].containers) == 1
    assert modules[0].health == ServiceHealth.HEALTHY


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@pytest.fixture
def deployment(tmp_path: Path) -> Path:
    """A config dir naming the core project and one active add-on."""
    (tmp_path / ".env").write_text(
        "COMPOSE_PROJECT_NAME=papaia\nCOMPOSE_PROFILES=librechat\n", encoding="utf-8"
    )
    addon = tmp_path / "addons" / "_managed" / "fidonis" / "paperless"
    addon.mkdir(parents=True)
    (addon / "docker-compose.yml").write_text(
        "services:\n"
        "  paperless:\n"
        "    labels:\n"
        "      de.fidonis.module: papaia-paperless\n"
        "      de.fidonis.role: webserver\n"
        "  paperless-db:\n"
        "    labels:\n"
        "      de.fidonis.module: papaia-paperless\n"
        "      de.fidonis.role: database\n",
        encoding="utf-8",
    )
    (tmp_path / "deployment.yaml").write_text(
        f"addons:\n  - name: paperless\n    path: {addon}\n    active: true\n", encoding="utf-8"
    )
    return tmp_path


def test_snapshot_splits_core_from_addons(deployment: Path) -> None:
    output = "\n".join(
        (
            _line("lc", "running", "Up 1 day", service="librechat", module="papaia-librechat"),
            _line("pl", "running", "Up 1 day", project="paperless", service="paperless",
                  module="papaia-paperless"),
            _line("foreign", "running", "Up 1 day", project="papaia-demo",
                  module="papaia-librechat"),
        )
    )
    snapshot = build_snapshot(str(deployment), str(deployment), output)

    assert [m.name for m in snapshot.core] == ["librechat"]
    assert [m.name for m in snapshot.addons] == ["paperless"]
    # The add-on's second declared service has no container.
    assert snapshot.addons[0].summary == "1 of 2 containers not deployed"


def test_snapshot_discards_another_environment_on_the_same_host(deployment: Path) -> None:
    output = _line("foreign", "running", "Up 1 day", project="papaia-demo",
                   module="papaia-librechat")
    snapshot = build_snapshot(str(deployment), str(deployment), output)

    # Nothing of papaia-demo shows up -- but it is still a running project, which
    # is what `/addons` needs to know about add-ons outside the manifest.
    assert all(c.name != "foreign" for m in snapshot.core for c in m.containers)
    assert "papaia-demo" in snapshot.running_projects


def test_running_projects_ignores_projects_with_no_running_container(deployment: Path) -> None:
    output = "\n".join(
        (
            _line("pl", "exited", "Exited (0) 1 hour ago", project="paperless"),
            _line("up", "running", "Up 1 day", project="something-else"),
        )
    )
    snapshot = build_snapshot(str(deployment), str(deployment), output)

    assert snapshot.running_projects == {"something-else"}


def test_an_unreachable_docker_socket_reports_nothing_not_everything_missing(
    deployment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The declared state alone would say "26 services down". Not knowing is not
    # the same as knowing it is gone, so the page falls back to its empty state.
    monkeypatch.setattr(services, "_CACHE", None)
    monkeypatch.setattr(services, "query_containers", lambda: None)
    snapshot = services.load_snapshot(str(deployment), str(deployment))

    assert snapshot.core == []
    assert snapshot.addons == []
    assert snapshot.running_projects == set()
    # And it is not cached, so the next request retries instead of pinning the
    # empty answer in place for the TTL.
    assert services._CACHE is None


def test_a_papaia_ctl_run_drops_the_cached_reading(
    deployment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def fake_query() -> str:
        calls.append(1)
        return _line("lc", "running", "Up 1 day", service="librechat",
                     module="papaia-librechat")

    monkeypatch.setattr(services, "_CACHE", None)
    monkeypatch.setattr(services, "query_containers", fake_query)
    monkeypatch.setattr(services, "query_restart_policies", lambda names: {})

    services.load_snapshot(str(deployment), str(deployment))
    services.load_snapshot(str(deployment), str(deployment))
    assert len(calls) == 1, "the TTL should absorb a second read"

    # An addon that was just started must not read as stopped for five seconds.
    services.invalidate_snapshot()
    services.load_snapshot(str(deployment), str(deployment))
    assert len(calls) == 2


def test_an_unreadable_workspace_leaves_the_live_view_intact(tmp_path: Path) -> None:
    # No .env, no deployment.yaml, no compose files: the target state is empty
    # and the page degrades to what Docker reported, rather than failing.
    output = _line("lc", "running", "Up 1 day", module="papaia-librechat")
    snapshot = build_snapshot(str(tmp_path), str(tmp_path), output)

    assert [m.name for m in snapshot.core] == ["librechat"]
    assert snapshot.addons == []


# ---------------------------------------------------------------------------
# Restart policies
# ---------------------------------------------------------------------------


def _inspect(*pairs: tuple[str, str]) -> str:
    """`docker inspect` output; the leading slash is how Docker reports names."""
    return "\n".join(f"/{name}\t{policy}" for name, policy in pairs)


def test_parses_name_and_policy_without_the_leading_slash() -> None:
    assert parse_inspect_output(_inspect(("li-init", "no"), ("lc", "unless-stopped"))) == {
        "li-init": "no",
        "lc": "unless-stopped",
    }


def test_blank_and_short_inspect_lines_are_skipped() -> None:
    assert parse_inspect_output("\n\nlc\n") == {}


def _completed_module(name: str = "lc") -> list[ServiceModule]:
    """One module holding a single container that exited cleanly."""
    return _modules(_line(name, "exited", "Exited (0) 5 minutes ago", module="papaia-librechat"))


def test_a_service_that_exited_cleanly_is_stopped_not_completed() -> None:
    for policy in ("unless-stopped", "always"):
        modules = apply_restart_policies(_completed_module(), {"lc": policy})
        assert modules[0].containers[0].health == ServiceHealth.STOPPED


def test_a_one_shot_that_exited_cleanly_stays_completed() -> None:
    for policy in ("no", "on-failure", ""):
        modules = apply_restart_policies(_completed_module(), {"lc": policy})
        assert modules[0].containers[0].health == ServiceHealth.COMPLETED


def test_a_container_docker_said_nothing_about_keeps_its_parsed_health() -> None:
    # An unanswered lookup must not invent an outage.
    modules = apply_restart_policies(_completed_module(), {})
    assert modules[0].containers[0].health == ServiceHealth.COMPLETED


def test_a_stopped_service_takes_its_whole_module_down() -> None:
    # The reported bug: LibreChat stopped, four sibling containers still up,
    # and the module kept reading healthy because a clean exit was taken for a
    # finished one-shot.
    output = "\n".join(
        (
            _line("lc", "exited", "Exited (0) 5 minutes ago", module="papaia-librechat"),
            _line("lc-db", "running", "Up 25 minutes (healthy)", module="papaia-librechat"),
            _line("lc-search", "running", "Up 25 minutes", module="papaia-librechat"),
            _line("lc-rag", "running", "Up 25 minutes", module="papaia-librechat"),
            _line("lc-vectordb", "running", "Up 25 minutes (healthy)", module="papaia-librechat"),
        )
    )
    modules = apply_restart_policies(_modules(output), {"lc": "unless-stopped"})

    assert modules[0].health == ServiceHealth.STOPPED
    assert modules[0].summary == "1 of 5 containers stopped"
    assert overall_health(modules) == ServiceHealth.STOPPED


def test_model_init_still_does_not_make_localai_look_broken() -> None:
    output = "\n".join(
        (
            _line("li", "running", "Up 2 days", module="papaia-localai", role="inference-engine"),
            _line(
                "li-init",
                "exited",
                "Exited (0) 2 days ago",
                module="papaia-localai",
                role="model-init",
            ),
        )
    )
    modules = apply_restart_policies(_modules(output), {"li-init": "no"})

    assert modules[0].health == ServiceHealth.HEALTHY
    assert modules[0].containers[1].health == ServiceHealth.COMPLETED


def test_a_module_turning_stopped_moves_to_the_front() -> None:
    # parse_ps_output sorted this module last while it still looked healthy.
    output = "\n".join(
        (
            _line("kc", "running", "Up 6 days (health: starting)", module="papaia-keycloak"),
            _line("lc", "exited", "Exited (0) 5 minutes ago", module="papaia-librechat"),
        )
    )
    assert [m.name for m in _modules(output)] == ["keycloak", "librechat"]

    modules = apply_restart_policies(_modules(output), {"lc": "unless-stopped"})
    assert [m.name for m in modules] == ["librechat", "keycloak"]
