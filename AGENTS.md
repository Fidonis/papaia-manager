# papaia-manager — project context

This document provides structural and architectural context for contributors and automated tooling working in this repository.

---

## What this project does

papaia-manager is a web-based control plane for the papAIa stack's addon lifecycle. It provides a browser UI for discovering, installing, starting, stopping, removing, and updating papAIa addons. It wraps `papaia-ctl` as a subprocess for all mutating operations and imports `lib.*` modules directly from the mounted papAIa workspace for read-only state queries.

It also serves the stack dashboard: a tile overview of the deployed applications, held in `manager/tiles.yaml` in the papAIa config directory and editable in place by administrators.

When the core's optional RAG system is active (profile `rag` in the core `.env`'s `COMPOSE_PROFILES`), administrators also get a computed "RAG" tile group on the dashboard (Qdrant) and a "RAG" category in the sidebar (pages of the manager only, no link out). `core/rag.py` owns this: it reads the profile and `QDRANT_PUBLIC_URL` and never tests whether the URL key exists, because the core keeps it while the profile is off. The tile is added in `_gather_tiles` at render time and is not persisted to `tiles.yaml`; the sidebar gates the whole category wrapper on the profile alone (`rag_enabled`, `templating.py`), so no empty caption remains and a missing URL key does not hide a page. The ingester's own web interface has no tile and no entry: its jobs, runs and credentials are the Ingest jobs pages.

Another surface, Connections (`/connections`), is listed before Collections and manages the vector database connections of the RAG system: create, edit, test and delete them, with the connection `default` (the integrated Qdrant) created automatically, editable and marked as the default. They are stored in the ingester's own store, `ai/rag/catalog/connections.yaml`, so the ingest service uses them unchanged (see the RAG connections section below). Collections (`/collections`), the next surface, manages the Qdrant collections of the selected connection (the default one unless another is chosen) of the RAG system and the Keycloak roles that may use them: list, create and delete collections, and keep any number of role names with the access level `r` or `rw` per collection. It is admin-only and exists only while the `rag` profile is active (a 404 otherwise, after the role check). The roles are stored in the format `qdrant-mcp-rbac` defines, so the MCP server enforces them unchanged (see the RAG collections section below).

A page after those two, Embedding (`/embedding`), puts files into a collection through the ingester: files are uploaded into a staging folder that is deleted after a successful run, or picked in the documents folder, and embedded either as "add and update" (nothing is deleted) or as "replace the collection". The run is the ingester's, and its status is read from it (see the RAG embedding section below).

Two pages after it, Ingest Jobs (`/ingest/jobs`) and Ingest Runs (`/ingest/runs`), manage everything else the ingester does: its jobs (create, edit, pause, run, delete with their runs), their runs with live progress and the file-by-file result, the credentials of remote sources (stored encrypted), the leftovers of deleted jobs, and the catalog file with its defaults. They are another editor of `jobs.yaml`, next to the ingester's own interface and the Embedding page (see the RAG ingest jobs section below).

A third surface, Backup / Restore (`/backup`), drives the stack-level `papaia-ctl` commands: `backup` as an ordinary job, `restore` in a detached container that outlives the manager (see the Restore model section below). The same page schedules backups from inside the manager (see Backup schedule below). It was called Maintenance up to 0.2.0; the old paths redirect, and the REST prefix is still `/api/v1/maintenance/`.

A fifth surface, Upgrade (`/upgrade`), moves the deployment to a newer papAIa release. It is split in two: a read-only check that resolves the target tag, gates the active add-ons against it and lists the pending migrations, and the upgrade itself, which runs `papaia-ctl upgrade` in a detached container (see the Upgrade model section below). The page also lists the Docker images an upgrade leaves behind and removes them on request.

A sixth surface, Audit (`/audit`), reads, exports and prunes the audit log the other surfaces already write to — filtered, paginated JSON/HTML, CSV/JSONL export, and a dry-run-gated prune, all admin-only and CSRF-checked like every other mutating route.

A seventh surface, Host (`/host`), reports memory, CPU, GPU, clock synchronisation, disk space and certificate expiry of the machine underneath the deployment, and what Docker's data takes. It measures nothing itself: it runs the core's `doctor` for the six checks it needs, and in a run of its own for `docker_usage`, and shows the core's verdicts (see the Host health section below). How often it measures is a setting (Settings, "Host monitoring"). Admin-only, like Services; the sidebar status row carries its counts to every role.

An eighth surface, Settings (`/settings`), holds the manager's own configuration in `$PAPAIA_CONFIG_DIR/manager/settings.yaml`, one section per topic: Branding (the name and second line at the top of the sidebar, and an uploaded logo) and Host monitoring (the interval of the Host page's measurements). Admin-only and CSRF-checked like every other mutating route, with every change audited; the logo is the one thing every role fetches, because the sidebar shows it (see the Settings and branding section below).

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
│       │   ├── backup_run.py   # Starting a backup: the enqueue path the button and the schedule share
│       │   ├── schedule.py     # schedule.yaml: model, standard-cron normalisation, presets,
│       │   │                   #   cadence, retention and overdue arithmetic, page/API state
│       │   ├── scheduler.py    # In-process APScheduler: when to start a backup, skip and
│       │   │                   #   retry, catch-up after a start
│       │   ├── runner.py       # Detached papaia-ctl container: restore, stack, upgrade
│       │   ├── images.py       # Outdated Docker images: declared vs. used vs. local, removal
│       │   ├── upgrade.py      # Core release check: git state, target resolution,
│       │   │                   #   add-on gate, migration plan, runner-log phases
│       │   ├── catalogs.py     # catalogs.yaml CRUD + git clone/fetch operations
│       │   ├── tiles.py        # tiles.yaml: dashboard tiles, visibility filtering, validation
│       │   ├── rag.py          # Optional RAG system (core profile `rag`): computed tiles, sidebar links,
│       │   │                   #   the RAG module's settings (`rag_backend`)
│       │   ├── qdrant.py       # Async REST client for Qdrant: api-key header, Qdrant's error shape,
│       │   │                   #   "unavailable" told apart from "this request failed"
│       │   ├── rag_collections.py # Collections and their roles in the format of qdrant-mcp-rbac:
│       │   │                   #   point ids, ACL and meta payloads, create/delete/set_roles
│       │   ├── vectordb/       # Connection types (a registry) and the ingester's connection store:
│       │   │                   #   base (types, ProbeEnv), qdrant_type, ingest_file (connections.yaml,
│       │   │                   #   compare-and-swap writer), catalog_io (the swap the catalog files
│       │   │                   #   share), crypto (enc:1: tokens), jobs_usage,
│       │   │                   #   service (default connection, key rules), errors
│       │   ├── ingest/         # The ingester's catalog and what runs on it: catalog (jobs.yaml and its
│       │   │                   #   compare-and-swap writer), catalog_load (reload and check what it
│       │   │                   #   serves), jobspec (mirror of the job schema and its cross-job rules),
│       │   │                   #   schedules (plain-language schedules <-> the ingester's cron), job_forms
│       │   │                   #   (the editor's state <-> a job), jobs_service (everything the pages do),
│       │   │                   #   secrets (secrets.yaml, encrypted), display (times and sizes), documents
│       │   │                   #   (jailed browsing and selection), uploads (staging folders, removed after
│       │   │                   #   a run), client (the ingester's REST), runs (start, follow, clean up),
│       │   │                   #   watcher (the background clean-up), errors
│       │   ├── settings_store.py # settings.yaml (one section per topic: branding, host) + logo files
│       │   ├── host_health.py  # Memory, CPU, GPU, clock, disk space + certificate expiry via the
│       │   │                   #   core's `doctor`; cached for the configured interval,
│       │   │                   #   single-flight, never raises
│       │   ├── docker_usage.py # What Docker's data takes (`docker system df`) via the core's
│       │   │                   #   `doctor`: its own slower run and cache, never part of the
│       │   │                   #   host reading; stale-while-revalidate, never raises
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
│       │   ├── api_maintenance.py # /api/v1/maintenance — backup, restore, backup schedule
│       │   ├── api_upgrade.py  # /api/v1/upgrade — release check, core upgrade, image cleanup
│       │   ├── api_audit.py    # /api/v1/audit — read, export, prune
│       │   ├── api_stack.py    # /api/v1/stack — service groups and whole-stack actions
│       │   ├── api_settings.py # /api/v1/settings — branding, host monitoring; GET /brand/logo
│       │   ├── api_tiles.py    # /api/v1/tiles — dashboard tile configuration
│       │   ├── api_collections.py # /api/v1/rag/collections — Qdrant collections and their roles
│       │   ├── api_connections.py # /api/v1/rag/connections — connections of the ingester's store
│       │   ├── api_ingest.py   # /api/v1/rag/ingest — uploads, the tree, embedding runs
│       │   ├── api_ingest_jobs.py # /api/v1/rag/ingest — jobs, their runs, credentials, the catalog file
│       │   ├── ui_ingest.py    # /ingest/... pages and their partials
│       │   └── rag_deps.py     # RagAdmin (admin + `rag` profile), the connection service and the
│       │                       #   per-request CollectionStore on the selected connection
│       ├── templates/          # Jinja2 HTML templates
│       │   └── partials/           # HTMX fragments returned by mutating/polling routes
│       │       ├── _addon_controls.html      # Per-addon action buttons (install/start/stop/...)
│       │       ├── _env_fields.html          # Rendered env-form fields (typed, masked secrets)
│       │       ├── _rag_js.html              # Escaping and JSON requests shared by the Connections and Collections pages
│       │       ├── addon_detail_content.html # Addon detail tab content
│       │       ├── addon_gallery.html        # Addon card grid
│       │       ├── backup_schedule.html      # Schedule and last-backup strip (backup page)
│       │       ├── backup_schedule_preview.html # Schedule editor: live validation and next runs
│       │       ├── catalog_list.html         # Catalog table rows
│       │       ├── collection_list.html      # Collections page body: collections, roles, dialogs, or why not
│       │       ├── connection_list.html      # Connections page body: connections, file problems, dialogs
│       │       ├── embedding_body.html       # Embedding page body: collection, sources, mode, run
│       │       ├── embedding_status.html     # Polled run strip and the latest runs
│       │       ├── embedding_tree.html       # One level of a file tree, a checkbox per entry
│       │       ├── embedding_uploads.html    # The staged uploads
│       │       ├── ingest_*.html, _ingest_*.html # Ingest jobs: list, job tabs, runs, credentials, leftovers,
│       │       │                                 #   the editor's dialogs and the scripts the pages share
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

All mutating operations (install, start, stop, update, remove, uninstall, catalog refresh, backup) run as `Job` objects through a single-flight FIFO queue backed by a single asyncio worker. A scheduled backup is enqueued onto the same queue. Only one mutating job runs at a time. An embedding run is deliberately not a job: it lives in the ingester, and a run of hours must not hold up a backup or an add-on action. Job state and output are persisted under `$PAPAIA_CONFIG_DIR/manager/jobs/`.

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

### Backup schedule

The Backup page can start backups on a schedule. The schedule is one file, `$PAPAIA_CONFIG_DIR/manager/schedule.yaml` (`core/schedule.py`), and the only source of truth: `core/scheduler.py` runs APScheduler 3.x with a **memory** job store and is rebuilt from the file, so a removed schedule cannot come back from a second store after a restart. APScheduler 4.x is not an option while every 4.x release is an alpha.

The scheduler decides *when*. The backup itself goes through `core/backup_run.enqueue_backup`, the path the button uses, so the job, its log and its audit entry (`user=scheduler`, `params.trigger=schedule`) are the same. What differs is the refusal: the button answers a busy queue, a running restore or upgrade, or an unmounted backup directory with a 409, while a scheduled run is skipped (audit `backup.schedule.skip`, result `skipped`) and retried every 10 minutes, up to six attempts. It cannot simply be queued behind the other work, because a restore or upgrade is not in the queue.

Consequences worth remembering when touching this area:

- **Standard cron, not APScheduler's dialect.** APScheduler 3.x counts weekdays from Monday (`0 3 * * 1` fires on *Tuesday*) and ANDs a restricted day-of-month with a restricted weekday, where cron ORs them. `parse_fields` rewrites weekdays to names and refuses the combination; a test pins the Monday case. Never hand an expression to `CronTrigger.from_crontab`.
- **Cadence is neutral across clock changes.** Around one, the wall clock and elapsed time disagree: a daily 03:00 is 24, 23 or 25 hours apart, and an hourly schedule is 2 hours apart on the wall clock across the spring change and runs twice at 02:00 across the autumn one. `analyse_cadence` measures the shortest gap in elapsed time, so an hourly schedule is not refused in a zone with daylight saving time, and takes the longest as the larger of each gap's smaller reading, so "daily" stays 24 hours and the retention floor and the overdue limit do not move with the season. Its loops step through real instants (`_after`), never `run + 1 s`: a zoned datetime forgets which side of a clock change it is on when it is added to, and the walk would go back into the hour that repeats. A time that does not exist on the day the clocks spring forward is shown where it happens (02:30 becomes 03:30).
- **Known limit: the hour that repeats.** APScheduler 3.x runs a fixed time inside the hour the clocks repeat twice on that day (02:30 CEST and 02:30 CET). With the single-flight queue that is a second backup an hour later, once a year, and it is not worked around. `test_a_fixed_time_inside_the_repeated_hour_runs_twice_on_that_day` pins the behaviour, so a fixed APScheduler fails it and the note can go.
- **The timezone is part of the schedule**, defaulting to the container's `TZ` and then UTC, and `tzdata` is a dependency so `zoneinfo` works on any base image and in tests on Windows.
- **One margin everywhere.** *Overdue* is 1.5 times the longest gap between two runs, and the status strip, the catch-up at start and the retention guard all read the same `overdue_after`.
- **Catch-up reads the catalogue, not memory.** The manager restarts on every upgrade, restore and reboot, and a memory job store cannot know a slot was missed. At start, if the newest restore point with `result == ok` is older than the limit, one run is queued 120 seconds later. A `partial` restore point is usable but is not a success.
- **Retention is held back while the newest success is overdue.** `papaia-ctl backup` prunes after every run, a failed one included, and `prune()` keeps no last usable restore point, so a long series of failed backups would delete every good one. `plan_retention` passes `--retention-period-days` only while a recent `ok` point exists, and the job log says when it did not. In the schedule the retention is at least 1 day and at least twice the longest gap; `0` is not allowed.
- **A damaged `schedule.yaml` is reported, never read as "no schedule".** `load_schedule` returns the reason, the page shows it, and `DELETE` clears the file.
- **PUT and DELETE are refused while a restore or upgrade runner runs** (they replace `$PAPAIA_CONFIG_DIR`, and a schedule written meanwhile would be overwritten with the state being restored), and PUT is refused without a reachable backup directory. A restore also returns `schedule.yaml` to the state of the restored snapshot.
- **`AsyncIOScheduler` binds to the loop it is created in**, so it is built in the startup hook, after the job queue, and a failure to start costs the schedule and not the manager. Its `shutdown()` runs a turn later and keeps reporting `running`, so `BackupScheduler.shutdown` is idempotent on its own flag.
- **The scheduler assumes one process.** The image runs a single uvicorn worker; with `--workers` every worker would fire.
- **The editor's preview is its validation.** `GET /partials/backup/schedule/preview` runs the same checks as the PUT, compiles the presets (daily, weekly, every N hours) to cron on the server and carries the result in hidden fields, so the page script holds no copy of those rules and Save sends what was previewed.

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
- **Not measured is not empty.** The manager mounts the config and backup directories, not `/var/lib/docker`, so the Docker data root is normally absent from `doctor`'s answer. The page has no row for its free space, because a row that only said "not measurable" read as a hole where a reading belonged; what Docker holds is shown under "Docker usage" instead. A data root the core can measure is an ordinary disk row. An unreadable certificate has no verdict and is left out of the counts. A resource check the core reports as `skip` is a row that says "Not measurable from this panel" and carries the core's reason, and is left out of the counts the same way. The one exception is a `gpu` skip with no details at all: nothing is configured to measure (no LocalAI, or the CPU image), so it gets no row. A core that predates `Fidonis/papaia#210` skips `gpu` and `time_sync` whenever `doctor` runs inside a container, which is where the manager runs it, so against such a core those two rows read as not measurable; a core that can read them there fills them in from the same fields.
- **Not knowing is not the same as bad.** A core without `doctor` (checked as the existence of `lib/doctor.py` in the workspace, not as a version comparison), a timeout, an unparseable answer and a refused argument all yield `available=False` with a reason. The chip leaves the host out of its headline in that case, the way it leaves out an empty add-on section.
- **Exit 2 is a result.** `doctor` exits 2 when a check failed and still prints the whole document. Exit 2 with nothing on stdout is a refused argument (for instance a skipped check the core renamed), and its first stderr line becomes the reason.
- **Reading is cheap, running is not.** `load_host_health` serves a reading for the refresh interval (60 s by default; 15 s for a failure, or the interval if that is shorter) and runs at most one `doctor` at a time; concurrent callers share the task. The run belongs to the process, not to the request that started it, and is shielded, so a visitor who closes the tab does not cancel it for the next one. "Re-check" bypasses the interval but not a five-second minimum interval.
- **One interval, set in Settings.** `host.refresh_seconds` in `settings.yaml` (10 s to 60 min) is the cache's time to live, the page's poll (rendered into `host.html`'s `hx-trigger`) and, times three with a floor of 180 s, the point where the cache-only consumers stop showing a reading. It is read at every call and judged against the age of the reading, so shortening it takes effect at the next poll. The Host page only displays it. An open page keeps the poll it was loaded with until it is reloaded. The API refuses a value out of range; a hand-edited file is clamped on read, because a validation error would reset every section of the document, the branding included.
- **Docker usage is measured apart.** `app/core/docker_usage.py` runs `doctor` for the `docker_usage` check alone (every other check in the core's registry is skipped), behind a cache of its own. It is the one check that costs the daemon real work: `docker system df` sizes every volume, which took 1.5 s with 75 volumes and grows with the data. So it is measured every tenth refresh interval, never sooner than 5 minutes and never later than an hour (`usage_interval`); a failed reading is retried after 2 minutes; "Re-check" gets a fresh one at most every 30 s. A visitor waits for it only when there is no reading yet or after "Re-check"; otherwise the last reading is served as it stands and a new run starts behind the page. Its failure or slowness is a note under the disk space and cannot hold up, empty or fail the host reading. It is a report with no verdict, so it is never in the counts, the chip or the dot.
- **The core is asked what it has before it is asked for it.** `doctor` cannot list its checks and `--skip` refuses a name it does not know, so `docker_usage.core_checks` reads the registry out of the core's `lib/doctor.py` (`("name", check_...)` entries) and `docker_usage` is skipped by the host run, and requested by its own, only for a core that has it. An older core gets neither: no run, no section, no refused argument.
- **The chip and the sidebar dot never wait.** They render on every page every 30 s, so they read `cached_host_health()` and ask `ensure_fresh()` for a background refresh. With no page open nothing runs, a cold cache shows no Host row for one poll, and they refresh no faster than every 30 s whatever the interval.
- **Counts only for non-admins.** The chip's Host row says `n / m ok`. Paths, host names and certificate names appear on `/host`, which is admin-only. A test asserts that none of them reaches the chip.
- **`run_py_cli` takes a `limit`.** A child that outlives it is killed and reaped before `CtlError` (exit code 124) is raised. The default is no limit, which the upgrade check relies on.
- **`doctor` is in `ALLOWED_PY_COMMANDS`.** It is read-only by the core's own contract; `tests/test_ctl.py` pins the set.

Known limits: no history (that is what an observability stack is for), no GPU figures and no clock state from inside the manager container from a core that cannot read them there, the free space of the Docker data root (it is not mounted; what Docker holds is shown instead), memory and CPU figures that are the host's own (`/proc/meminfo` and the load average are not namespaced) and not those of any container limit, and thresholds that cannot be tuned from here. Memory, CPU and the clock change faster than disk and certificates, but all of them share one run and one interval.

### Settings and branding

The manager's own configuration is one document, `$PAPAIA_CONFIG_DIR/manager/settings.yaml` (`core/settings_store.py`), with one top-level section per topic: `branding` and `host` today. The uploaded logo lives next to it in `manager/branding/`, so both travel with a backup and come back with a restore. Without the file nothing changes: every field has a default, and the sidebar looks as it always did.

Consequences worth remembering when touching this area:

- **A reading never fails.** A missing file, a missing section, an unreadable document or a section written by a newer release yields defaults, and unknown keys are ignored on read and dropped on the next save. `effective_branding` is what the sidebar renders and it never raises; a settings problem must not take the page down.
- **`None` is not empty.** In `branding`, `None` means "use the built-in default" and an empty tagline is a deliberate "show no second line". `validate_branding` keeps the two apart: a blank name falls back to the default, a blank tagline hides the line.
- **The API is strict, the file is clamped.** `host.refresh_seconds` read from the file is clamped to 10 s to 60 min (and a non-number becomes the default), because a pydantic error would send `load_settings` back to the defaults for the whole document and reset the branding over a number somebody edited by hand. `PUT /api/v1/settings/host` refuses an out-of-range value with a 422 instead.
- **Stale writes are refused.** `revision` is the SHA-256 of the file's bytes (empty when there is none). Saving the branding text, resetting it and saving the host interval carry the revision the page was loaded with and answer 409 when it no longer matches. Uploading and deleting the logo do not. Writes go through a temporary file and a rename.
- **The logo type is decided by its bytes.** `save_logo` looks at magic bytes (PNG, JPEG, WebP, SVG) and ignores the client's filename and `Content-Type`. At most 512 KB are read, one byte past the cap so an oversized body is refused without being buffered. An SVG that contains a script, `foreignObject`, an event handler, `javascript:`, an entity declaration or an `iframe` is refused at upload.
- **The logo is served defensively.** `GET /brand/logo` is open to every signed-in role, since the sidebar shows it to all of them, and answers with `X-Content-Type-Options: nosniff` and `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox`, so an SVG opened directly by URL cannot run anything. The URL carries the stored filename (`?v=`), which changes with the content, so the one-day private cache cannot show a stale logo.
- **Every change is audited** as `settings.branding.update`, `settings.branding.logo.upload`, `settings.branding.logo.delete`, `settings.branding.reset` or `settings.host.update`, with target `settings`.

### RAG connections

The page and its API (`/connections`, `/api/v1/rag/connections`) manage the ingester's connection store, `$PAPAIA_CONFIG_DIR/ai/rag/catalog/connections.yaml`. The ingester reads it as `/config/catalog/connections.yaml`, and its own web interface still edits it, so the manager is a second writer of a file it does not own. `core/vectordb/` holds all of it; `tests/test_vectordb_*.py` and `tests/test_api_connections.py` pin it.

Consequences worth remembering when touching this area:

- **The format is the ingester's, exactly.** `{version: 1, connections: [{name, url, api_key?}]}`, with `api_key` as `enc:1:` plus a Fernet token whose key is the URL-safe base64 of the SHA-256 of `QI_CONNECTIONS_SECRET` (`core/vectordb/crypto.py`; the reference token in `tests/test_vectordb_crypto.py` was produced by the ingester's own code). Its entry schema forbids any other key, and a rejected entry makes it refuse the whole file at runtime: it keeps its previous set and reports `degraded`. So `type`, `default` or a timestamp are never written. `ConnectionEntry` mirrors the schema, and every candidate is validated against it before it reaches the file.
- **The default connection is the entry named `default`.** There is no second place that could drift from the file. `ensure_default()` creates it (the integrated Qdrant at `http://qdrant:6333`, the address the ingester uses, with `QDRANT_JWT_SECRET` encrypted) at start and on first use of a RAG page. It never overwrites an existing entry and never raises. It does nothing without the profile or without both secrets, and not again for a minute after a failed attempt. Its address and key are editable, it cannot be renamed or deleted, and "Reset" writes both again. While the store has no `default`, `resolve("default")` answers from `QDRANT_URL` and `QDRANT_JWT_SECRET`, so the Collections page never depends on the file.
- **`QDRANT_URL` is the manager's reach, not the ingester's.** It replaces the stored address of an entry that points at the integrated Qdrant when the manager connects (`ConnectionService._reach`), and is never written. The page shows both when they differ.
- **A stored key goes only to the address it was stored with.** Otherwise an administrator could send `QDRANT_JWT_SECRET`, which also derives the MCP tokens, to a host of their choosing. Testing a stored connection ignores an address that comes with the request, and a changed address on a connection with a key needs the key again or its removal (422). A key that is not touched keeps its stored token byte for byte, because a Fernet token is randomised and re-encrypting would rewrite the file. An address with credentials in it is refused.
- **A write is compare-and-swap.** `IngestFileRepository.update` reads the bytes, applies a mutator to the parsed document, validates, stages the result in `.connections.yaml.manager.tmp` (the ingester stages in `.connections.yaml.tmp`, which would collide), re-reads the file and swaps only if it still has the bytes the change was computed from; otherwise it computes the change again, up to three times. The mode of the file is kept, the previous content goes to `connections.yaml.bak` on a best-effort basis, the catalog directory is never created, and an `OSError` while writing is the "read-only" state. A change to an entry carries the etag of that entry rather than a fingerprint of the file, so another administrator's or the ingester's write elsewhere in the file is no conflict.
- **Problems that were already there do not lock the file.** The ingester refuses any write while the file has an error. The manager refuses only a write that introduces a new one, so the repair after a rotation of the secret (every key unreadable) stays possible. Structural damage (not YAML, not a mapping, a version other than 1, `connections` not a list) refuses everything, and the page shows it. A change is a surgery on the parsed document, so top-level keys and entries the manager does not understand stay.
- **Known limit: the ingester does not compare before it replaces.** Its interface reads the file, edits it and replaces it. A save of its own that began before a manager save and ends after it replaces the manager's change, and the manager cannot close that window from its side. It is milliseconds wide for people saving by hand; two processes writing in a tight loop lost about half of the changes. A fix belongs in the ingester. The view of a change is taken from the file as it was right after the write, so such a loss does not turn a successful change into an error.
- **Names are fixed, jobs are checked.** Ingest jobs refer to a connection by name and nothing tells them about a rename, so a name cannot change. A delete is refused while a job in `jobs.yaml` (`jobs[*].target.connection`) uses the connection, and also when `jobs.yaml` cannot be read. Changing the address of a used connection needs `confirm_jobs`.
- **The key is never an output.** Views carry `has_key` and a state (`ok`, `none`, `unreadable`), `Connection.api_key` is out of `repr`, and the audit log records `key_action` (`set`, `none`, `kept`, `replaced`, `removed`), never a value or a token. Tests search the responses, the audit file and the log for the keys and the stored token. `unreadable` means the token does not decrypt with the current `QI_CONNECTIONS_SECRET`: it was rotated, or it is quoted (the manager's `.env` parser keeps quotes that Compose strips, and the core writes the secret unquoted). `key_drift` flags a default whose stored key is no longer `QDRANT_JWT_SECRET`.
- **TLS.** Connections are verified against the public CAs and, with `SSL_CERT_FILE`, the stack's own as well (`tls_verify`). The ingester's schema has no per-connection TLS option, so a self-signed Qdrant outside the stack is not supported.
- **Every change is audited** as `rag.connection.create`, `.update`, `.delete`, `.reset`, `.test` or `.seed` (user `manager`).
- **Verified against the real code.** A file written by the manager loads with the ingester's own `load_connections` and its keys decrypt there, and a file edited with the ingester's own writer reads back in the manager, in throwaway directories with the ingester's unmodified modules. Repeat this when the ingester's schema or cipher changes.

#### Adding a type of vector database

A type is a class that satisfies `ConnectionType` (`core/vectordb/base.py`) and is registered with `register_type`, as `QdrantType` is in `core/vectordb/__init__.py`. It declares its fields (`FieldSpec`; the dialog renders them, so the page needs no change), validates its values, turns them into the stored entry, says where the database is (`address_of`, the address a stored key is bound to) and probes it. Its `capabilities` decide where it appears: only a type with `collections` is offered on the Collections page. Two things are not generic yet, on purpose, and belong to the first second type:

- **Where its connections are stored.** The ingester reads only Qdrant entries. An entry without a type is a Qdrant, and a type the ingester cannot use should get a store of the manager's own (`manager/connections.yaml`, the same envelope plus a `type` key), so the ingester's file is never broken. `ConnectionService` talks to `IngestFileRepository` only; it is where a repository per type goes. Until the ingester's schema accepts a `type` key, the validation of every candidate refuses to write one into its file.
- **How the Collections page gets a store.** `get_store` in `routers/rag_deps.py` builds a `CollectionStore` on a `QdrantClient`. Another type brings its own store behind the same interface, or stays off the `collections` capability.

### RAG collections

The page and its API (`/collections`, `/api/v1/rag/collections`) write to the Qdrant of the selected connection as the holder of its api-key, so the only authorization is `AdminUser` and the CSRF check. Qdrant is reached through `core/qdrant.py` (plain `httpx`, no `qdrant-client`), and `core/rag_collections.py` holds the storage contract of `qdrant-mcp-rbac`. Nothing on the MCP server's side pins that contract, so `tests/test_rag_collections.py` does, against literals computed with its code.

Consequences worth remembering when touching this area:

- **One connection at a time, named in every request.** The `connection` query parameter (the default connection when absent) goes through `get_store`: an unknown name is a 404, a type without the `collections` capability a 422, and a key that cannot be read a store that explains why (a state, not an error). The partial wraps its body in an element that carries the connection it was rendered for (`data-connection`), and every change is sent to that connection, not to whatever the selector shows by then. The meta and ACL collection names and the operator role are the same on every connection.
- **Roles are editable on every connection, but enforced on one.** The stack's MCP server reads the ACL collection of the integrated Qdrant only. On a connection that does not point at it (`roles_enforced` is false) the roles are stored in the same format and the page says nothing in the stack enforces them there.
- **The roles are in the ACL collection, not in the meta collection.** `_rbac_acl` holds one point per (role, collection): id `uuid5(8c9f3b0e-4a5d-4d0a-9f1e-7d6c5b2a1f00, "<role>|<collection>")`, vector `[0.0]`, payload `{role, collection, access, doc_policy}` with `doc_policy` an explicit `null`. `_collection_meta` holds one point per collection, `{collection, embedding_model, vector_dimension}`, id `uuid5(9e3a5c2f-8b7d-4f1e-a6b3-2d8c9e4f1a02, "<collection>")`, and no roles. The MCP server skips a point it cannot parse without a word, so a payload that does not match exactly is a grant that silently does not exist. Its own `AGENTS.md` states the id formula differently; its code is authoritative.
- **Access levels.** `r` and `rw` are per collection. `m` is global manage: the MCP server returns a manage token for any role that holds one, whatever the `collection` field says, and `*` is only the convention. The page lists those under "Access to every collection" and never edits them, and neither lists nor deletes an `m` grant that names a collection.
- **Nothing cascades in Qdrant, and the ids are deterministic.** Deleting a collection leaves its meta point and its grants behind, and re-creating the name would revive the grants. `delete` removes all three and is idempotent, so a delete that stopped half-way is finished by repeating it. `create` removes what it wrote if a step after the collection exists fails.
- **An access change keeps `doc_policy`.** The policy is not edited here, but another tool may have set it; a changed point keeps its id and its policy, and an unchanged role is not written again.
- **The ingest operator role is never listed per collection.** It always has access to every collection, so it is shown locked, refused in a role list (422), and its `r`/`rw` points are hidden. A global `m` grant `(role, "*", "m")` makes the MCP server treat it as manage; it is written with every change and from the banner when it is missing. Changing `QI_OIDC_OPERATOR_ROLE` does not remove the grant of the old name, and the Keycloak realm still only knows `qdrant-ingest-operator`.
- **Names come from two places that the core does not tie together.** The MCP server reads `EMBEDDING_META_COLLECTION` and `RBAC_ACL_COLLECTION`, the ingester `QI_EMBED_META_COLLECTION` and `QI_RBAC_ACL_COLLECTION`; the manager follows the MCP server's name first and warns when the two disagree. The core passes none of them on to the MCP server, so a changed name takes effect for the ingester only. Until it does, changing the names is not supported.
- **A collection created without a model is open to any model.** The ingester writes the meta point at the end of its first run and refuses a later run whose model differs from a recorded one. Creating with a model makes it text-searchable through the MCP server straight away, and binds ingest jobs to that model.
- **System collections are invisible.** The two configured names are never listed, created, deleted or given roles. A new collection cannot start with an underscore, which the services reserve for theirs. Collections the ingester created may have names this page would not accept, so deleting and editing roles accept any existing name.
- **Role names are exact.** The MCP server matches them case-sensitively against the realm and client roles of the token. They are trimmed, but not otherwise changed, and `|` is refused because it separates role and collection in the point id.
- **The profile is checked after the role.** `RagAdmin` resolves `AdminUser` first, so a signed-out browser still gets the login redirect and a non-admin the 403; only an administrator on a deployment without the profile gets the 404.
- **Qdrant being unavailable is a state, not an error.** An unreachable Qdrant, a refused key and a missing key become a reason on the page (`CollectionsView.available`), and a write answers 503. The api-key is never part of a message, a log line or an audit entry. A collection that disappears between the listing and its detail call is still listed, without numbers.
- **Every change is audited** as `rag.collection.create`, `rag.collection.delete`, `rag.collection.roles.update` or `rag.collection.operator-grant`, with the collection as target (`*` for the grant). The grant is audited only when it was actually written.
- **Verified against the real code.** The grants and meta points the manager writes were read back with `qdrant-mcp-rbac`'s own loader and token builder (including the operator role resolving to global manage), and `qdrant-ingest`'s writer accepted the collections and enforced model and dimension, against a throwaway Qdrant. Repeat this when either contract changes.

### RAG embedding

The page and its API (`/embedding`, `/api/v1/rag/ingest`) put files into a collection through the ingester. `core/ingest/` holds all of it; `tests/test_ingest_*.py` and `tests/test_api_ingest.py` pin it, against `tests/fake_ingest.py`, a fake that reads the real `jobs.yaml` on a reload.

Consequences worth remembering when touching this area:

- **The ingester can only run what `jobs.yaml` declares and cannot be handed a file.** So the manager keeps two jobs per (connection, collection) in `ai/rag/catalog/jobs.yaml`, one per kind of source, with the id prefix `mgr-` (`catalog.managed_job_id`). Only entries with that prefix are ever written; everything else in the file stays as the operator or the ingester's own interface left it. The job is manual-only and its stored mode is `append`, which cannot delete or update, so a run started from the ingester's own interface can do no harm; the manager chooses the real mode in each run request. The upload job's path is changed for every run, and a path that no longer exists scans as empty.
- **A document's identity is the job, the source label and the path inside the source.** The ingester derives every point id from them, which is why the job ids and the labels (`manager-upload`, `manager-folder`) are stable and why uploading the same relative path again replaces a document instead of adding a copy. The state rows outlive the files, so this holds after the staged files are gone.
- **Add and update needs `delete_vanished: false` (`Fidonis/qdrant-ingest#41`).** Without it `upsert` removes every source that is missing from the scan, and the staged files are gone after each run. An ingester without the option answers a run request carrying it with 422 `extra_forbidden`; `IngestClient.supports_update_without_delete` finds that out with a request for a job id no job can have (`_probe`, answered 404 by a new ingester and 422 by an old one), so nothing is started to ask. Replace works with any ingester that has `full_scope: collection`.
- **The ingester refuses a catalog with one invalid job as a whole and keeps serving the previous one.** So a job that exists after a reload proves nothing: `_loaded_as_written` compares the served definition with what was written, and a mismatch rolls the write back (`JobsFileRepository.restore`, which refuses to overwrite somebody else's later change). The cross-job rules (one connection and one model per collection, distinct labels, the connection exists) are checked before the write (`catalog_problems`); They were compared with the ingester's own loader when this was written (see the last item).
- **The file is rewritten, not edited.** Comments and blank lines in `jobs.yaml` are lost on a write, as when the ingester's form saves a job; the previous content is kept as `jobs.yaml.bak` and nothing is written when the entry is already right. The writer shares its compare-and-swap with the connection store (`vectordb/catalog_io.py`).
- **Paths are jailed by construction** (`documents.py`): plain segments only, every segment `lstat`-ed on the way down from a root resolved once, a symbolic link anywhere refuses the path, the staging area is hidden from the folder tree and from every selection of it, and a name with `*` or `?` is refused because the ingester's glob dialect has no escape. The folder job always carries `exclude: uploads/**`, so a selection of the whole folder cannot read another administrator's upload. Tests that need a link are skipped where the platform cannot create one; run them on Linux.
- **An upload is a loan, not storage** (`uploads.py`): one staging folder per owner and upload (`uploads/<owner>/<id>/`, `0700`/`0600`), a manifest outside the documents folder (the ingester would embed it), names checked rather than repaired, limits enforced while the bytes arrive, removal folder first and record second. It is removed after a run with status `success` and no failed document, kept for a retry otherwise, and removed by the time limit whatever its state, except while its run is verifiably still working. The multipart body is read inside the handler after the role and CSRF checks, because FastAPI parses a declared body before it looks at who sent it.
- **Cleaning up does not depend on a browser.** `reconcile` applies the rules and is called by the status strip when it sees a run end and by `IngestWatcher`, a task built in the startup hook (every few seconds while an upload is being embedded, once a minute otherwise). It writes nothing but what a manifest records, so a restart or two passes at once lose nothing. It assumes one process.
- **The status is the ingester's** (`GET /v1/runs`), so it survives a restart of the manager. The ingester writes its counters when a run ends, so while one works the page shows the points written so far, counted in Qdrant by `ingest_run` (`CollectionStore.count_run_points`); that is a hint and may be absent. An abort is cooperative and takes effect between documents, which the page says.
- **One run per collection, over both kinds of source**, because the upload job's path is the run's source. None starts while a restore, an upgrade or a stack action runs.
- **The token is never an output.** `QI_API_TOKEN` is read from `ai/rag/.env` at request time (`RagSecrets.ingest_api_token`) and goes into one header. Audit entries (`rag.ingest.upload.*`, `rag.ingest.run.*`) carry counts and ids, never file names.
- **A custom `QI_LOCAL_MOUNT` is invisible to the manager** unless it lies inside the configuration or workspace directory; `documents_dir` says so and the page disables what depends on it. A backup of the configuration directory contains an upload that is still staged; excluding `ai/rag/documents/uploads` there is a change in the core.
- **Verified against the real code.** The jobs the manager writes load with the ingester's `load_catalog`, its include globs select exactly the intended files under the ingester's `scan_tree`, and a run through the real ingester and Qdrant (add, update by path, unchanged file, folder source, replace, failure with retry, a refused catalog, abort) behaved as described. Repeat this when the ingester's job schema, glob dialect or run options change.

### RAG ingest jobs

The pages (`/ingest/jobs`, `/ingest/jobs/{id}`, `/ingest/new`, `/ingest/jobs/{id}/edit`, `/ingest/runs`, `/ingest/runs/{id}`, `/ingest/secrets`, `/ingest/orphans`, `/ingest/catalog`) and their API (`/api/v1/rag/ingest/...`, `routers/api_ingest_jobs.py`) manage the ingester's catalog. `core/ingest/jobs_service.py` does what the routes ask and is the only thing they call; `tests/test_ingest_*.py`, `tests/test_api_ingest_jobs.py` and `tests/test_ingester_contract.py` pin it, against `tests/fake_ingest.py`.

Consequences worth remembering when touching this area:

- **`jobs.yaml` has several writers and none of them owns it.** The ingester's own interface, this editor, the Embedding page (`mgr-` jobs) and a person with a text editor all write it. Every manager write goes through `JobsFileRepository` (`update(mutate, rules)`, `put_job`, `remove_job`, `set_enabled`, `set_defaults`, `replace_raw`, `restore`) and the compare-and-swap of `vectordb/catalog_io.py`; the change is computed again from the bytes that are there when the swap fails. An editor's save carries the `entry_etag` of the entry it opened, so a change to another job is no conflict and one to this job is a 409.
- **The mirror is not the ingester.** `jobspec.JobSpec` copies the ingester's job schema, defaults and cross-job rules so the editor can check a job without the ingester and the manager can say what a job inherits. Where the two differ, the ingester wins at runtime, which is why a write is never trusted: `catalog_load.reload_and_verify` makes the ingester reload and compares what it serves with what was written (`loaded_as_written`, `subset_matches`). `tests/test_ingester_contract.py` compares field sets, defaults, the rules that refuse a job and the secret store's format against the ingester's own modules; it needs `QDRANT_INGEST_SRC` (the ingester's `src` directory) and is skipped without it. Run it when either schema changes.
- **All or nothing, and what that means for a save.** The ingester keeps its previous catalog when any job is invalid (`applied: false`), and at its start loads the valid subset. So after a write the manager distinguishes "the ingester refuses my job" (the write is taken back) from "it refuses other jobs" (the write stays, and the result says the previous catalog is still served, with `elsewhere` naming the jobs). With the ingester unreachable a job is checked by the manager's rules, saved, and the result says it could not be confirmed. A job that is in `jobs.yaml` is never an orphan, however it is served; a leftover is a state row whose id is in no file.
- **The editor works on the effective job.** `jobspec.effective` merges the catalog defaults over a job and `minimise` writes back only what differs from them, except `source`, `target`, `mode` and `embedding.model`, which are always written: a collection is bound to its model, and a changed default must never move a job to another one. A key the editor does not know stays as it was.
- **Ids are for life.** Every point derives from the job id, the source label and the path, so an id cannot change in an edit (duplicate instead), `mgr-` (the Embedding page's) and `new` are reserved, and re-creating a deleted job with the same id, label and path adopts what is left of it. Pausing is `enabled: false`; the ingester also has a pause of its own (`paused` in its job list), which the list shows and *Resume scheduling* clears.
- **Schedules are in the ingester's dialect, not the backup schedule's.** `schedules.py` is separate from `schedule.py` on purpose: the ingester feeds a cron expression to APScheduler's `CronTrigger.from_crontab`, which counts weekdays from Monday (0) and ANDs day-of-month with weekday. The manager always writes weekday names and reads numbers the way the ingester does, so what the editor shows and what runs agree; the "next runs" come from the ingester. An interval schedule keeps its timer across a catalog reload (an ingester change).
- **Credentials.** A value goes in `ai/rag/catalog/secrets.yaml` (`version: 1`, `secrets: [{name, value}]`, `value` = `enc:1:` + Fernet token keyed from `QI_CONNECTIONS_SECRET`, the same cipher as the connection store) and is write-only: no view, log line, audit entry or response carries it. The writer is compare-and-swap, keeps no `.bak` (a backup copy of a secret would be one more place for it), and the ingester reads the file when a value is needed, so a new credential works without a restart. The ingester answers `${env:QI_SECRET_X}` from its process environment first and the file second, and the manager follows that: a name defined in `ai/rag/.env` is never shadowed, and the manager knows the names from that file only, not from an environment it cannot see. A credential a job refers to cannot be deleted. This is the same level of protection as `connections.yaml`: the key is in the `.env` next to it.
- **What the ingester can do decides what the page offers.** `/health.features` lists `run_progress`, `documents`, `validate`, `delete_runs` and `secret_store`. An ingester without one keeps working: no progress, a files tab that explains, the manager's own checks, runs that stay when a job is deleted, no stored credentials. 0.3.0 has none of them.
- **A run is the ingester's, a page is a view of it.** The list polls only while a run works (the partial renders its own `hx-trigger` conditionally), the runs and the files come from the ingester's REST API, and the ingester's records survive a restart of the manager. An abort is cooperative and takes effect between two files.
- **A job's runs go with it, and can go without it.** The ingester prunes a job's history only after one of the job's own runs, so the runs of a deleted job would stay for good; `JobsService.delete` therefore also calls `DELETE /v1/jobs/{id}/runs` (`IngestClient.delete_runs`), and only after the reload shows the ingester no longer serves the job (while another job is invalid it keeps its previous catalog and the job with it, and a run could still write to the history). Each step reports into the note and none stops the others. `delete_runs` deletes a job's history for whole days, both included, converted to `[start, end)` instants in the ingester's zone (`display.day_bounds`: UTC with an explicit `+00:00`, never `Z`, because the ingester compares the stored text), counts first (`dry_run`), and works for a job that no longer exists. A run that is working is never deleted; the ingester refuses it in the same statement, so a run that starts meanwhile is safe, and the answer counts it. It is `POST .../runs/delete`, not a `DELETE`, because `DELETE /job-runs/{id}` aborts a run. What the job embedded and the ingester's `documents` rows are not touched; the time of the job's last run is, which can make a `run_on_startup: if_missed` job start at the next start of the ingester. The jobs of the Embedding page can be deleted too (the next embedding writes the job again, an upload that still waits keeps its files until the time limit because `reconcile` keeps an upload whose run is unknown); they still cannot be edited or paused.
- **Page scripts.** `htmx` and `alpine` are deferred scripts, so an inline script must not call `htmx` while the page is parsed (the job page starts its first tab after `DOMContentLoaded`; a test pins it). A `<select>` whose options an Alpine `x-for` makes shows its first option whatever the model holds, because `x-model` is applied before the options exist; such selects carry `x-effect="sync($el, ...)"`, and a test checks that they all do. Unit tests with a `TestClient` cannot see either, so look at a page in a browser when you change one. The editor asks before a link leaves it with unsaved changes in its own dialog (`guardLink`, `#leave-editor`); a page cannot replace the browser's prompt for a reload, a closed tab or the back button, so `beforeunload` stays as the fallback.
- **Every change is audited** as `rag.ingest.job.create|update|delete|enable|disable|resume|run`, `rag.ingest.run.abort|delete`, `rag.ingest.orphan.delete`, `rag.ingest.secret.set|delete`, `rag.ingest.defaults.update` or `rag.ingest.catalog.raw`, with counts and ids and never a value.
- **Verified against the real code.** What the editor produces loads with the ingester's `load_catalog` and what the ingester refuses is refused here (the contract test), and the pages were driven in a browser against the real ingester and a throwaway Qdrant: runs with progress and an abort, a dry run, a rebuild, a failed fetch, a stored credential making a job valid without a restart, an invalid job kept out by the ingester and repaired, a leftover adopted by a job of the same id, an edit that raced another, an ingester that is down and one without the new features. The deletion of runs was driven the same way: a period in the dialog (counted, then deleted, the list refreshed), the history of a job that was deleted earlier, a job deleted with its runs, an Embedding page job deleted with its runs, a job that the ingester kept serving because another job was invalid (its runs stayed, the note said why), and the 0.3.0 image without `delete_runs` (no menu item, the dialog says the runs stay). Repeat this when the ingester's REST API or the page scripts change.

Known limits: the ingester does not compare before it replaces a file, so a save of its own interface that began before a manager save and ends after it replaces the manager's change; the editor offers what the ingester's job schema has (the nine source types and their fields; any other rclone option goes into the free-text flags field); a run's history is the ingester's last 200 per job, unless it is deleted sooner, and an ingester without `delete_runs` keeps the runs of a deleted job.

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
# From the repository root: the two checks CI runs from there
yamllint .
uv run --project src ruff check .

# From src/
cd src
uv run mypy .
uv run pytest -q
```

- **Python linting**: ruff with rule sets E, F, I, B, UP, N, RET, SIM, ASYNC. Run it from the
  **repository root**, as CI does (`ruff check .` with the root `ruff.toml`): `tests/` is a
  sibling of `src/`, so `uv run ruff check .` from inside `src/` lints `src/` only and passes
  over a line in a test that CI then rejects. The `--project src` above only borrows the
  environment `uv sync` made there.
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
| `QDRANT_URL` | Where the manager itself reaches the integrated Qdrant (default: `http://qdrant:6333`, which resolves on the network the manager shares with it). It replaces the stored address of a connection to the integrated Qdrant when connecting and is never written to the connection store |
| `QDRANT_INGEST_URL` | Where the manager reaches the ingester's REST API (default: `http://qdrant-ingest:8300`, on the same network). Not the public URL of its web interface |
| `INGEST_UPLOAD_TTL_HOURS` | How long an upload that was not embedded successfully stays before it is deleted (default: 24) |
| `INGEST_MAX_UPLOAD_MB` / `INGEST_MAX_BATCH_MB` | Size limits of one uploaded file (default: 200) and of one upload (default: 2048) |

The Connections and Collections pages read the rest of their settings from the RAG
module's `.env` (`$PAPAIA_CONFIG_DIR/ai/rag/.env`) at request time, not from the
manager's environment: `QDRANT_JWT_SECRET` (the stack's api-key, which the default
connection stores) and `QI_CONNECTIONS_SECRET` (it derives the key that encrypts the
api-keys in the connection store), `EMBEDDING_META_COLLECTION` or
`QI_EMBED_META_COLLECTION`, `RBAC_ACL_COLLECTION` or `QI_RBAC_ACL_COLLECTION`, and
`QI_OIDC_OPERATOR_ROLE`, `QI_API_TOKEN` (the ingester's REST token, for the Embedding and Ingest pages), `QI_LOCAL_MOUNT` (to find the documents folder), `QI_TIMEZONE` (the zone schedules are read in when neither a job nor the catalog names one) and the names of `QI_SECRET_*` (the credentials a job can refer to). A missing key means the services' own default
(`_collection_meta`, `_rbac_acl`, `qdrant-ingest-operator`); a missing secret means
no default connection is created and no api-key can be stored.

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
