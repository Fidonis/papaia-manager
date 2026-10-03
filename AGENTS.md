# papaia-manager — project context

This document provides structural and architectural context for contributors and automated tooling working in this repository.

---

## What this project does

papaia-manager is a web-based control plane for the papAIa stack's addon lifecycle. It provides a browser UI for discovering, installing, starting, stopping, removing, and updating papAIa addons. It wraps `papaia-ctl` as a subprocess for all mutating operations and imports `lib.*` modules directly from the mounted papAIa workspace for read-only state queries.

It also serves the stack dashboard: a tile overview of the deployed applications, held in `manager/tiles.yaml` in the papAIa config directory and editable in place by administrators.

A third surface, Backup / Restore (`/backup`), drives the stack-level `papaia-ctl` commands: `backup` as an ordinary job, `restore` in a detached container that outlives the manager (see the Restore model section below). It was called Maintenance up to 0.2.0; the old paths redirect, and the REST prefix is still `/api/v1/maintenance/`.

A fifth surface, Upgrade (`/upgrade`), moves the deployment to a newer papAIa release. It is split in two: a read-only check that resolves the target tag, gates the active add-ons against it and lists the pending migrations, and the upgrade itself, which runs `papaia-ctl upgrade` in a detached container (see the Upgrade model section below). The page also lists the Docker images an upgrade leaves behind and removes them on request.

A sixth surface, Audit (`/audit`), reads, exports and prunes the audit log the other surfaces already write to — filtered, paginated JSON/HTML, CSV/JSONL export, and a dry-run-gated prune, all admin-only and CSRF-checked like every other mutating route.

A seventh surface, Host (`/host`), reports memory, CPU, GPU, clock synchronisation, disk space and certificate expiry of the machine underneath the deployment. It measures nothing itself: it runs the core's `doctor` for the six checks it needs and shows the core's verdicts (see the Host health section below). How often it measures is a setting (Settings, "Host monitoring"). Admin-only, like Services; the sidebar status row carries its counts to every role.

A fourth surface, Services, reports the declared state of the deployment against the live one. Containers come from a single unfiltered `docker ps -a`, partitioned by `com.docker.compose.project` into the core stack and the active add-ons, grouped by their `de.fidonis.module` label and scored from their healthcheck. The declared half comes from the Compose files themselves — core fragments filtered by `COMPOSE_PROFILES`, add-on fragments named by `deployment.yaml` — so a service that was configured but never started renders as *not deployed* rather than vanishing. The page also drives lifecycle: one Compose profile at a time via `papaia-ctl start`/`stop --profiles=`, several profiles at once, or the whole stack in a detached container (see the Service group control section below). The same snapshot drives the status row in the sidebar of every page, for every authenticated role; its popover keeps the core stack and the add-ons apart and adds a row for the host.

That snapshot is also the single Docker reading behind the add-on surfaces: `state.compute_status` takes its set of running Compose projects from `StackSnapshot.running_projects` rather than issuing a `docker ps` of its own, so `/addons` and `/services` cannot disagree about whether an add-on is up.

Authentication is handled natively via OIDC Authorization Code Flow with PKCE against Keycloak. Two configurable realm roles gate access: the admin role reaches every surface, while the user role reaches the dashboard only. Authorization is enforced by the route dependencies, so the JSON API is restricted exactly like the pages.

---

## Repository layout

```
papaia-manager/
├── src/                    # Python application (uv project, Python 3.12+)
│   ├── pyproject.toml      # uv project config; ruff, mypy, pytest settings
│   ├── uv.lock             # Pinned dependency lock (tracked for Docker reproducibility)
│   ├── .env.example        # All required env vars with placeholder values
│   └── app/
│       ├── main.py         # FastAPI application factory; startup checks
│       ├── config.py       # Pydantic Settings; all env-var configuration
│       ├── templating.py   # Shared Jinja2 environment
│       ├── auth/
│       │   ├── oidc.py     # OIDC Authorization Code + PKCE client
│       │   ├── roles.py    # Authorization policy: which realm role grants what
│       │   ├── deps.py     # FastAPI dependencies: AdminUser, AnyUser
│       │   └── csrf.py     # Session-bound CSRF Double-Submit token
│       ├── core/
│       │   ├── papaia_lib.py   # sys.path bootstrap + core version handshake
│       │   ├── ctl.py          # Whitelisted subprocess wrapper for papaia-ctl
│       │   │                   #   (separate allowlists for addon verbs, core verbs
│       │   │                   #    and the core's read-only python sub-commands)
│       │   ├── backups.py      # Read-only backup.yaml / manifest.yaml catalogue access
│       │   ├── runner.py       # Detached papaia-ctl container: restore, stack, upgrade
│       │   ├── images.py       # Outdated Docker images: declared vs. used vs. local, removal
│       │   ├── upgrade.py      # Core release check: git state, target resolution,
│       │   │                   #   add-on gate, migration plan, runner-log phases
│       │   ├── catalogs.py     # catalogs.yaml CRUD + git clone/fetch operations
│       │   ├── tiles.py        # tiles.yaml: dashboard tiles, visibility filtering, validation
│       │   ├── settings_store.py # settings.yaml (one section per topic: branding, host) + logo files
│       │   ├── host_health.py  # Memory, CPU, GPU, clock, disk space + certificate expiry via the
│       │   │                   #   core's `doctor`; cached for the configured interval,
│       │   │                   #   single-flight, never raises
│       │   ├── services.py     # Container status from docker ps, by module label; declared
│       │   │                   #   vs. live merge; shared snapshot for the addon surfaces
│       │   ├── inventory.py    # Declared state: compose fragments × profiles, addon manifests
│       │   ├── envfile.py      # Shared KEY=value env-file parsing
│       │   ├── snapshots.py    # git-archive materialization + installed.yaml
│       │   ├── state.py        # Merged addon status (catalog × deployment × Docker)
│       │   ├── envforms.py     # Env-form spec from .env.example + manifest prompts
│       │   ├── envvalidate.py  # Server-side validation/coercion of add-on env values
│       │   ├── resolve.py      # Cross-catalog addon dedup: groups same-name hits by version
│       │   ├── keycloak.py     # Idempotent Keycloak admin REST client registration
│       │   ├── jobs.py         # Single-flight job queue + streaming log store
│       │   └── audit.py        # Append-only JSONL audit log
│       ├── routers/
│       │   ├── auth.py         # /auth/login, /auth/callback, /auth/logout
│       │   ├── health.py       # GET /health (unauthenticated)
│       │   ├── ui.py           # Server-rendered HTML pages
│       │   ├── api_catalogs.py # /api/v1/catalogs — catalog CRUD + refresh
│       │   ├── api_addons.py   # /api/v1/addons — addon lifecycle verbs
│       │   ├── api_jobs.py     # /api/v1/jobs — job status + log streaming
│       │   ├── api_maintenance.py # /api/v1/maintenance — backup + restore
│       │   ├── api_upgrade.py  # /api/v1/upgrade — release check, core upgrade, image cleanup
│       │   ├── api_audit.py    # /api/v1/audit — read, export, prune
│       │   └── api_tiles.py    # /api/v1/tiles — dashboard tile configuration
│       ├── templates/          # Jinja2 HTML templates
│       │   └── partials/           # HTMX fragments returned by mutating/polling routes
│       │       ├── _addon_controls.html      # Per-addon action buttons (install/start/stop/...)
│       │       ├── _env_fields.html          # Rendered env-form fields (typed, masked secrets)
│       │       ├── addon_detail_content.html # Addon detail tab content
│       │       ├── addon_gallery.html        # Addon card grid
│       │       ├── catalog_list.html         # Catalog table rows
│       │       ├── host_list.html            # Host page body: resources, disks, certificates, or why not
│       │       ├── job_status.html           # Polled job progress/log fragment
│       │       ├── restore_point_list.html   # Restore point cards
│       │       ├── restore_status.html       # Polled restore-runner state
│       │       ├── tile_editor.html          # Dashboard editor (admin-only, client-side draft)
│       │       └── tile_gallery.html         # Dashboard tile grid, visibility-filtered
│       └── static/             # htmx.min.js, alpine.min.js, sortable.min.js, app.css (Tailwind build)
├── tests/                  # pytest suite (sibling to src/)
└── docker/
    ├── Dockerfile          # Multi-stage build; installs Docker CLI + compose plugin
    ├── docker-compose.yml  # Local development compose
    ├── git-askpass.sh      # Three-line GIT_ASKPASS helper for private catalog auth
    └── .env.example        # All required env vars for docker-compose
```

---

## Architecture

### Request flow (authenticated pages)

```
Browser
  │  GET /addons
  ▼
SessionMiddleware  (itsdangerous-signed cookie)
  │  cookie present and valid?    →  extract OIDCClaims
  │  access token near expiry?    →  refresh silently via the stored refresh
  │                                  token (deps.py → OIDCClient.refresh)
  │  no session / refresh failed  →  navigation: 307 /auth/login?next=<path>
  │                                  HTMX or /api/ request: 401 (JSON)
  ▼
role dependency  (deps.py → roles.py)
  │  AdminUser  →  MANAGER_ADMIN_ROLE required        (add-ons, catalogs, jobs,
  │                                                    backup, services)
  │  AnyUser    →  MANAGER_ADMIN_ROLE OR MANAGER_USER_ROLE (dashboard, status pill)
  │  role missing  →  403  (HTML page, or JSON under /api/)
  ▼
Route handler
  │  read-only ops:   import lib.* from mounted workspace
  │  mutating ops:    enqueue Job → subprocess papaia-ctl
  ▼
HTML response (Jinja2 + HTMX partials)
```

### OIDC Authorization Code Flow

```
Browser → /auth/login
  Manager: generate state + PKCE pair → store in session
  Manager: 302 → Keycloak (OIDC_ISSUER_KC_AUTH)

Browser ← Keycloak login dialog
Browser → /auth/callback?code&state
  Manager: verify state, exchange code+verifier for tokens (OIDC_ISSUER_KC_TOKEN)
  Manager: validate id_token via JWKS (OIDC_ISSUER_KC_CERTS)
  Manager: require admin OR user role, else 403 without a session
  Manager: store refresh token in the session for silent renewal
  Manager: set session cookie → 302 to the remembered `next`, else /
```

### Job model

All mutating operations (install, start, stop, update, remove, uninstall, catalog refresh, backup) run as `Job` objects through a single-flight FIFO queue backed by a single asyncio worker. Only one mutating job runs at a time. Job state and output are persisted under `$PAPAIA_CONFIG_DIR/manager/jobs/`.

### Restore model

Restore is the one mutating operation that is **not** a job, and the reason is structural rather than stylistic.

`papaia-ctl restore` calls `docker compose down` on the core project before it unpacks any archive, and `papaia-manager` is a service of that same project (`papaia/src/manager/docker-compose.yml`, profile `manager`). A restore running in this process would be SIGKILLed the moment teardown removed its own container — after the stack is down and before anything was put back. `--no-restart` is not an escape: it overwrites volumes underneath live processes.

So `core/runner.py` starts `papaia-ctl restore -y` in a **separate container**, built by cloning the manager's own container spec (`docker inspect` of the container carrying `de.fidonis.module=papaia-manager`): same image, binds, user and supplementary groups. Cloning rather than re-deriving means path parity and docker.sock access hold by construction and the compose fragment stays the only place those mounts are declared.

State lives in Docker. The runner is started without `--rm` and with `--restart no`, so after it exits `docker inspect` still yields its status and exit code and `docker logs` still yields its output — readable by a manager container that was removed and recreated mid-operation. A progress file could not do this: restore replaces `$PAPAIA_CONFIG_DIR` wholesale. The durable cross-restore record is papaia-ctl's own `backup.log` in the backup directory, which is never restored over.

Consequences worth remembering when touching this area:

- `ALLOWED_CORE_VERBS` in `core/ctl.py` contains `backup`, `start` and `stop`, and deliberately **not** `restore`.
- Backup and restore are mutually exclusive, enforced in `routers/api_maintenance.py` with 409s.
- The restore-point id is validated against an exact timestamp pattern before it reaches a path join or an argv.
- `PAPAIA_BACKUP_DIR` must be mounted at its host path, or the catalogue is invisible to the container.

### Upgrade model

Upgrade is the second mutating operation that is **not** a job, for a stronger version of restore's reason: `papaia-ctl upgrade` runs `cmd_stop --clean-up --addons` between its two phases, unconditionally and unscoped. It also `exec`s itself from the target release's tree after moving the checkout, so it cannot be a streamed in-process job even setting the teardown aside.

The read half and the execute half are deliberately separate.

**The check** (`core/upgrade.py`) never changes anything and is split by cost. `current_version`, `checkout_state` and `read_upgrade_log` are file reads plus three local `git` calls, cheap enough to render on page load. `run_check` fetches from the remote and materialises a `git worktree` of the target tag, because the add-on gate has no honest answer without one — only the target's tree carries its own `ADDON_API` window and its Compose service names. It is an explicit operator action, serialised behind an `asyncio.Lock`, and its result is cached for the process. The fetch runs in the manager container, which has no SSH client or key, so an SSH `origin` is fetched through its HTTPS equivalent (`https_equivalent`, passed as a URL — the checkout's configuration is never written). A fetch that still fails is a warning carrying the commands from `fetch_hint`, never "the newest release": the page and the header button say "Could not check" / "Check failed" instead.

The arithmetic itself is delegated straight back to the core: `ALLOWED_PY_COMMANDS` in `core/ctl.py` allows `upgrade-resolve`, `upgrade-plan` and `addon-check`, invoked through `run_py_cli` as `python3 -m lib.cli` with the workspace on `PYTHONPATH` — the same shape `papaia-ctl` uses for itself. Parsing four lines of TSV is the price of the manager and a shell on the host never reaching different verdicts about the same checkout. `upgrade-record` is deliberately absent: it writes the migration ledger, and only the upgrade's own second phase may do that.

**The upgrade** goes through `core/runner.py` as a third `RunnerKind`, next to restore and stack. `--version` is always pinned, never omitted: without it papaia-ctl means "go to whatever is newest", and a tag published between the operator reading the migration list and clicking the button would move the deployment somewhere nobody reviewed. It also makes the runner name deterministic, which is the real mutual exclusion — `docker run` refuses a duplicate name.

Consequences worth remembering when touching this area:

- `ALLOWED_CORE_VERBS` deliberately does **not** contain `upgrade`, and `ALLOWED_PY_COMMANDS` is a separate set with no overlap.
- The manager upgrades itself. The target release pins its own `papaia-manager` image in `papaia/src/manager/docker-compose.yml`, so the `up` at the end of phase 2 recreates this container from a different image than the runner was cloned from. The session survives (`setup --env-only` keeps `MANAGER_SESSION_SECRET`); the page an operator returns to does not — the success strip tells them to reload.
- Phase 2 therefore runs the *new* core's Python under the *old* image's interpreter. Fine today; there is no manager-side mitigation if a future core raises its Python floor.
- `/partials/upgrade/runner` is a **frozen path**. A tab open across the upgrade keeps polling the URL baked into the previous image's markup, so renaming it would leave that tab on a 404 for the whole outage.
- An upgrade is mutually exclusive with every job, restore and stack action, and the guards are symmetric — `api_maintenance.py` and `api_stack.py` refuse while an upgrade runs, not just `api_upgrade.py`.
- A finished upgrade runner is **not** cleared automatically the way a stack runner is. It holds the outcome of the last attempt, and `$CONFIG_DIR/upgrade.log` is what makes dismissing it safe.
- There is no automatic rollback, by design in papaia-ctl. The failure panel renders `_upgrade_failed`'s recovery block verbatim rather than re-deriving it.
- A dirty checkout blocks the upgrade with no override. `--force` degrades the add-on gate only, and is refused outright when the gate passed or failed on an `ERROR`.
- The target version reaches both an argv and a container name, so it is validated with `\Z`-anchored patterns in both `core/upgrade.py` and `core/runner.py` — `$` would also match before a trailing newline.

**Image cleanup.** `papaia-ctl upgrade` pulls the target's images through `docker compose up` and never removes the ones it replaced. `core/images.py` finds them, and it does so without the previous release's tree: an image is *outdated* when it is neither *declared* nor *used* and every reference it carries lives in a repository the stack declares. Declared is `docker compose config --images`, run the way papaia-ctl runs compose — the core file with every override in the config directory (the LocalAI GPU override swaps a tag, which reading the YAML would miss) and each installed add-on, active or not, with its own env file. Used is the image of every container on the host, stopped ones included. The repository condition is what keeps it from being `docker image prune -a`; the per-image (not per-repository) declared check is what keeps `postgres:16` next to `postgres:18.3`.

- **Fail closed.** If any source's declared images cannot be resolved the report carries the reason and no candidate. Removal never uses `-f`, so the daemon's own refusal is the last line of defence.
- `POST /api/v1/upgrade/images/prune` recomputes the candidates under a lock and treats the request's ids as a narrowing, never a widening. It is refused while an upgrade, restore, stack runner or job is running; a *finished* upgrade runner does not block it.
- The option is `prune_images` on `POST /api/v1/upgrade`, off by default at the API and preselected in the dialog. With it, `build_upgrade_run_args` wraps papaia-ctl in a small shell (`runner._UPGRADE_WITH_PRUNE`) that runs `python -m app.core.images prune` only after a zero exit and always exits with papaia-ctl's status: a failed cleanup must not turn a successful upgrade into "upgrade failed". The runner carries the label `de.fidonis.upgrade-prune-images`, which is how the phase list knows to show the step and how a later manager finds out what it was started for.
- The runner container holds the *previous* papaia-manager image, so that one cannot go during the run. `POST /runner/clear` therefore repeats the cleanup after removing a successful, labelled runner. A failed run is never cleaned up.
- Known limits: an image of a service a release drops entirely stays (its repository is no longer declared), and sizes are the images' own, so shared layers are counted twice.

### Service group control

A **service group** is one Compose profile. That is the granularity, because it is the only one `papaia-ctl` accepts — there is no per-service verb. The mapping to modules is many-to-many and read out of the fragments, never derived from the name: `librechat-websearch` covers `firecrawl`, `searxng`, `jinaai` and `mcp-firecrawl`, while `oauth2-proxy` is labelled `papaia-auth`. `inventory.core_groups` is the authority, and the set it returns is the allowlist a request is validated against — a profile that is not in `COMPOSE_PROFILES` is absent from it, and `papaia-ctl` would otherwise hand `docker compose` a profile whose env file setup never rendered.

Two execution models, split on whether the operation removes the container serving the request:

- **Group actions** (`POST /api/v1/stack/groups/{start,stop,restart}`) name profiles explicitly and never `manager`, so they are ordinary queued jobs. `ctl.profiles_flag` builds the `--profiles=` flag and refuses `manager` regardless of what the caller passed.
- **Stack actions** (`POST /api/v1/stack/{start,stop,restart}`) cover every profile including `manager`, so they go through `core/runner.py` in a detached container, exactly like restore. `runner.RunnerKind` keeps the two runner flavours apart by label and name prefix. The outcome is read back from `docker inspect` / `docker logs` by `/partials/stack-runner`.

Further points that are easy to get wrong:

- `restart` is composed as stop → start. papaia-ctl has no restart verb, and adding one would mean a change in the stack repo plus a minimum-version coupling.
- `--clean-up` (`docker compose down` instead of `stop`) attaches to anything that stops — a stop, and the stop half of a restart, where it turns the operation into a full recreate. It is **rejected with 400** on start, which has no such flag; ignoring the field there would confirm an operation that did not happen. After a stop with it the page reports the modules as *not deployed*: a removed container is indistinguishable from one that was never created.
- A stack action never passes `--addons`. That flag exists and would take every add-on down with the core stack — the bulk action this surface deliberately omits. Add-ons are started, stopped and restarted one at a time.
- The selection state lives in an Alpine scope in `services.html`, **outside** `#service-list`. That element is swapped every 15 s, and state held inside it would not survive a single poll — nor would an open confirmation dialog.

### Host health

The Host page and the chip's Host row read the machine, not the containers, and they read it from the core. `app/core/host_health.py` runs `doctor --json` through `run_py_cli` and keeps only `memory`, `cpu`, `gpu`, `time_sync`, `disk_space` and `certs`; the other five checks fork `docker`, resolve names and probe ports, which is not something to repeat on a poll, so they are skipped by name and the answer is filtered to the six that were asked for. A check the core adds later is ignored rather than shown unreviewed.

Consequences worth remembering when touching this area:

- **The verdict is the core's.** Its disk thresholds are free bytes (warn below 10 GiB, fail below 2 GiB) and its certificate thresholds are days (30 and 7); memory, CPU and VRAM have limits of their own in the core. The percentage drawn next to a row is for the eye and never picks a colour; a threshold of this panel's own would let it and a shell on the host disagree about the same disk. For memory, CPU and VRAM the core states the reason for a warning only in its summary sentence, so that sentence is shown as is on the row. Changing a threshold is a change in the core.
- **The skip list may only name old checks.** `--skip` refuses a name the core does not know, so `_SKIPPED_CHECKS` holds only checks every core with `doctor` has, and a new check is requested by *not* skipping it. A 1.4.0 core that predates one simply has no row for it.
- **Not measured is not empty.** The manager mounts the config and backup directories, not `/var/lib/docker`, so the Docker data root is normally absent from `doctor`'s answer and the page says so. An unreadable certificate has no verdict and is left out of the counts. A resource check the core reports as `skip` is a row that says "Not measurable from this panel" and carries the core's reason, and is left out of the counts the same way. The one exception is a `gpu` skip with no details at all: nothing is configured to measure (no LocalAI, or the CPU image), so it gets no row. Today `gpu` and `time_sync` skip whenever `doctor` runs inside a container, which is where the manager runs it, so those two rows read as not measurable until the core can read them from there.
- **Not knowing is not the same as bad.** A core without `doctor` (checked as the existence of `lib/doctor.py` in the workspace, not as a version comparison), a timeout, an unparseable answer and a refused argument all yield `available=False` with a reason. The chip leaves the host out of its headline in that case, the way it leaves out an empty add-on section.
- **Exit 2 is a result.** `doctor` exits 2 when a check failed and still prints the whole document. Exit 2 with nothing on stdout is a refused argument (for instance a skipped check the core renamed), and its first stderr line becomes the reason.
- **Reading is cheap, running is not.** `load_host_health` serves a reading for the refresh interval (60 s by default; 15 s for a failure, or the interval if that is shorter) and runs at most one `doctor` at a time; concurrent callers share the task. The run belongs to the process, not to the request that started it, and is shielded, so a visitor who closes the tab does not cancel it for the next one. "Re-check" bypasses the interval but not a five-second minimum interval.
- **One interval, set in Settings.** `host.refresh_seconds` in `settings.yaml` (10 s to 60 min) is the cache's time to live, the page's poll (rendered into `host.html`'s `hx-trigger`) and, times three with a floor of 180 s, the point where the cache-only consumers stop showing a reading. It is read at every call and judged against the age of the reading, so shortening it takes effect at the next poll. The Host page only displays it. An open page keeps the poll it was loaded with until it is reloaded. The API refuses a value out of range; a hand-edited file is clamped on read, because a validation error would reset every section of the document, the branding included.
- **The chip and the sidebar dot never wait.** They render on every page every 30 s, so they read `cached_host_health()` and ask `ensure_fresh()` for a background refresh. With no page open nothing runs, a cold cache shows no Host row for one poll, and they refresh no faster than every 30 s whatever the interval.
- **Counts only for non-admins.** The chip's Host row says `n / m ok`. Paths, host names and certificate names appear on `/host`, which is admin-only. A test asserts that none of them reaches the chip.
- **`run_py_cli` takes a `limit`.** A child that outlives it is killed and reaped before `CtlError` (exit code 124) is raised. The default is no limit, which the upgrade check relies on.
- **`doctor` is in `ALLOWED_PY_COMMANDS`.** It is read-only by the core's own contract; `tests/test_ctl.py` pins the set.

Known limits: no history (that is what an observability stack is for), no GPU figures and no clock state from inside the manager container until the core can read them there, memory and CPU figures that are the host's own (`/proc/meminfo` and the load average are not namespaced) and not those of any container limit, and thresholds that cannot be tuned from here. Memory, CPU and the clock change faster than disk and certificates, but all of them share one run and one interval.

---

## Engineering conventions

### Branches

| Prefix | Use |
|---|---|
| `feat/<short>` | New user-facing feature |
| `fix/<short>` | Bug fix |
| `docs/<short>` | Documentation only |
| `refactor/<short>` | Refactoring without behaviour change |
| `test/<short>` | Test additions or fixes |
| `ci/<short>` | CI/CD configuration |
| `chore/<short>` | Maintenance |

Never push directly to `main`; always open a pull request.

### PR titles — Conventional Commits

Format: `<type>[(<scope>)][!]: <subject>`

- Subject: lowercase, imperative mood, no trailing period
- `!` suffix marks a breaking change (triggers a major version bump)
- CI enforces this format on every PR

### Merge strategy

All PRs are **squash-merged**. The PR title becomes the single commit message on `main`.

---

## Code style and local checks

Run all checks locally before pushing:

```bash
# YAML — from the repository root
yamllint .

# Python — from src/
cd src
uv run ruff check .
uv run mypy .
uv run pytest -q
```

- **Python linting**: ruff with rule sets E, F, I, B, UP, N, RET, SIM, ASYNC
- **Type checking**: mypy in strict mode; all public functions must carry explicit type annotations
- **Import style**: absolute imports (`from app.config import get_settings`)
- **YAML**: yamllint with the project `.yamllint` config
- **Python version**: 3.12 minimum

---

## Configuration reference

All settings are loaded via Pydantic Settings in `app/config.py`. See `src/.env.example` for the full list with descriptions.

| Variable | Purpose |
|---|---|
| `OIDC_ISSUER_KC_AUTH` | Browser-side Keycloak authorization endpoint |
| `OIDC_ISSUER_KC_TOKEN` | Server-side token endpoint (internal Docker DNS) |
| `OIDC_ISSUER_KC_CERTS` | JWKS endpoint for id_token validation |
| `MANAGER_ADMIN_ROLE` | Keycloak realm role granting full access — add-ons, catalogs, jobs, dashboard (default: `manager-admin`) |
| `MANAGER_USER_ROLE` | Keycloak realm role granting dashboard-only access (default: `user`) |
| `MANAGER_HOST` | Public base URL of the manager (used as OIDC redirect URI base) |
| `MANAGER_OIDC_CLIENT_ID` | Keycloak client ID (default: `papaia-manager`) |
| `MANAGER_OIDC_CLIENT_SECRET` | Keycloak client secret |
| `MANAGER_SESSION_SECRET` | itsdangerous session signing secret |
| `PAPAIA_CONFIG_DIR` | Path to papAIa config directory (must equal host path in container) |
| `PAPAIA_WORKSPACE_DIR` | Path to papAIa workspace (must equal host path in container) |

`PAPAIA_BACKUP_DIR` is **not** a manager setting: the backup location belongs to
the stack, so it is read from `$PAPAIA_CONFIG_DIR/.env` at request time and the
manager and a shell on the host always agree on it. It appears in
`docker/.env.example` only because compose needs it to place the path-parity
mount.

---

## Security boundaries

- **Never log** session secrets, OIDC client secrets, or catalog tokens.
- **docker.sock access is root-equivalent.** The manager container mounts `/var/run/docker.sock`. This is required for Compose operations and is intentional — the manager profile is off by default.
- **Subprocess inputs are whitelisted.** All calls to `papaia-ctl` go through `core/ctl.py` which validates the verb against an allowlist and uses arg arrays (never `shell=True`).
- **Copyleft dependencies are not accepted.** The CI license-check workflow rejects GPL, LGPL, AGPL, EUPL, and similar licences.
