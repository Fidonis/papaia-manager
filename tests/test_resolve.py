"""Unit tests for cross-catalog add-on resolution (name+version grouping)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from app.core.catalogs import Catalog, CatalogRegistry
from app.core.resolve import resolve_catalog_addons
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
