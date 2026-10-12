"""Unit tests for cross-catalog add-on resolution (name+version grouping)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from app.core.catalogs import Catalog, CatalogRegistry
from app.core.resolve import (
    AddonUpdate,
    outdated_addons,
    resolve_catalog_addons,
    update_available,
)
from app.core.snapshots import InstalledAddon


def _make_addon(root: Path, name: str, version: str | None = "1.0.0") -> None:
    addon_dir = root / name
    addon_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {"description": f"{name} test addon"}
    if version is not None:
        manifest["version"] = version
    (addon_dir / "papaia-app.yaml").write_text(yaml.dump(manifest), encoding="utf-8")


def _local_catalog(name: str, path: Path, enabled: bool = True) -> Catalog:
    return Catalog(name=name, type="local", enabled=enabled, path=str(path))


def _installed(catalog: str, version: str) -> InstalledAddon:
    return InstalledAddon(
        catalog=catalog,
        commit="local",
        manifest_version=version,
        installed_at=datetime.now(UTC),
    )


def test_same_version_collapses_with_shadowed_by(tmp_path: Path) -> None:
    cat_a = tmp_path / "cat-a"
    cat_b = tmp_path / "cat-b"
    _make_addon(cat_a, "paperless", "1.0.0")
    _make_addon(cat_b, "paperless", "1.0.0")
    registry = CatalogRegistry(
        catalogs=[_local_catalog("cat-a", cat_a), _local_catalog("cat-b", cat_b)]
    )

    results = resolve_catalog_addons(registry, str(tmp_path), {})

    assert len(results) == 1
    entry = results[0]
    assert entry.key == "paperless"
    assert entry.catalog == "cat-a"
    assert entry.shadowed_by == ["cat-b"]
    assert entry.is_variant is False


def test_different_versions_kept_separate(tmp_path: Path) -> None:
    cat_a = tmp_path / "cat-a"
    cat_b = tmp_path / "cat-b"
    _make_addon(cat_a, "paperless", "1.0.0")
    _make_addon(cat_b, "paperless", "1.1.0")
    registry = CatalogRegistry(
        catalogs=[_local_catalog("cat-a", cat_a), _local_catalog("cat-b", cat_b)]
    )

    results = resolve_catalog_addons(registry, str(tmp_path), {})

    assert {r.key for r in results} == {"paperless", "paperless@cat-b"}
    primary = next(r for r in results if r.key == "paperless")
    variant = next(r for r in results if r.key == "paperless@cat-b")
    assert primary.catalog == "cat-a"
    assert primary.is_variant is False
    assert primary.shadowed_by == []
    assert variant.catalog == "cat-b"
    assert variant.is_variant is True
    assert variant.manifest["version"] == "1.1.0"


def test_installed_catalog_is_promoted_to_primary(tmp_path: Path) -> None:
    cat_a = tmp_path / "cat-a"
    cat_b = tmp_path / "cat-b"
    _make_addon(cat_a, "paperless", "1.0.0")
    _make_addon(cat_b, "paperless", "1.1.0")
    registry = CatalogRegistry(
        catalogs=[_local_catalog("cat-a", cat_a), _local_catalog("cat-b", cat_b)]
    )
    installed = {"paperless": _installed("cat-b", "1.1.0")}

    results = resolve_catalog_addons(registry, str(tmp_path), installed)

    primary = next(r for r in results if not r.is_variant)
    variant = next(r for r in results if r.is_variant)
    assert primary.catalog == "cat-b"
    assert primary.key == "paperless"
    assert variant.catalog == "cat-a"
    assert variant.key == "paperless@cat-a"


def test_disabled_catalog_is_ignored(tmp_path: Path) -> None:
    cat_a = tmp_path / "cat-a"
    cat_b = tmp_path / "cat-b"
    _make_addon(cat_a, "paperless", "1.0.0")
    _make_addon(cat_b, "paperless", "9.9.9")
    registry = CatalogRegistry(
        catalogs=[_local_catalog("cat-a", cat_a), _local_catalog("cat-b", cat_b, enabled=False)]
    )

    results = resolve_catalog_addons(registry, str(tmp_path), {})

    assert len(results) == 1
    assert results[0].catalog == "cat-a"
    assert results[0].shadowed_by == []


def test_missing_version_falls_back_to_unknown(tmp_path: Path) -> None:
    cat_a = tmp_path / "cat-a"
    cat_b = tmp_path / "cat-b"
    _make_addon(cat_a, "paperless", version=None)
    _make_addon(cat_b, "paperless", version=None)
    registry = CatalogRegistry(
        catalogs=[_local_catalog("cat-a", cat_a), _local_catalog("cat-b", cat_b)]
    )

    results = resolve_catalog_addons(registry, str(tmp_path), {})

    assert len(results) == 1
    assert results[0].shadowed_by == ["cat-b"]


def test_independent_addon_names_are_unaffected(tmp_path: Path) -> None:
    cat_a = tmp_path / "cat-a"
    cat_b = tmp_path / "cat-b"
    _make_addon(cat_a, "paperless", "1.0.0")
    _make_addon(cat_b, "paperless-connect", "1.0.0")
    registry = CatalogRegistry(
        catalogs=[_local_catalog("cat-a", cat_a), _local_catalog("cat-b", cat_b)]
    )

    results = resolve_catalog_addons(registry, str(tmp_path), {})

    assert {r.name for r in results} == {"paperless", "paperless-connect"}
    assert all(not r.is_variant for r in results)


# ---------------------------------------------------------------------------
# The update marker, shared with the notification about it
# ---------------------------------------------------------------------------


def test_the_catalog_offering_another_version_is_an_update() -> None:
    assert update_available(_installed("cat-a", "1.0.0"), "1.1.0") is True
    assert update_available(_installed("cat-a", "1.1.0"), "1.1.0") is False


def test_nothing_installed_or_nothing_stated_is_no_update() -> None:
    assert update_available(None, "1.1.0") is False
    assert update_available(_installed("cat-a", "1.0.0"), None) is False


def test_an_add_on_the_manager_does_not_manage_is_never_an_update() -> None:
    installed = _installed("cat-a", "1.0.0")
    unmanaged = InstalledAddon(
        catalog=installed.catalog,
        commit=installed.commit,
        manifest_version=installed.manifest_version,
        installed_at=installed.installed_at,
        managed=False,
    )
    assert update_available(unmanaged, "2.0.0") is False


def test_the_outdated_add_ons_are_the_installed_ones_a_catalog_has_moved_past(
    tmp_path: Path,
) -> None:
    cat = tmp_path / "cat"
    _make_addon(cat, "paperless", "1.1.0")
    _make_addon(cat, "n8n", "2.0.0")
    _make_addon(cat, "qdrant", "3.0.0")  # in the catalog, not installed: not an update
    registry = CatalogRegistry(catalogs=[_local_catalog("cat", cat)])
    installed = {"paperless": _installed("cat", "1.0.0"), "n8n": _installed("cat", "2.0.0")}

    assert outdated_addons(registry, str(tmp_path), installed) == [
        AddonUpdate(name="paperless", installed="1.0.0", available="1.1.0", catalog="cat")
    ]


def test_a_second_catalog_with_another_version_does_not_make_the_installed_one_outdated(
    tmp_path: Path,
) -> None:
    """The add-on came from `cat-a` and is current there; `cat-b` carries its own, newer copy
    (a variant). That is a choice the add-ons page offers, not an update of the installed one."""
    cat_a, cat_b = tmp_path / "cat-a", tmp_path / "cat-b"
    _make_addon(cat_a, "paperless", "1.0.0")
    _make_addon(cat_b, "paperless", "9.9.9")
    registry = CatalogRegistry(
        catalogs=[_local_catalog("cat-a", cat_a), _local_catalog("cat-b", cat_b)]
    )
    installed = {"paperless": _installed("cat-a", "1.0.0")}
    assert outdated_addons(registry, str(tmp_path), installed) == []


def test_an_installed_add_on_that_no_catalog_has_is_not_listed(tmp_path: Path) -> None:
    registry = CatalogRegistry(catalogs=[_local_catalog("cat", tmp_path / "empty")])
    assert outdated_addons(registry, str(tmp_path), {"gone": _installed("cat", "1.0.0")}) == []
