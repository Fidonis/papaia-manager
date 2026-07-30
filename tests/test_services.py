"""Unit tests for core-service status derivation.

All of these run against `docker ps` output as text -- no Docker required,
which is also the state CI runs in.
"""
from __future__ import annotations

from app.core.services import (
    ServiceHealth,
    count_by_health,
    derive_health,
    overall_health,
    parse_ps_line,
    parse_ps_output,
    worst,
)


def _line(
    name: str,
    state: str,
    status: str,
    service: str = "",
    module: str = "",
    role: str = "",
    ports: str = "",
) -> str:
    return "\t".join((name, state, status, service or name, module, role, ports))


# ---------------------------------------------------------------------------
# Health derivation
# ---------------------------------------------------------------------------


def test_running_with_passing_healthcheck() -> None:
    assert derive_health("running", "Up 6 days (healthy)") == ServiceHealth.HEALTHY


def test_running_without_a_healthcheck_counts_as_healthy() -> None:
    # Most of the stack defines one, but librechat-ragapi and friends do not;
    # their absence is not a fault.
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


def test_parses_all_seven_fields() -> None:
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
    module, container = parsed
    assert module == "librechat"
    assert container.service == "librechat"
    assert container.role == "chat-interface"
    assert container.health == ServiceHealth.HEALTHY


def test_duplicate_ipv4_and_ipv6_publish_collapses_to_one_port() -> None:
    parsed = parse_ps_line(
        _line("c", "running", "Up 1 day", ports="0.0.0.0:8000->3080/tcp, :::8000->3080/tcp")
    )
    assert parsed is not None
    assert parsed[1].ports == ["8000"]


def test_container_without_published_ports() -> None:
    parsed = parse_ps_line(_line("c", "running", "Up 1 day"))
    assert parsed is not None
    assert parsed[1].ports == []


def test_container_without_a_module_label_is_grouped_separately() -> None:
    # An add-on sharing the Compose project, or a locally edited core service.
    # Dropping it would understate what is running on the host.
    parsed = parse_ps_line(_line("stray", "running", "Up 1 day"))
    assert parsed is not None
    assert parsed[0] == "other"


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
    modules = parse_ps_output(output)

    assert [m.name for m in modules] == ["keycloak", "litellm", "librechat"]
    assert modules[0].health == ServiceHealth.STOPPED
    assert modules[0].summary == "1 of 2 containers stopped"
    assert modules[1].health == ServiceHealth.STARTING
    assert modules[2].health == ServiceHealth.HEALTHY
    assert [c.name for c in modules[2].containers] == ["lc", "lc-db"]


def test_blank_lines_are_ignored() -> None:
    assert parse_ps_output("\n\n") == []


def test_empty_output_yields_no_modules() -> None:
    assert parse_ps_output("") == []


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_worst_wins() -> None:
    assert (
        worst([ServiceHealth.HEALTHY, ServiceHealth.STARTING, ServiceHealth.STOPPED])
        == ServiceHealth.STOPPED
    )
    assert worst([ServiceHealth.HEALTHY, ServiceHealth.STARTING]) == ServiceHealth.STARTING


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
    modules = parse_ps_output(output)
    assert len(modules) == 1
    assert modules[0].health == ServiceHealth.HEALTHY


def test_overall_health_across_modules() -> None:
    output = "\n".join(
        (
            _line("lc", "running", "Up 6 days (healthy)", module="papaia-librechat"),
            _line("ll", "running", "Up 2 hours (unhealthy)", module="papaia-litellm"),
        )
    )
    modules = parse_ps_output(output)
    assert overall_health(modules) == ServiceHealth.UNHEALTHY


def test_overall_health_of_an_empty_stack_is_unknown() -> None:
    assert overall_health([]) == ServiceHealth.UNKNOWN


def test_counts_cover_every_health_value() -> None:
    output = "\n".join(
        (
            _line("lc", "running", "Up 6 days (healthy)", module="papaia-librechat"),
            _line("kc", "exited", "Exited (1) 1 minute ago", module="papaia-keycloak"),
        )
    )
    counts = count_by_health(parse_ps_output(output))

    assert counts[ServiceHealth.HEALTHY] == 1
    assert counts[ServiceHealth.STOPPED] == 1
    assert counts[ServiceHealth.STARTING] == 0
    # Every member is present, so the routers can index it without a default.
    assert set(counts) == set(ServiceHealth)
