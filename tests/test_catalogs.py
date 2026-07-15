"""Unit tests for catalog validators and registry round-trip."""
from __future__ import annotations

import pytest

from app.core.catalogs import (
    Catalog,
    CatalogRegistry,
    load_registry,
    save_registry,
    validate_catalog_name,
    validate_catalog_url,
    validate_local_path,
    validate_ref,
)


# ---------------------------------------------------------------------------
# validate_catalog_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["fidonis", "my-catalog", "catalog1", "a", "a1-b2-c3"],
)
def test_valid_catalog_names(name: str) -> None:
    validate_catalog_name(name)  # must not raise


@pytest.mark.parametrize(
    "name",
    [
        "",
        "-starts-with-dash",
        "UPPER",
        "has space",
        "a" * 33,
        "has.dot",
        "has_underscore",
    ],
)
def test_invalid_catalog_names(name: str) -> None:
    with pytest.raises(ValueError):
        validate_catalog_name(name)


# ---------------------------------------------------------------------------
# validate_catalog_url
# ---------------------------------------------------------------------------


def test_valid_catalog_url() -> None:
    validate_catalog_url("https://github.com/Fidonis/papaia-addons.git")


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/example/repo.git",
        "git@github.com:example/repo.git",
        "ftp://example.com/repo",
        "/local/path",
    ],
)
def test_invalid_catalog_urls(url: str) -> None:
    with pytest.raises(ValueError):
        validate_catalog_url(url)


# ---------------------------------------------------------------------------
# validate_ref
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    ["main", "v1.0.0", "feature/my-branch", "abc123", "HEAD"],
)
def test_valid_refs(ref: str) -> None:
    validate_ref(ref)


@pytest.mark.parametrize(
    "ref",
    ["", "ref with space", "ref\x00null", "a" * 101],
)
def test_invalid_refs(ref: str) -> None:
    with pytest.raises(ValueError):
        validate_ref(ref)


# ---------------------------------------------------------------------------
# validate_local_path
# ---------------------------------------------------------------------------


def test_local_path_inside_workspace(tmp_path: pytest.TempPathFactory) -> None:
    workspace = str(tmp_path)
    inside = str(tmp_path / "my-addon")
    validate_local_path(inside, workspace)  # must not raise


def test_local_path_outside_workspace(tmp_path: pytest.TempPathFactory) -> None:
    workspace = str(tmp_path / "workspace")
    outside = str(tmp_path / "other")
    with pytest.raises(ValueError, match="PAPAIA_WORKSPACE_DIR"):
        validate_local_path(outside, workspace)


def test_local_path_traversal_blocked(tmp_path: pytest.TempPathFactory) -> None:
    workspace = str(tmp_path / "workspace")
    traversal = str(tmp_path / "workspace" / ".." / "secret")
    with pytest.raises(ValueError):
        validate_local_path(traversal, workspace)


# ---------------------------------------------------------------------------
# save_registry / load_registry round-trip
# ---------------------------------------------------------------------------


def test_registry_roundtrip(tmp_path: pytest.TempPathFactory) -> None:
    config_dir = str(tmp_path)
    registry = CatalogRegistry(
        catalogs=[
            Catalog(
                name="mycat",
                type="git",
                url="https://github.com/example/addons.git",
                ref="v2",
                enabled=True,
            )
        ]
    )
    save_registry(config_dir, registry)
    loaded = load_registry(config_dir)
    assert len(loaded.catalogs) == 1
    cat = loaded.catalogs[0]
    assert cat.name == "mycat"
    assert cat.url == "https://github.com/example/addons.git"
    assert cat.ref == "v2"


def test_registry_seeds_default_on_first_run(tmp_path: pytest.TempPathFactory) -> None:
    config_dir = str(tmp_path)
    registry = load_registry(config_dir)
    assert any(c.name == "fidonis" for c in registry.catalogs)


def test_registry_idempotent_seed(tmp_path: pytest.TempPathFactory) -> None:
    config_dir = str(tmp_path)
    r1 = load_registry(config_dir)
    r2 = load_registry(config_dir)
    assert len(r1.catalogs) == len(r2.catalogs)


def test_registry_atomic_write_leaves_no_tmp(tmp_path: pytest.TempPathFactory) -> None:
    config_dir = str(tmp_path)
    registry = CatalogRegistry(catalogs=[])
    save_registry(config_dir, registry)
    manager_dir = tmp_path / "manager"
    tmp_files = list(manager_dir.glob("*.tmp"))
    assert tmp_files == []
