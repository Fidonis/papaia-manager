"""Unit tests for deployment.yaml addon-entry indexing."""
from __future__ import annotations

from app.core.state import deployment_addons_by_name


def test_empty_addons_list() -> None:
    assert deployment_addons_by_name({"addons": []}) == {}


def test_missing_addons_key() -> None:
    assert deployment_addons_by_name({}) == {}


def test_indexes_entries_by_name() -> None:
    entries = [
        {"name": "docrag", "path": "addons/_managed/docrag", "active": True},
        {"name": "n8n", "path": "addons/_managed/n8n", "active": False},
    ]
    result = deployment_addons_by_name({"addons": entries})
    assert result == {"docrag": entries[0], "n8n": entries[1]}


def test_skips_entries_without_name() -> None:
    entries = [{"path": "addons/_managed/broken", "active": True}]
    assert deployment_addons_by_name({"addons": entries}) == {}
