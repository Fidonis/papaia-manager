"""The `knowledge_map_finding` event: new or changed high findings of the knowledge map, from
the render's lints.yaml and from the manager's own model check, reported once and remembered
across restarts in the notification state file.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.core import knowledge_map as km
from app.core import knowledge_map_governance as gov
from app.core.model_usage import ModelEntry
from app.core.notifications import detect
from app.core.notifications.model import INFO_BY_EVENT, Event, EventSettings, Severity
from app.core.notifications.store import load_state, update_state

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
SETTINGS = EventSettings()


def _config(tmp_path: Path, findings: list[dict[str, Any]], *, active: bool = True) -> str:
    (tmp_path / "knowledge-map" / "manifests").mkdir(parents=True)
    profiles = "librechat,knowledge-map" if active else "librechat"
    (tmp_path / ".env").write_text(f"COMPOSE_PROFILES={profiles}\n")
    _lints(tmp_path, findings)
    return str(tmp_path)


def _lints(base: Path, findings: list[dict[str, Any]]) -> None:
    (base / "knowledge-map" / "lints.yaml").write_text(yaml.safe_dump({"findings": findings}))


def _f(code: str, target: str, severity: str = "high", **extra: Any) -> dict[str, Any]:
    return {"severity": severity, "code": code, "target": target, "message": f"{code} {target}",
            "origin": "render", "category": "governance", "fix": f"fix {target}", **extra}


def _pass(config_dir: str, live: gov.LiveModels | None = None) -> list[detect.Condition]:
    """One pass as the service makes it: detect, then decide against the stored state."""
    findings = detect.knowledge_map_findings(config_dir, live, now=NOW)
    return update_state(
        config_dir, lambda state: detect.decide(findings, state, now=NOW, settings=SETTINGS)
    )


def test_the_event_is_in_the_catalogue() -> None:
    assert INFO_BY_EVENT[Event.KNOWLEDGE_MAP_FINDING].severity is Severity.WARNING


def test_only_high_findings_without_exception_are_reported(tmp_path: Path) -> None:
    config = _config(tmp_path, [
        _f("confidential_in_free_chat", "erp.main"),
        _f("personal_data_unknown", "wiki.main", severity="medium"),
        _f("open_confidential", "wiki.main", exception={"expires": "2099-01-01"}),
        {**_f("dead_access_path", "crm.main"), "category": "quality"},
    ])
    new = _pass(config)
    assert [c.key for c in new] == [
        "render:confidential_in_free_chat:erp.main", "render:dead_access_path:crm.main"]
    first = new[0]
    assert first.event is Event.KNOWLEDGE_MAP_FINDING and first.subject == "erp.main"
    assert first.details["fix"] == "fix erp.main" and first.details["category"] == "governance"


def test_a_finding_is_reported_once_even_across_restarts(tmp_path: Path) -> None:
    config = _config(tmp_path, [_f("agent_model_policy", "agent.offer")])
    assert len(_pass(config)) == 1
    # a restart: nothing in memory, only the state file
    assert "knowledge_map_finding|render:agent_model_policy:agent.offer" in load_state(config).seen
    assert _pass(config) == []


def test_a_changed_finding_is_reported_again(tmp_path: Path) -> None:
    config = _config(tmp_path, [_f("agent_model_policy", "agent.offer")])
    _pass(config)
    changed = {**_f("agent_model_policy", "agent.offer"), "message": "now reaches the ERP too"}
    _lints(tmp_path, [changed])
    assert [c.message for c in _pass(config)] == ["now reaches the ERP too"]


def test_a_finding_that_goes_and_comes_back_is_new(tmp_path: Path) -> None:
    config = _config(tmp_path, [_f("agent_model_policy", "agent.offer")])
    _pass(config)
    _lints(tmp_path, [])
    assert _pass(config) == []
    _lints(tmp_path, [_f("agent_model_policy", "agent.offer")])
    assert len(_pass(config)) == 1


def test_an_unrendered_map_or_unreadable_litellm_clears_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path, [_f("agent_model_policy", "agent.offer")])
    km.write_file(config, "governance", b"models:\n  gpt: {tier: local}\n", expected_revision="")
    live = gov.LiveModels((ModelEntry("gpt", "openai", "api.example.com"),))
    assert {c.key for c in _pass(config, live)} == {
        "render:agent_model_policy:agent.offer", "manager:model_tier_contradiction:gpt"}
    # LiteLLM down: the manager scope says nothing, so its finding is not forgotten
    down = detect.knowledge_map_findings(config, gov.LiveModels(None, "down"), now=NOW)
    assert [f.scope for f in down] == ["render:"]
    assert _pass(config, gov.LiveModels(None, "down")) == []
    assert _pass(config, live) == []  # back again: not reported twice
    # the map is not rendered (no manifests directory): nothing is concluded
    (tmp_path / "knowledge-map" / "manifests").rmdir()
    assert detect.knowledge_map_findings(config, None, now=NOW) == []


def test_a_manager_finding_under_a_valid_exception_is_not_reported(tmp_path: Path) -> None:
    config = _config(tmp_path, [])
    km.write_file(config, "governance", yaml.safe_dump({
        "models": {"gpt": {"tier": "local"}},
        "exceptions": [{"finding": "model_tier_contradiction", "subject": "gpt",
                        "expires": "2099-01-01"}],
    }).encode(), expected_revision="")
    live = gov.LiveModels((ModelEntry("gpt", "openai", "api.example.com"),))
    assert _pass(config, live) == []


def test_switching_the_map_off_forgets_its_findings(tmp_path: Path) -> None:
    config = _config(tmp_path, [_f("agent_model_policy", "agent.offer")])
    _pass(config)
    (tmp_path / ".env").write_text("COMPOSE_PROFILES=librechat\n")
    assert _pass(config) == []
    assert not [k for k in load_state(config).seen if k.startswith("knowledge_map_finding")]
