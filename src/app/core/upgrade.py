"""Read-only view of what a core upgrade would do, and where this one stands.

`papaia-ctl upgrade` owns the operation itself; this module only ever *asks*.
The split runs along the same line the core draws internally: bash owns the git
calls and the lifecycle, the Python side owns version arithmetic and the
migration ledger. Here, git is a subprocess and the arithmetic is delegated
straight back to the core's own `lib.cli` sub-commands, so the manager and a
shell on the host cannot reach different verdicts about the same checkout.

Two tiers, because they cost wildly different amounts:

* `current_version` / `checkout_state` / `read_upgrade_log` are offline and
  cheap -- file reads plus three `git` calls against a local checkout. Safe to
  render on page load.
* `run_check` fetches from the remote and materialises a worktree of the target
  tag, because the add-on gate has no honest answer without one: only the target
  tree carries its own ADDON_API window and its Compose service names. It is an
  explicit operator action, serialised, and its result is cached.

Nothing here imports `lib.*`. `core/backups.py` records the reason in full: the
test suite skips the lifespan that puts `papaia/tools` on `sys.path`, so an
in-process import would not be exercisable by any test. Shelling out to
`python3 -m lib.cli` keeps the core's arithmetic authoritative *and* testable,
at the cost of parsing four lines of TSV.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.state import load_deployment_yaml

logger = logging.getLogger(__name__)

# Local git calls are milliseconds; the fetch talks to the remote. Both are
# bounded because they sit in a request, and an unbounded `git fetch` against an
# unreachable remote would hold a worker until the client gives up.
_GIT_TIMEOUT = 15.0
_FETCH_TIMEOUT = 60.0

# Release tags, as `lib/upgrade.py` writes them. Only the stable form: a
# pre-release is never offered in the picker, mirroring `latest_release`, which
# skips them so that an upgrade without --version stays a production move.
_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+\Z")

# Same shape, without the `v`. The value reaches a `--version=` argv, so this is
# the guard against a version string that would be read as another flag.
#
# `\Z` rather than `$`: `$` also matches before a trailing newline, so `1.2.0\n`
# would pass and reach the argv with the newline still attached.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\Z")

# One line of $PAPAIA_CONFIG_DIR/upgrade.log, as `_upgrade_log` writes it:
#   2026-09-02T10:11:12Z upgrade from=1.0.0 to=1.2.0 result=ok migrations=2 ...
_LOG_RE = re.compile(
    r"^(?P<ts>\S+)\s+upgrade\s+from=(?P<from_version>\S+)\s+to=(?P<to_version>\S+)\s+"
    r"result=(?P<result>\S+)\s*(?P<rest>.*)$"
)

# How much of the dirty-tree listing is worth showing. A checkout with hundreds
# of modified files is one fact, not two hundred, and the operator has to go to
# a shell either way.
_MAX_DIRTY_LINES = 50

# Credential helper shipped in the runtime image. Resolved once at import: it is
# a filesystem probe, and repeating it inside an async call would block the loop
# for the sake of a path that cannot change while the process lives.
_ASKPASS = "/app/bin/git-askpass" if Path("/app/bin/git-askpass").is_file() else ""


class UpgradeError(Exception):
    """Raised when the checkout cannot be inspected."""


def repo_path(workspace_dir: str) -> Path:
    """The papaia checkout inside the mounted workspace."""
    return Path(workspace_dir) / "papaia"


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


async def _run_git(repo: Path, args: tuple[str, ...], limit: float) -> str:
    """Run a git command in `repo` and return stdout, raising on failure.

    Mirrors `runner._docker`: argv arrays, never a shell, stdin closed, and a
    bounded wait that kills the process rather than leaving it behind.
    `GIT_TERMINAL_PROMPT=0` matters more than it looks -- without it a remote
    that asks for credentials blocks until the timeout instead of failing, and
    the fetch is the one call an operator waits on.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if _ASKPASS:
        env.setdefault("GIT_ASKPASS", _ASKPASS)
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except (FileNotFoundError, OSError) as exc:
        raise UpgradeError(f"cannot invoke git: {exc}") from exc
    try:
        raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=limit)
    except TimeoutError as exc:
        proc.kill()
        raise UpgradeError(f"git {args[0]} timed out after {limit:.0f}s") from exc
    if proc.returncode != 0:
        detail = raw_err.decode(errors="replace").strip() or f"exit code {proc.returncode}"
        raise UpgradeError(f"git {args[0]} failed: {detail}")
    return raw_out.decode(errors="replace")


async def _git(repo: Path, *args: str) -> str:
    """A local git call. Everything except the fetch goes through here."""
    return await _run_git(repo, args, _GIT_TIMEOUT)


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionState:
    """What this installation is at, from both sources that claim to know.

    `recorded` is `platform_version` in deployment.yaml -- the version the config
    bundle was last migrated and rendered to, and the one `lib/upgrade.py`
    treats as authoritative for exactly that reason. `checkout` is the VERSION
    file in the tree.

    They disagree in one situation, and it is worth naming: an upgrade that died
    after `git checkout <tag>` but before the setup pass leaves the tree on the
    new release with a bundle still shaped like the old one. The CLI does not
    surface that; two file reads here do.
    """

    recorded: str = ""
    checkout: str = ""

    @property
    def current(self) -> str:
        """The version an upgrade would start from."""
        return self.recorded or self.checkout

    @property
    def mismatch(self) -> bool:
        return bool(self.recorded and self.checkout and self.recorded != self.checkout)


def current_version(config_dir: str, workspace_dir: str) -> VersionState:
    """Read both version sources. Missing files are empty strings, never errors."""
    recorded = ""
    try:
        raw = load_deployment_yaml(config_dir).get("platform_version")
        recorded = str(raw).strip() if raw else ""
    except (OSError, ValueError) as exc:
        logger.warning("cannot read platform_version: %s", exc)
    checkout = ""
    # A workspace without a VERSION file is a deployment too old to carry one;
    # the recorded version answers for it.
    with contextlib.suppress(OSError):
        checkout = (repo_path(workspace_dir) / "VERSION").read_text(encoding="utf-8").strip()
    return VersionState(recorded=recorded, checkout=checkout)


def is_valid_target_version(value: str) -> bool:
    """True if `value` is shaped like a release version this page may target."""
    return bool(_VERSION_RE.match(value))


def parse_tags(lines: list[str]) -> list[str]:
    """Stable release versions from `git tag --list`, newest first."""
    found = [line.strip()[1:] for line in lines if _TAG_RE.match(line.strip())]
    return sorted(set(found), key=_sort_key, reverse=True)


def _sort_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return (int(major), int(minor), int(patch))


def newer_than(versions: list[str], current: str) -> list[str]:
    """The subset strictly ahead of `current`, newest first.

    Only ever used to populate the picker. Which of them the upgrade may
    actually move to is still decided by the core's `upgrade-resolve`, which is
    handed the chosen value back as `--version=`; a comparison here that
    disagreed would be caught there rather than acted on.
    """
    if not _VERSION_RE.match(current):
        return list(versions)
    key = _sort_key(current)
    return [v for v in versions if _sort_key(v) > key]


# ---------------------------------------------------------------------------
# Checkout state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckoutState:
    """Whether the checkout is in a shape `papaia-ctl upgrade` would accept."""

    is_git: bool = False
    clean: bool = True
    dirty: str = ""
    head: str = ""
    tag: str = ""
    error: str = ""

    @property
    def upgradable(self) -> bool:
        return self.is_git and self.clean and not self.error


async def checkout_state(workspace_dir: str) -> CheckoutState:
    """Inspect the checkout with the same two commands `cmd_upgrade` runs.

    `--untracked-files=no` is load-bearing and matches the core exactly: every
    file papaia-ctl writes into the checkout (`src/**/.env`, the generated realm
    JSON) is gitignored, so a healthy installation is clean here. Counting
    untracked files would report every healthy installation as dirty and
    disable the button on all of them.
    """
    repo = repo_path(workspace_dir)
    try:
        await _git(repo, "rev-parse", "--git-dir")
    except UpgradeError as exc:
        # Not a checkout is a state, not a fault: it is what an installation
        # unpacked from a tarball looks like, and the page says so.
        logger.info("workspace is not a git checkout: %s", exc)
        return CheckoutState(is_git=False)

    try:
        porcelain = await _git(repo, "status", "--porcelain", "--untracked-files=no")
        head = (await _git(repo, "rev-parse", "--short", "HEAD")).strip()
    except UpgradeError as exc:
        return CheckoutState(is_git=True, error=str(exc))

    dirty = ""
    if porcelain.strip():
        lines = (await _git(repo, "status", "--short", "--untracked-files=no")).splitlines()
        dirty = "\n".join(lines[:_MAX_DIRTY_LINES])
        if len(lines) > _MAX_DIRTY_LINES:
            dirty += f"\n... and {len(lines) - _MAX_DIRTY_LINES} more"

    # Detached at a plain commit, or on a branch: `describe --exact-match` exits
    # non-zero and there is no tag to report. Neither blocks an upgrade; it only
    # means the failure path's "go back to v<from>" has no tag to name, which
    # the page mentions rather than treats as a problem.
    tag = ""
    with contextlib.suppress(UpgradeError):
        tag = (await _git(repo, "describe", "--tags", "--exact-match", "HEAD")).strip()

    return CheckoutState(
        is_git=True,
        clean=not porcelain.strip(),
        dirty=dirty,
        head=head,
        tag=tag,
    )


async def local_tags(workspace_dir: str) -> list[str]:
    """Release versions already in the checkout. No network."""
    out = await _git(repo_path(workspace_dir), "tag", "--list")
    return parse_tags(out.splitlines())


async def fetch_tags(workspace_dir: str) -> str:
    """Fetch release tags from the remote. Returns "" on success, else the reason.

    A failure is deliberately not raised. `cmd_upgrade` warns and carries on with
    the tags already in the checkout, and the page has to behave the same way or
    it stops working the moment the deployment has no route to the remote.
    """
    try:
        await _run_git(
            repo_path(workspace_dir),
            ("fetch", "--tags", "--quiet", "origin"),
            _FETCH_TIMEOUT,
        )
    except UpgradeError as exc:
        logger.info("could not fetch tags: %s", exc)
        return str(exc)
    return ""


# ---------------------------------------------------------------------------
# Parsers for the core's machine-readable output
# ---------------------------------------------------------------------------


def parse_resolve_tsv(text: str) -> dict[str, str]:
    r"""CURRENT / TARGET / TAG / STATUS from `lib.cli upgrade-resolve`.

    `\r` is stripped for the same reason the shell pipes this through
    `tr -d '\r'`: on Windows the interpreter emits CRLF, and a tag name ending
    in CR matches no ref.
    """
    out: dict[str, str] = {}
    for line in text.replace("\r", "").splitlines():
        key, _, value = line.partition("\t")
        if key in ("CURRENT", "TARGET", "TAG", "STATUS") and value:
            out[key] = value
    return out


@dataclass(frozen=True)
class Migration:
    """One pending release migration, as `upgrade-plan` reports it."""

    id: str
    version: str
    kind: str


def parse_plan_tsv(text: str) -> list[Migration]:
    """MIGRATION rows from `lib.cli upgrade-plan`, in execution order.

    The path field is read and dropped: it names a script inside the *target*
    worktree, which is removed as soon as the check finishes, so it would only
    ever be a dangling path on the page.
    """
    found: list[Migration] = []
    for line in text.replace("\r", "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 5 and parts[0] == "MIGRATION":
            found.append(Migration(id=parts[1], version=parts[2], kind=parts[4]))
    return found


@dataclass(frozen=True)
class GateResult:
    """One add-on's verdict against the target core."""

    name: str
    status: str
    axis: str = ""
    requirement: str = ""
    core_value: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Gate:
    """The add-on compatibility gate as a whole.

    `forceable` is the distinction that decides whether the page may offer
    `--force` at all: `compat.gate` returns 2 for an ERROR regardless of the
    flag, so offering it against a malformed manifest would promise something
    the CLI cannot deliver.
    """

    passed: bool = True
    results: tuple[GateResult, ...] = ()

    @property
    def blocking(self) -> tuple[GateResult, ...]:
        return tuple(r for r in self.results if r.status in ("INCOMPATIBLE", "ERROR"))

    @property
    def has_error(self) -> bool:
        return any(r.status == "ERROR" for r in self.results)

    @property
    def forceable(self) -> bool:
        return not self.passed and not self.has_error


def parse_gate_json(text: str, *, exit_code: int) -> Gate:
    """Build the gate from `addon-check --json`, which prints even on exit 2."""
    try:
        raw = json.loads(text) if text.strip() else []
    except json.JSONDecodeError as exc:
        raise UpgradeError(f"could not read the add-on compatibility report: {exc}") from exc
    if not isinstance(raw, list):
        raise UpgradeError("the add-on compatibility report is not a list")
    results = tuple(
        GateResult(
            name=str(entry.get("name") or "?"),
            status=str(entry.get("status") or "UNKNOWN"),
            axis=str(entry.get("axis") or ""),
            requirement=_fmt(entry.get("requirement")),
            core_value=_fmt(entry.get("core_value")),
            reason=str(entry.get("reason") or ""),
        )
        for entry in raw
        if isinstance(entry, dict)
    )
    return Gate(passed=exit_code == 0, results=results)


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


# ---------------------------------------------------------------------------
# upgrade.log
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogEntry:
    """One recorded upgrade attempt."""

    at: str
    from_version: str
    to_version: str
    result: str
    details: dict[str, str] = field(default_factory=dict)

    @property
    def stage(self) -> str:
        return self.details.get("stage", "")

    @property
    def restore_point(self) -> str:
        point = self.details.get("restore_point", "")
        return "" if point == "none" else point


def read_upgrade_log(config_dir: str, *, limit: int = 20) -> list[LogEntry]:
    """The last recorded upgrade attempts, newest first.

    Fails soft, like the backup catalogue: an installation that has never been
    upgraded has no file here, and that is an empty list rather than an error.
    Unparseable lines are skipped -- a log written by a newer core must not take
    the page down.
    """
    try:
        lines = (
            (Path(config_dir) / "upgrade.log")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        return []
    entries: list[LogEntry] = []
    for line in lines:
        match = _LOG_RE.match(line.strip())
        if match is None:
            continue
        details: dict[str, str] = {}
        for token in match.group("rest").split():
            key, sep, value = token.partition("=")
            if sep:
                details[key] = value
        entries.append(
            LogEntry(
                at=match.group("ts"),
                from_version=match.group("from_version"),
                to_version=match.group("to_version"),
                result=match.group("result"),
                details=details,
            )
        )
    entries.reverse()
    return entries[:limit]


# ---------------------------------------------------------------------------
# Reading the runner's output
# ---------------------------------------------------------------------------
#
# papaia-ctl announces each phase on stdout before it starts it, and those
# strings are stable enough to key on: they are the command's own description of
# where it is, and the alternative -- a progress file -- cannot exist, because
# the operation removes the container that would be writing one.
#
# Matching is by prefix and deliberately loose. A phase that stops being
# recognised degrades to "not reached yet" next to a log that still says
# everything; a phase falsely matched would claim progress that did not happen.
# So the markers err towards missing rather than guessing.

PHASE_DONE = "done"
PHASE_RUNNING = "running"
PHASE_PENDING = "pending"
PHASE_FAILED = "failed"
PHASE_SKIPPED = "skipped"

# (key, prefix, contains, label). Order is execution order, which is what makes
# the last matched phase the one in flight.
#
# `contains` exists because two of the announcements share an opening with a
# line that is not a phase at all: "Running 1.2.0's papaia-ctl from here on."
# is the banner phase 2 prints about itself, and matching it as the start of the
# migrations would claim the migrations began before they did. The second word
# is what tells them apart.
_PHASES: tuple[tuple[str, str, str, str], ...] = (
    ("backup", "Creating a restore point", "", "Create a restore point"),
    ("stop", "Stopping and removing the containers", "", "Remove the containers"),
    ("checkout", "Moving the checkout to", "", "Move the checkout to the release tag"),
    ("migrations", "Running ", "migration", "Run the release migrations"),
    ("render", "Applying ", "configuration", "Apply the new configuration"),
    ("start", "Starting the stack", "", "Start the stack and add-ons"),
)

# Reached only at the very end, after `cmd_start` returned.
_COMPLETE_MARKER = "upgrade complete:"

# The first line of `_upgrade_failed`'s block. Everything from here on is the
# operator's way back, printed by papaia-ctl itself.
_RECOVERY_MARKER = "The checkout is on v"

# "No migrations to run." replaces the "Running N migration(s)..." line, so the
# migrations phase has two openings.
_MIGRATIONS_NONE = "No migrations to run"


@dataclass(frozen=True)
class Phase:
    """One step of the upgrade, and how far it got."""

    key: str
    label: str
    state: str
    detail: str = ""


def _strip_prefix(line: str) -> str:
    """Drop papaia-ctl's `[papaia-ctl] ` / `[ok] ` / `[error] ` prefix.

    The bare form matters: `error ""` prints `[error] ` with nothing after it,
    and the trailing space is gone by the time a line reaches here. Those blank
    separator lines sit inside the recovery block, so leaving their prefix on
    would put `[error]` in the middle of what an operator pastes into a shell.
    """
    for prefix in ("[papaia-ctl]", "[ok]", "[error]", "[!]"):
        if line == prefix:
            return ""
        if line.startswith(prefix + " "):
            return line[len(prefix) + 1 :]
    return line


def _phase_match(key: str, prefix: str, contains: str, line: str) -> bool:
    if key == "migrations" and line.startswith(_MIGRATIONS_NONE):
        return True
    return line.startswith(prefix) and (not contains or contains in line)


def phases_from_log(log: str, *, running: bool = True) -> list[Phase]:
    """Turn the runner's output into the six phases, in order.

    `running` is the container's own state. It is what tells a log that simply
    stops -- because the manager was killed mid-phase and the tail was captured
    then -- apart from one that stopped because the phase failed.
    """
    lines = [_strip_prefix(raw.strip()) for raw in log.splitlines()]
    seen: dict[str, str] = {}
    for line in lines:
        for key, prefix, contains, _ in _PHASES:
            if _phase_match(key, prefix, contains, line):
                seen.setdefault(key, line)
    complete = any(_COMPLETE_MARKER in line for line in lines)
    failed = not running and not complete

    order = [key for key, _, _, _ in _PHASES]
    last_seen = max((order.index(k) for k in seen), default=-1)

    phases: list[Phase] = []
    for index, (key, _, _, label) in enumerate(_PHASES):
        if complete:
            state = PHASE_DONE if key in seen else PHASE_SKIPPED
        elif key in seen:
            state = PHASE_DONE if index < last_seen else (
                PHASE_FAILED if failed else PHASE_RUNNING
            )
        else:
            # A phase the run passed without announcing was skipped, not
            # pending: --no-backup is the case that produces this, and calling
            # it "waiting" would leave a spinner on a step that never runs.
            state = PHASE_SKIPPED if index < last_seen else PHASE_PENDING
        phases.append(Phase(key=key, label=label, state=state, detail=_detail(key, seen)))
    return phases


def _detail(key: str, seen: dict[str, str]) -> str:
    """The announcement line itself, where it carries more than the label does."""
    line = seen.get(key, "")
    if key == "migrations" and line.startswith(_MIGRATIONS_NONE):
        return "none in this release"
    if key == "checkout" and line.startswith("Moving the checkout to "):
        return line.removeprefix("Moving the checkout to ").rstrip(".")
    return ""


def recovery_from_log(log: str) -> str:
    """`_upgrade_failed`'s recovery block, verbatim, or "" when it is not there.

    Not re-derived from the version numbers. papaia-ctl already prints the exact
    commands, including which of the two forms applies when the run had no
    restore point, and a second copy of that logic here would drift from the
    shell that owns it.
    """
    lines = [_strip_prefix(raw.rstrip()) for raw in log.splitlines()]
    for index, line in enumerate(lines):
        if line.startswith(_RECOVERY_MARKER):
            return "\n".join(lines[index:]).strip()
    return ""


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


@dataclass
class UpgradeCheck:
    """The result of one `run_check`, cached until the next one."""

    current: str = ""
    target: str = ""
    tag: str = ""
    status: str = ""
    available: list[str] = field(default_factory=list)
    migrations: list[Migration] = field(default_factory=list)
    gate: Gate = field(default_factory=Gate)
    fetch_error: str = ""
    checked_at: str = ""

    @property
    def up_to_date(self) -> bool:
        return self.status == "up-to-date"


_check_lock = asyncio.Lock()
_cached: UpgradeCheck | None = None


def cached_check() -> UpgradeCheck | None:
    """The last check's result, or None when none has run in this process."""
    return _cached


def reset_cache() -> None:
    """Drop the cached result, because the deployment moved under it."""
    global _cached  # noqa: PLW0603
    _cached = None


async def run_check(
    *,
    workspace_dir: str,
    config_dir: str,
    version: str | None = None,
) -> UpgradeCheck:
    """Resolve a target and evaluate it, then cache the answer.

    Serialised: two administrators opening the page at once would otherwise race
    two `git worktree add` calls over the same `.git`. The second waits and then
    re-runs rather than reading the first one's result -- the requested version
    may differ, and answering for the wrong one is worse than the extra work.
    """
    from app.core.ctl import CtlError, run_py_cli  # noqa: PLC0415

    global _cached  # noqa: PLW0603
    async with _check_lock:
        repo = repo_path(workspace_dir)
        state = await checkout_state(workspace_dir)
        if not state.is_git:
            raise UpgradeError(
                f"{repo} is not a git checkout, so it cannot be moved to a release tag"
            )

        fetch_error = await fetch_tags(workspace_dir)
        tags = await local_tags(workspace_dir)

        with tempfile.TemporaryDirectory(prefix="papaia-upgrade-") as tmp:
            tags_file = Path(tmp) / "tags"
            tags_file.write_text("".join(f"v{v}\n" for v in tags), encoding="utf-8")
            flags = [f"--tags-file={tags_file}"]
            if version is not None:
                flags.append(f"--version={version}")
            code, out, err = await run_py_cli(
                command="upgrade-resolve",
                workspace_dir=workspace_dir,
                config_dir=config_dir,
                extra_flags=flags,
            )
        if code != 0:
            raise UpgradeError(err.strip() or "could not resolve a target release")
        resolved = parse_resolve_tsv(out)

        current = resolved.get("CURRENT", "")
        target = resolved.get("TARGET", "")
        tag = resolved.get("TAG", "")
        check = UpgradeCheck(
            current=current,
            target=target,
            tag=tag,
            status=resolved.get("STATUS", ""),
            available=newer_than(tags, current),
            fetch_error=fetch_error,
            checked_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
        )

        if not check.up_to_date and tag:
            try:
                check.gate, check.migrations = await _evaluate_target(
                    workspace_dir=workspace_dir,
                    config_dir=config_dir,
                    tag=tag,
                    current=current,
                    target=target,
                )
            except CtlError as exc:
                raise UpgradeError(str(exc)) from exc

        _cached = check
        return check


async def _evaluate_target(
    *,
    workspace_dir: str,
    config_dir: str,
    tag: str,
    current: str,
    target: str,
) -> tuple[Gate, list[Migration]]:
    """Run the add-on gate and the migration plan against a worktree of `tag`.

    A worktree is the honest way to answer both questions before anything is
    touched -- it is what `cmd_upgrade` itself does, for the same reason: the
    target's ADDON_API window and its migration directory exist only in the
    target's own tree.
    """
    from app.core.ctl import run_py_cli  # noqa: PLC0415

    repo = repo_path(workspace_dir)
    tmp = Path(tempfile.mkdtemp(prefix="papaia-upgrade-target-"))
    worktree = tmp / tag
    try:
        # A worktree left behind by a killed request would make `add` fail with
        # "already registered"; pruning first keeps the check self-healing.
        await _git(repo, "worktree", "prune")
        try:
            await _git(repo, "worktree", "add", "--detach", "--quiet", str(worktree), tag)
        except UpgradeError as exc:
            raise UpgradeError(
                f"could not check out {tag} for inspection. Does the tag exist? ({exc})"
            ) from exc

        code, out, _ = await run_py_cli(
            command="addon-check",
            workspace_dir=workspace_dir,
            config_dir=config_dir,
            repo_root=str(worktree),
            extra_flags=["--json"],
        )
        gate = parse_gate_json(out, exit_code=code)

        code, out, err = await run_py_cli(
            command="upgrade-plan",
            workspace_dir=workspace_dir,
            config_dir=config_dir,
            repo_root=str(worktree),
            extra_flags=[f"--from={current}", f"--to={target}"],
        )
        if code != 0:
            raise UpgradeError(err.strip() or "could not determine the pending migrations")
        return gate, parse_plan_tsv(out)
    finally:
        try:
            await _git(repo, "worktree", "remove", "--force", str(worktree))
        except UpgradeError as exc:
            logger.warning("could not remove the inspection worktree: %s", exc)
        shutil.rmtree(tmp, ignore_errors=True)
