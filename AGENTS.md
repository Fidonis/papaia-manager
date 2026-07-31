# papaia-manager — project context

This document provides structural and architectural context for contributors and automated tooling working in this repository.

---

## What this project does

papaia-manager is a web-based control plane for the papAIa stack's addon lifecycle. It provides a browser UI for discovering, installing, starting, stopping, removing, and updating papAIa addons. It wraps `papaia-ctl` as a subprocess for all mutating operations and imports `lib.*` modules directly from the mounted papAIa workspace for read-only state queries.

It also serves the stack dashboard: a tile overview of the deployed applications, configured through `manager/tiles.yaml` in the papAIa config directory.

A third surface, Backup / Restore (`/backup`), drives the stack-level `papaia-ctl` commands: `backup` as an ordinary job, `restore` in a detached container that outlives the manager (see the Restore model section below). It was called Maintenance up to 0.2.0; the old paths redirect, and the REST prefix is still `/api/v1/maintenance/`.

A fourth surface, Services, reports the declared state of the deployment against the live one. Containers come from a single unfiltered `docker ps -a`, partitioned by `com.docker.compose.project` into the core stack and the active add-ons, grouped by their `de.fidonis.module` label and scored from their healthcheck. The declared half comes from the Compose files themselves — core fragments filtered by `COMPOSE_PROFILES`, add-on fragments named by `deployment.yaml` — so a service that was configured but never started renders as *not deployed* rather than vanishing. It is read-only — lifecycle control over core services stays with `papaia-ctl`, whose core-verb allowlist holds nothing but `backup`. Two aggregates of the same snapshot render as status pills in the header of every page, one per section, for every authenticated role.

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
│       │   │                   #   (separate allowlists for addon and core verbs)
│       │   ├── backups.py      # Read-only backup.yaml / manifest.yaml catalogue access
│       │   ├── runner.py       # Detached `papaia-ctl restore` container
│       │   ├── catalogs.py     # catalogs.yaml CRUD + git clone/fetch operations
│       │   ├── tiles.py        # tiles.yaml: dashboard tiles, visibility filtering
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
│       │   └── api_maintenance.py # /api/v1/maintenance — backup + restore
│       ├── templates/          # Jinja2 HTML templates
│       │   └── partials/           # HTMX fragments returned by mutating/polling routes
│       │       ├── _addon_controls.html      # Per-addon action buttons (install/start/stop/...)
│       │       ├── _env_fields.html          # Rendered env-form fields (typed, masked secrets)
│       │       ├── addon_detail_content.html # Addon detail tab content
│       │       ├── addon_gallery.html        # Addon card grid
│       │       ├── catalog_list.html         # Catalog table rows
│       │       ├── job_status.html           # Polled job progress/log fragment
│       │       ├── restore_point_list.html   # Restore point cards
│       │       └── restore_status.html       # Polled restore-runner state
│       └── static/             # htmx.min.js, alpine.min.js, app.css (Tailwind build)
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
  │  cookie present and valid?  →  extract OIDCClaims
  │  no valid cookie            →  302 /auth/login
  ▼
role dependency  (deps.py → roles.py)
  │  AdminUser  →  MANAGER_ADMIN_ROLE required        (add-ons, catalogs, jobs,
  │                                                    backup, services)
  │  AnyUser    →  admin OR MANAGER_USER_ROLE         (dashboard, status pill)
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
  Manager: set session cookie → 302 /
```

### Job model

All mutating operations (install, start, stop, update, remove, uninstall, catalog refresh, backup) run as `Job` objects through a single-flight FIFO queue backed by a single asyncio worker. Only one mutating job runs at a time. Job state and output are persisted under `$PAPAIA_CONFIG_DIR/manager/jobs/`.

### Restore model

Restore is the one mutating operation that is **not** a job, and the reason is structural rather than stylistic.

`papaia-ctl restore` calls `docker compose down` on the core project before it unpacks any archive, and `papaia-manager` is a service of that same project (`papaia/src/manager/docker-compose.yml`, profile `manager`). A restore running in this process would be SIGKILLed the moment teardown removed its own container — after the stack is down and before anything was put back. `--no-restart` is not an escape: it overwrites volumes underneath live processes.

So `core/runner.py` starts `papaia-ctl restore -y` in a **separate container**, built by cloning the manager's own container spec (`docker inspect` of the container carrying `de.fidonis.module=papaia-manager`): same image, binds, user and supplementary groups. Cloning rather than re-deriving means path parity and docker.sock access hold by construction and the compose fragment stays the only place those mounts are declared.

State lives in Docker. The runner is started without `--rm` and with `--restart no`, so after it exits `docker inspect` still yields its status and exit code and `docker logs` still yields its output — readable by a manager container that was removed and recreated mid-operation. A progress file could not do this: restore replaces `$PAPAIA_CONFIG_DIR` wholesale. The durable cross-restore record is papaia-ctl's own `backup.log` in the backup directory, which is never restored over.

Consequences worth remembering when touching this area:

- `ALLOWED_CORE_VERBS` in `core/ctl.py` contains `backup` and deliberately **not** `restore`.
- Backup and restore are mutually exclusive, enforced in `routers/api_maintenance.py` with 409s.
- The restore-point id is validated against an exact timestamp pattern before it reaches a path join or an argv.
- `PAPAIA_BACKUP_DIR` must be mounted at its host path, or the catalogue is invisible to the container.

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
| `MANAGER_ADMIN_ROLE` | Keycloak realm role granting full access — add-ons, catalogs, jobs, dashboard (default: `admin`) |
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
