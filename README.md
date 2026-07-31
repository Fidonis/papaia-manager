# papaia-manager

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
Maintained by **Fidonis** · See [TRADEMARK.md](TRADEMARK.md) for the trademark notice.

A web-based control plane for the papAIa stack's add-on lifecycle. It gives
non-technical operators a browser UI for discovering, installing, starting,
stopping, removing, and updating add-ons — without needing shell access to
the host running the stack.

The manager does not duplicate orchestration logic: it drives `papaia-ctl`
as a subprocess for every mutating operation and reads shared library
modules directly from the mounted papAIa workspace for status queries, so
CLI and web stay behaviourally identical.

```
┌─────────┐   OIDC + PKCE   ┌────────────────┐   subprocess    ┌───────────┐
│ Browser │ ──────────────▶ │ papaia-manager │ ──────────────▶ │ papaia-ctl│
└─────────┘                 │   (FastAPI)    │                 └───────────┘
                                   │    │                            │
                                   ▼    ▼                            ▼
                              Keycloak  git clone/fetch         docker compose
                              (OIDC)    (add-on catalogs)        (addon lifecycle)
```

## Quick start (Docker)

```bash
cp docker/.env.example docker/.env   # fill in OIDC + path values
docker compose -f docker/docker-compose.yml up -d
```

This builds the image from `docker/Dockerfile` and publishes the UI on
`127.0.0.1:8120`, with a `/health` health check. Every variable is documented
in [`docker/.env.example`](docker/.env.example).

The manager mounts `/var/run/docker.sock` plus the papAIa workspace, config
and backup directories **at their host paths** (path parity — required so that
bind-mount sources in add-on compose files resolve identically whether
`docker compose` is invoked by the manager or by an operator on the host
directly). Because of this, the `manager` profile is **Linux-only**;
Windows/macOS Docker Desktop hosts don't preserve host-path parity through
the VM boundary.

## How it works

**Dashboard and access tiers.** Two Keycloak realm roles gate the UI.
`MANAGER_ADMIN_ROLE` (default `admin`) reaches every surface;
`MANAGER_USER_ROLE` (default `user`) reaches the dashboard only. Accounts
holding neither role are rejected at login. The dashboard at `/` is a tile
overview of the deployed applications, configured by the operator through
`$PAPAIA_CONFIG_DIR/manager/tiles.yaml` and seeded with the applications the
stack ships today on first run. `{{KEY}}` placeholders in tile links resolve
against the core `.env`, and each tile's `visibility: all | admin` is filtered
server-side, so an admin-only tile is absent from a regular user's response
rather than hidden by CSS. Authorization is enforced by the route
dependencies, so the JSON API is restricted exactly like the pages.

**Services.** An admin-only page at `/services` showing what the core stack is
doing right now. Containers are read from `docker ps` and grouped by the
`de.fidonis.module` label the core Compose files put on every service, so each
module lists its own containers with their role, uptime, healthcheck result and
published ports; the module in the worst state sorts to the top. A container
without a healthcheck counts as healthy while it runs, and a one-shot container
that has finished its work is reported as completed rather than dragging its
module down — recognised by its restart policy, so a service that was shut down
still reads as stopped even though it exited just as cleanly. The view is
read-only — starting and stopping core services stays with `papaia-ctl`.

The same data drives a status pill in the header of every page, visible to
every authenticated account regardless of role. It carries the aggregate only —
the worst state across the stack — and links to the services page for
administrators. A user without the admin role therefore learns that something
in the stack is unhealthy, but not which service.

**Catalogs.** A catalog is a source of add-ons — a public or private Git
repository, or a local directory — registered at runtime (not versioned in
this repo). Each catalog is scanned for top-level `papaia-app.yaml`
manifests. Installing an add-on materializes a pinned snapshot
(`git archive` at a specific commit) so that a later catalog refresh never
moves code out from under a running container.

**Status model.** Each add-on resolves to one of five states, merged from
the catalog scan, `deployment.yaml`, and live Docker container labels:
`available`, `installed`, `running`, `inactive`, `unmanaged` (an
operator-managed checkout outside the manager's own snapshot directory).
If the same add-on name exists in more than one enabled catalog, each
distinct version is surfaced as its own entry rather than one silently
hiding the other.

**Jobs.** Installs, updates, and lifecycle verbs that shell out to
`papaia-ctl` can take minutes (image pulls, container starts). These run as
queued `Job` objects processed one at a time by a single worker; the UI
polls for live status and streamed log output.

**Backup / Restore.** A separate, admin-only section for stack-level operations,
served at `/backup` — it was called "Maintenance" up to 0.2.0 and `/maintenance`
still redirects there. The REST prefix stays `/api/v1/maintenance/` so the
versioned surface does not break; it follows the UI name at the next major.
`papaia-ctl backup` archives the config directory, every core volume, and every
active add-on's volumes and data directories; it runs hot (each container is
paused only while its own volume is archived) and therefore runs as an ordinary
job. Restore points are listed from the catalogue papaia-ctl writes next to the
snapshots, with an optional retention period pruning older ones.

Restore is the exception to "everything is a job". `papaia-ctl restore` tears the
core stack down before unpacking archives, and the manager is a service of that
same stack — a restore run in-process would be killed by its own teardown step.
It therefore runs in a **detached container** cloned from the manager's own
container spec (same image, binds, user and groups, so path parity holds by
construction). Docker keeps the state: the runner is started without `--rm`, so
its status and log stay readable by the recreated manager container once the
stack is back up. The page loses its connection while that happens, reconnects on
its own, and reports the outcome.

**Updates.** Refresh the catalog, diff the candidate manifest's
`.env.example` against the installed bundle (new `CHANGE_ME` keys prompt for
values before the job starts), stop the add-on, re-materialize the snapshot
at the new commit, reinstall, and start again.

## REST API

All mutating routes require the `MANAGER_ADMIN_ROLE` and a CSRF header.
Long-running operations return `202` with a job id.

```
GET  /health                              # unauthenticated

GET    /api/v1/catalogs
POST   /api/v1/catalogs                   # {name, type, url|path, ref?, auth?}
PUT    /api/v1/catalogs/{name}
DELETE /api/v1/catalogs/{name}
POST   /api/v1/catalogs/{name}/refresh     # → 202 {job_id}

GET  /api/v1/addons
GET  /api/v1/addons/{name}
GET  /api/v1/addons/{name}/env-form
POST /api/v1/addons/{name}/install         # → 202
POST /api/v1/addons/{name}/start           # → 202
POST /api/v1/addons/{name}/stop            # → 202
POST /api/v1/addons/{name}/remove          # → 202
POST /api/v1/addons/{name}/uninstall       # → 202
POST /api/v1/addons/{name}/update          # → 202
POST /api/v1/addons/{name}/save-config     # → 202
POST /api/v1/addons/{name}/check           # synchronous compatibility check

GET /api/v1/jobs
GET /api/v1/jobs/{id}
GET /api/v1/jobs/{id}/log

GET    /api/v1/maintenance/backup-dir
GET    /api/v1/maintenance/restore-points
GET    /api/v1/maintenance/restore-points/{id}
POST   /api/v1/maintenance/backup            # {retention_days?} → 202 {job_id}
POST   /api/v1/maintenance/restore           # {restore_point, restart_clean?} → 202
GET    /api/v1/maintenance/restore/status
DELETE /api/v1/maintenance/restore           # acknowledge a finished restore
```

## Layout

```
papaia-manager/
├── src/                    # Python application (uv project, Python 3.12+)
│   ├── pyproject.toml
│   ├── uv.lock             # pinned lock, tracked for reproducible Docker builds
│   ├── .env.example
│   └── app/
│       ├── main.py         # FastAPI application factory
│       ├── config.py       # Pydantic Settings
│       ├── auth/           # OIDC + PKCE login, CSRF, admin-role dependency
│       ├── core/           # catalogs, snapshots, status, env-forms, jobs, audit,
│       │                   # services (core-stack container status),
│       │                   # backups (restore-point catalogue), runner (detached restore)
│       ├── routers/        # auth, health, ui, api_catalogs, api_addons, api_jobs,
│       │                   # api_maintenance
│       ├── templates/      # Jinja2 pages + HTMX partials
│       └── static/         # htmx.min.js, alpine.min.js, app.css (Tailwind build)
├── tests/                  # pytest suite (sibling to src/)
└── docker/
    ├── Dockerfile          # multi-stage build; installs Docker CLI + compose plugin
    ├── docker-compose.yml  # local development compose
    ├── git-askpass.sh      # GIT_ASKPASS helper for private catalog auth
    └── .env.example
```

## Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) installed
- A reachable OIDC provider (e.g. Keycloak)
- A papAIa workspace checkout and config directory on the host (Linux)

### Install

```bash
cd src
uv sync
```

### Configure

```bash
cp src/.env.example src/.env
```

The server reads `src/.env`. See [`src/.env.example`](src/.env.example) for
every variable, including the OIDC endpoints, `MANAGER_ADMIN_ROLE`,
`MANAGER_HOST`, and the papAIa workspace/config paths.

### Run

```bash
cd src
uv run uvicorn app.main:app --reload
```

### Run with Docker

See [Quick start](#quick-start-docker) above.

## About Fidonis

`papaia-manager` is built and maintained by **Fidonis** as part of the papAIa
stack. We help companies run their own AI infrastructure end to end — open
source, open standards, no vendor lock-in.

If you are building a similar self-hosted stack and want to talk shop, drop
by at [fidonis.de](https://fidonis.de).

## License

`papaia-manager` is released under the [MIT license](LICENSE) —
*Copyright (c) 2026 Fidonis GmbH (in Gründung) and contributors.*

- See [`TRADEMARK.md`](TRADEMARK.md) for the trademark notice covering the
  name "Fidonis" and the project name `papaia-manager`.
- See [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) for the licenses
  of the third-party Python dependencies bundled with this project.
- See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the *Inbound = Outbound (MIT)*
  rule and the list of license categories that contributions may not
  introduce without prior approval.
