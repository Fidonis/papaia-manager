"""Which Docker images are outdated, and how they are removed.

The verdict (`build_report`) is a pure function over three readings, so most of
what matters is asserted there with fixtures shaped like a real host. The
Docker-facing halves are covered by replacing `images._run`: nothing here talks
to a daemon.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.core import images
from app.core.images import (
    ImageReport,
    ImagesError,
    LocalImage,
    OutdatedImage,
    build_report,
    format_size,
    parse_ref,
)

_A = "sha256:" + "a" * 64
_B = "sha256:" + "b" * 64
_C = "sha256:" + "c" * 64
_D = "sha256:" + "d" * 64


def _image(
    image_id: str, *tags: str, digests: tuple[str, ...] = (), size: int = 1_000_000
) -> LocalImage:
    return LocalImage(
        id=image_id, tags=tags, digests=digests, size=size, created="2026-01-02T03:04:05Z"
    )


def _report(declared: dict[str, set[str]], local: list[LocalImage], used: set[str] | None = None):
    return build_report(
        {source: frozenset(refs) for source, refs in declared.items()}, local, used or set()
    )


def _names(report: ImageReport) -> list[str]:
    return [image.name for image in report.outdated]


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("postgres:18", ("postgres", "18", "")),
        # Compose spells an official image out; the daemon abbreviates it.
        ("docker.io/library/postgres:18", ("postgres", "18", "")),
        ("docker.io/valkey/valkey:9-alpine", ("valkey/valkey", "9-alpine", "")),
        ("index.docker.io/library/redis", ("redis", "latest", "")),
        ("ghcr.io/berriai/litellm:v1.100.0", ("ghcr.io/berriai/litellm", "v1.100.0", "")),
        # A colon before the last slash is a registry port, not a tag.
        ("localhost:5000/app", ("localhost:5000/app", "latest", "")),
        ("localhost:5000/app:2", ("localhost:5000/app", "2", "")),
        # A pin by digest names no tag -- `latest` must not be invented for it.
        ("ghcr.io/x/y@sha256:abc", ("ghcr.io/x/y", "", "sha256:abc")),
        ("ghcr.io/x/y:1.0@sha256:abc", ("ghcr.io/x/y", "1.0", "sha256:abc")),
    ],
)
def test_references_are_normalised_the_way_the_daemon_spells_them(
    ref: str, expected: tuple[str, str, str]
) -> None:
    assert parse_ref(ref) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.55GB", 1_550_000_000),
        ("361MB", 361_000_000),
        ("13.4kB", 13_400),
        ("1.55 GB", 1_550_000_000),
        ("0B", 0),
        ("N/A", 0),
        ("", 0),
    ],
)
def test_the_listed_size_is_read_as_bytes(text: str, expected: int) -> None:
    assert images.parse_size(text) == expected


def test_the_listed_size_wins_over_the_inspected_one() -> None:
    # The containerd store reports the compressed content size in `inspect`.
    entry = {"Id": _A, "RepoTags": ["postgres:15"], "Size": 361_000_000}
    assert images._local_image(entry, 1_550_000_000).size == 1_550_000_000
    assert images._local_image(entry).size == 361_000_000


async def test_local_images_take_their_size_from_the_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(args: list[str], *, limit: float) -> str:
        if args[:3] == ["docker", "image", "ls"]:
            # One row per tag: the id repeats.
            return (
                f'{{"ID":"{_A}","Repository":"postgres","Tag":"15","Size":"1.55GB"}}\n'
                f'{{"ID":"{_A}","Repository":"postgres","Tag":"15.1","Size":"1.55GB"}}\n'
            )
        assert args[:3] == ["docker", "image", "inspect"]
        assert args.count(_A) == 1
        return f'{{"Id":"{_A}","RepoTags":["postgres:15","postgres:15.1"],"Size":361000000}}\n'

    monkeypatch.setattr(images, "_run", run)

    (image,) = await images.local_images()

    assert image.size == 1_550_000_000
    assert image.tags == ("postgres:15", "postgres:15.1")


def test_sizes_read_like_dockers() -> None:
    assert format_size(0) == "0 B"
    assert format_size(999) == "999 B"
    assert format_size(1_500_000) == "1.5 MB"
    assert format_size(3_440_000_000) == "3.4 GB"


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def test_a_superseded_version_is_outdated_and_the_current_one_is_not() -> None:
    report = _report(
        {"core": {"ghcr.io/berriai/litellm:v1.100.0"}},
        [
            _image(_A, "ghcr.io/berriai/litellm:v1.100.0"),
            _image(_B, "ghcr.io/berriai/litellm:v1.91.1"),
        ],
    )
    assert _names(report) == ["ghcr.io/berriai/litellm:v1.91.1"]
    assert report.outdated[0].id == _B
    assert report.outdated[0].source == "core"


def test_different_services_may_pin_different_versions_of_one_repository() -> None:
    # `postgres:16` (keycloak), `postgres:18.3` (litellm) and the paperless
    # add-on's `docker.io/library/postgres:18` are all current. Judging by
    # repository alone would delete two of them.
    report = _report(
        {"core": {"postgres:16", "postgres:18.3"}, "paperless": {"docker.io/library/postgres:18"}},
        [_image(_A, "postgres:16"), _image(_B, "postgres:18.3"), _image(_C, "postgres:18")],
    )
    assert report.outdated == []


def test_an_image_in_use_by_any_container_is_never_offered() -> None:
    # Stopped containers count: the finished upgrade runner holds the previous
    # papaia-manager image, and `docker image rm` would refuse it anyway.
    report = _report(
        {"core": {"ghcr.io/fidonis/papaia-manager:1.1.0"}},
        [_image(_A, "ghcr.io/fidonis/papaia-manager:1.0.0")],
        used={_A},
    )
    assert report.outdated == []


def test_an_image_of_an_unrelated_repository_is_left_alone() -> None:
    # This is what keeps the feature from being `docker image prune -a`.
    report = _report(
        {"core": {"postgres:18.3"}},
        [_image(_A, "alpine:3"), _image(_B, "python:3.12-slim"), _image(_C, "acme/app:latest")],
    )
    assert report.outdated == []


def test_an_image_that_also_carries_a_foreign_tag_is_left_alone() -> None:
    report = _report(
        {"core": {"postgres:18.3"}},
        [_image(_A, "postgres:15", "mycorp/postgres:15")],
    )
    assert report.outdated == []


def test_the_gpu_override_is_what_makes_the_cpu_variant_outdated() -> None:
    # Declared comes from `compose config` *with* the overrides, which is what
    # swaps the tag. With the override active the plain CPU image is unused.
    declared = {"core": {"localai/localai:v4.7.1-gpu-nvidia-cuda-13"}}
    local = [
        _image(_A, "localai/localai:v4.7.1-gpu-nvidia-cuda-13"),
        _image(_B, "localai/localai:v4.7.1"),
        _image(_C, "localai/localai:v4.6.2"),
    ]
    assert _names(_report(declared, local)) == ["localai/localai:v4.6.2", "localai/localai:v4.7.1"]


def test_a_digest_pinned_image_matches_by_digest() -> None:
    pin = "ghcr.io/firecrawl/playwright-service@sha256:6359"
    report = _report(
        {"core": {pin}},
        [
            _image(_A, digests=(pin,)),
            _image(_B, digests=("ghcr.io/firecrawl/playwright-service@sha256:0000",)),
        ],
    )
    assert [i.id for i in report.outdated] == [_B]
    # No tag to remove by, so the id is what `docker image rm` gets -- and the
    # name shows the repository with a short digest.
    assert report.outdated[0].remove == (_B,)
    assert report.outdated[0].name == "ghcr.io/firecrawl/playwright-service@sha256:0000"


def test_an_image_without_any_reference_is_not_the_stacks_to_remove() -> None:
    assert _report({"core": {"postgres:18.3"}}, [_image(_A)]).outdated == []


def test_the_containerd_store_lists_digest_pins_among_the_tags() -> None:
    entry = {
        "Id": _A,
        "RepoTags": ["ghcr.io/x/y@sha256:abc"],
        "RepoDigests": ["ghcr.io/x/y@sha256:abc"],
        "Size": 5,
        "Created": "2026-01-01T00:00:00Z",
    }
    image = images._local_image(entry)
    assert image.tags == ()
    assert image.digests == ("ghcr.io/x/y@sha256:abc",)
    # A `<none>` entry is not a reference at all.
    assert images._local_image({"Id": _B, "RepoTags": ["<none>:<none>"]}).tags == ()


def test_an_image_is_attributed_to_the_source_that_declares_its_repository() -> None:
    report = _report(
        {"core": {"postgres:16"}, "paperless": {"gotenberg/gotenberg:8.34"}},
        [_image(_A, "gotenberg/gotenberg:8.27"), _image(_B, "postgres:15")],
    )
    assert {i.name: i.source for i in report.outdated} == {
        "gotenberg/gotenberg:8.27": "paperless",
        "postgres:15": "core",
    }
    # Core first, then the add-ons.
    assert [i.source for i in report.outdated] == ["core", "paperless"]


def test_an_image_with_several_tags_is_removed_by_each_of_them() -> None:
    report = _report({"core": {"redis:8.8.0-alpine"}}, [_image(_A, "redis:8", "redis:8.6")])
    assert report.outdated[0].remove == ("redis:8", "redis:8.6")


def test_one_declared_tag_keeps_an_image_that_has_other_tags_too() -> None:
    report = _report({"core": {"postgres:18.3"}}, [_image(_A, "postgres:18", "postgres:18.3")])
    assert report.outdated == []


def test_unreadable_declarations_fail_closed() -> None:
    report = build_report(
        {"core": frozenset({"postgres:18.3"})},
        [_image(_A, "postgres:15")],
        set(),
        ["paperless: docker compose config failed: no such file"],
    )
    assert report.outdated == []
    assert report.errors == ["paperless: docker compose config failed: no such file"]


def test_the_total_size_adds_up() -> None:
    report = _report(
        {"core": {"postgres:18.3"}},
        [_image(_A, "postgres:15", size=600_000_000), _image(_B, "postgres:14", size=400_000_000)],
    )
    assert report.total_size == 1_000_000_000
    assert report.total_size_human == "1.0 GB"


# ---------------------------------------------------------------------------
# Declared images -- the compose invocations
# ---------------------------------------------------------------------------


class _FakeDocker:
    """Records `_run` calls and answers `compose config` from a table."""

    def __init__(self, answers: dict[str, str | ImagesError]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    async def __call__(self, args: list[str], *, limit: float) -> str:
        self.calls.append(args)
        # The project name marks an add-on call; anything else is the core.
        key = args[args.index("-p") + 1] if "-p" in args else "core"
        answer = self.answers[key]
        if isinstance(answer, ImagesError):
            raise answer
        return answer


def _workspace(tmp_path: Path, addons: dict[str, bool] | None = None) -> tuple[str, str]:
    workspace, config = tmp_path / "ws", tmp_path / "config"
    src = workspace / "papaia" / "src"
    src.mkdir(parents=True)
    (src / "docker-compose.yml").write_text("services: {}\n")
    (src / ".env").write_text("COMPOSE_PROFILES=litellm\n")
    (config / "overrides").mkdir(parents=True)
    (config / "overrides" / "docker-compose.localai-gpu.override.yml").write_text("{}\n")
    (config / "overrides" / "docker-compose.n8n.override.yml").write_text("{}\n")
    entries = []
    for name, active in (addons or {}).items():
        path = workspace / "addons" / name
        path.mkdir(parents=True)
        (path / "docker-compose.yml").write_text("services: {}\n")
        (path / ".env").write_text("X=1\n")
        entries.append({"name": name, "path": str(path), "active": active})
    (config / "deployment.yaml").write_text(yaml.safe_dump({"addons": entries}))
    return str(config), str(workspace)


async def test_the_core_is_resolved_with_every_override_and_the_rendered_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, workspace = _workspace(tmp_path)
    fake = _FakeDocker({"core": "postgres:18.3\nlocalai/localai:v4.7.1-gpu\n"})
    monkeypatch.setattr(images, "_run", fake)

    declared, errors = await images.declared_images(config, workspace)

    assert errors == []
    assert declared == {"core": frozenset({"postgres:18.3", "localai/localai:v4.7.1-gpu"})}
    (call,) = fake.calls
    assert call[:2] == ["docker", "compose"]
    files = [call[i + 1] for i, a in enumerate(call) if a == "-f"]
    assert files[0].endswith("docker-compose.yml")
    assert [Path(f).name for f in files[1:]] == [
        "docker-compose.localai-gpu.override.yml",
        "docker-compose.n8n.override.yml",
    ]
    assert call[call.index("--env-file") + 1].endswith(str(Path("src") / ".env"))
    assert call[-2:] == ["config", "--images"]


async def test_an_inactive_addon_keeps_its_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `addon start` would have to pull them again otherwise.
    config, workspace = _workspace(tmp_path, {"paperless": True, "n8n": False})
    fake = _FakeDocker(
        {
            "core": "postgres:18.3\n",
            "paperless": "gotenberg/gotenberg:8.34\n",
            "n8n": "n8nio/n8n:2\n",
        }
    )
    monkeypatch.setattr(images, "_run", fake)

    declared, errors = await images.declared_images(config, workspace)

    assert errors == []
    assert list(declared) == ["core", "n8n", "paperless"]
    n8n = next(c for c in fake.calls if "-p" in c and c[c.index("-p") + 1] == "n8n")
    # Root env first, the add-on's own second: later files win.
    env_files = [n8n[i + 1] for i, a in enumerate(n8n) if a == "--env-file"]
    assert len(env_files) == 2
    assert env_files[0].endswith(str(Path("src") / ".env"))
    assert env_files[1].endswith(str(Path("addons") / "n8n" / ".env"))


async def test_one_failing_source_is_reported_and_the_others_still_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, workspace = _workspace(tmp_path, {"paperless": True})
    fake = _FakeDocker(
        {"core": "postgres:18.3\n", "paperless": ImagesError("invalid interpolation")}
    )
    monkeypatch.setattr(images, "_run", fake)

    declared, errors = await images.declared_images(config, workspace)

    assert "core" in declared
    assert errors == ["paperless: docker compose config failed: invalid interpolation"]


async def test_a_missing_addon_directory_is_an_error_not_a_silent_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, workspace = _workspace(tmp_path, {"paperless": True})
    (Path(workspace) / "addons" / "paperless" / "docker-compose.yml").unlink()
    monkeypatch.setattr(images, "_run", _FakeDocker({"core": "postgres:18.3\n"}))

    _, errors = await images.declared_images(config, workspace)

    assert len(errors) == 1
    assert "paperless" in errors[0]
    assert "does not exist" in errors[0]


async def test_a_workspace_without_a_checkout_declares_nothing(tmp_path: Path) -> None:
    declared, errors = await images.declared_images(str(tmp_path / "c"), str(tmp_path / "w"))
    assert declared == {}
    assert errors == ["the papaia checkout has no src/docker-compose.yml"]


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


def _outdated(image_id: str, name: str, *remove: str, size: int = 10) -> OutdatedImage:
    return OutdatedImage(
        id=image_id, name=name, remove=remove or (name,), size=size, created="", source="core"
    )


@pytest.fixture
def fake_report(monkeypatch: pytest.MonkeyPatch) -> list[OutdatedImage]:
    candidates = [_outdated(_A, "postgres:15"), _outdated(_B, "redis:8", "redis:8", "redis:8.6")]

    async def report(config: str, workspace: str) -> ImageReport:
        return ImageReport(outdated=list(candidates))

    monkeypatch.setattr(images, "gather_report", report)
    return candidates


async def test_prune_removes_every_candidate_without_force(
    fake_report: list[OutdatedImage], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    async def run(args: list[str], *, limit: float) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(images, "_run", run)

    result = await images.prune("c", "w")

    assert [i.name for i in result.removed] == ["postgres:15", "redis:8"]
    assert calls == [
        ["docker", "image", "rm", "postgres:15"],
        ["docker", "image", "rm", "redis:8"],
        ["docker", "image", "rm", "redis:8.6"],
    ]
    # Never forced: the daemon's own refusal is the last line of defence.
    assert all("-f" not in c and "--force" not in c for c in calls)
    assert result.reclaimed == 20


async def test_a_refused_image_is_reported_and_the_rest_still_go(
    fake_report: list[OutdatedImage], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run(args: list[str], *, limit: float) -> str:
        if args[-1] == "postgres:15":
            raise ImagesError("image is being used by stopped container abc")
        return ""

    monkeypatch.setattr(images, "_run", run)

    result = await images.prune("c", "w")

    assert [i.name for i in result.removed] == ["redis:8"]
    assert [(i.name, reason) for i, reason in result.failed] == [
        ("postgres:15", "image is being used by stopped container abc")
    ]


async def test_named_ids_only_narrow_the_candidates(
    fake_report: list[OutdatedImage], monkeypatch: pytest.MonkeyPatch
) -> None:
    # An id that is not outdated right now -- a stale page, or a forged request
    # naming an image that is in use -- must never reach `docker image rm`.
    calls: list[list[str]] = []

    async def run(args: list[str], *, limit: float) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(images, "_run", run)

    result = await images.prune("c", "w", only={_A, _D})

    assert [i.id for i in result.removed] == [_A]
    assert result.skipped == [_D]
    assert calls == [["docker", "image", "rm", "postgres:15"]]


async def test_prune_refuses_when_the_declared_images_are_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def report(config: str, workspace: str) -> ImageReport:
        return ImageReport(errors=["core: docker compose config failed: boom"])

    async def run(args: list[str], *, limit: float) -> str:  # pragma: no cover
        raise AssertionError("nothing may be removed")

    monkeypatch.setattr(images, "gather_report", report)
    monkeypatch.setattr(images, "_run", run)

    with pytest.raises(ImagesError, match="boom"):
        await images.prune("c", "w")


async def test_an_unreachable_daemon_is_a_report_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, workspace = _workspace(tmp_path)

    async def run(args: list[str], *, limit: float) -> str:
        raise ImagesError("cannot invoke docker: no such file")

    monkeypatch.setattr(images, "_run", run)

    report = await images.gather_report(config, workspace)

    assert report.outdated == []
    assert report.errors


# ---------------------------------------------------------------------------
# The upgrade runner's entry point
# ---------------------------------------------------------------------------


async def test_the_runner_reports_what_it_removed(
    fake_report: list[OutdatedImage],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def run(args: list[str], *, limit: float) -> str:
        if args[-1] == "redis:8.6":
            raise ImagesError("conflict: in use")
        return ""

    monkeypatch.setattr(images, "_run", run)

    await images._run_prune("c", "w")

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "[papaia-manager] Removing outdated Docker images..."
    assert "[papaia-manager]   removed postgres:15 (10 B)" in lines
    assert "[papaia-manager]   kept redis:8: conflict: in use" in lines
    assert lines[-1] == (
        "[papaia-manager] Image cleanup finished: 1 removed, up to 10 B reclaimed, 1 kept"
    )


async def test_a_cleanup_that_cannot_run_says_so_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def report(config: str, workspace: str) -> ImageReport:
        return ImageReport(errors=["core: docker compose config failed: boom"])

    monkeypatch.setattr(images, "gather_report", report)

    await images._run_prune("c", "w")  # would raise if the failure escaped

    last = capsys.readouterr().out.splitlines()[-1]
    assert last.startswith("[papaia-manager] Image cleanup skipped -- nothing was removed:")
    assert "boom" in last
