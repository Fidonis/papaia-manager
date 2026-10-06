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

**Settings and branding.** Administrators open **Settings** to change the
name and second line shown at the top of the sidebar (defaults: "papAIa
manager" / "by Fidonis") and to upload their own logo (PNG, JPEG, WebP or SVG,
up to 512 KB; it is scaled to fit the header). The values are stored in
`$PAPAIA_CONFIG_DIR/manager/settings.yaml`, one section per topic, and the logo
in `$PAPAIA_CONFIG_DIR/manager/branding/`, so both are part of a backup.
Without a settings file nothing changes. A second card, **Host monitoring**, sets
how often the Host page re-measures the machine (10 seconds to 60 minutes,
default 60 seconds); it applies to every administrator.

**Dashboard and access tiers.** Two Keycloak realm roles gate the UI.
`MANAGER_ADMIN_ROLE` (default `manager-admin`) reaches every surface;
`MANAGER_USER_ROLE` (default `user`) reaches the dashboard only. Accounts
holding neither role are rejected at login. The dashboard at `/` is a tile
overview of the deployed applications, held in
`$PAPAIA_CONFIG_DIR/manager/tiles.yaml` and seeded with the applications the
stack ships today on first run. Administrators edit it in place: **Edit
dashboard** turns the page into an editor for groups and tiles, with drag and
drop reordering, a live preview of each tile, and one **Save changes** that
writes the whole file. Hand editing the file keeps working, but a save from the
UI rewrites the document, so comments are not preserved; a concurrent change on
the host is detected and refused rather than overwritten. The raw file is also
editable from the editor's overflow menu. `{{KEY}}` placeholders in tile links
resolve against the core `.env`, and each tile's `visibility: all | admin` is
filtered server-side, so an admin-only tile is absent from a regular user's
response rather than hidden by CSS. Authorization is enforced by the route
dependencies, so the JSON API is restricted exactly like the pages.

**RAG system.** When the core runs its optional RAG system (the `rag` profile
in `COMPOSE_PROFILES` of the core `.env`), administrators get two more things.
The dashboard shows a **RAG** group with a *Qdrant* tile (the vector database's
dashboard), and the sidebar gets a **RAG** category between *Extensions* and
*System* with the pages described below: *Connections*, *Collections*,
*Embedding*, *Ingest Jobs* and *Ingest Runs*. The ingest service's own web
interface has no tile and no menu entry: what it offered is on those pages.
The link of the tile is built from `QDRANT_PUBLIC_URL` in the core `.env`. The
profile decides, not that key: the core keeps it while the system is switched
off, so a core without the profile, such as 1.4.0, shows nothing new. The tile
is administrator-only, because the Qdrant dashboard bypasses the MCP server's
role checks. It is computed when the dashboard is rendered and never written to
`tiles.yaml`, so it appears on an existing deployment too, and the tile editor
neither lists nor saves it; the price is that it cannot be reordered or removed
there. A tile of your own with the same name or link wins, and a group you call
*RAG* receives it.

**Connections.** An admin-only page at `/connections`, first under *RAG* in the
sidebar and only while the `rag` profile is active, for the vector databases the
RAG system works with. A connection is a name, a type and the values that type
needs; Qdrant, the only type so far, takes an address and an optional api-key.

Connections are kept in the ingester's own store, so the ingest service uses
them without any change and its web interface keeps editing the same file:
`ai/rag/catalog/connections.yaml` in the config directory, `version: 1`, entries
of `name`, `url` and an `api_key` stored as `enc:1:` plus a Fernet token derived
from `QI_CONNECTIONS_SECRET` in `ai/rag/.env`. Nothing but those three keys is
ever written, because the ingester rejects an entry with any other key. The
connection named **default** is the integrated Qdrant (`http://qdrant:6333`, with
the stack's api-key `QDRANT_JWT_SECRET`). It is created when the manager starts
and on first use, never overwritten, and marked as the default. Its address and
key can be edited and an action resets it to the integrated Qdrant, but it cannot
be renamed or deleted. The other connections are created, edited, tested and
deleted on the page.

The api-key is never shown or returned, only whether one is stored; leaving it
empty keeps the stored one. A stored key is only ever sent to the address it was
stored with: testing a stored connection ignores an address that is sent along,
and changing the address needs the key again, or its removal. A name cannot be
changed once the connection exists, because ingest jobs refer to a connection by
it. A connection that a job in `jobs.yaml` writes to cannot be deleted, and
changing its address asks for a confirmation that names the jobs. The ingester
picks a change up within about 30 seconds. `QDRANT_URL` is where the manager
itself reaches the integrated Qdrant (default `http://qdrant:6333`); it replaces
the stored address of a connection to the integrated Qdrant when connecting, and
is never written to the file.

**Collections.** An admin-only page at `/collections`, second under *RAG* in the
sidebar and only while the `rag` profile is active, for the Qdrant collections of
the RAG system and the Keycloak roles that may use them. It works on one
connection at a time, chosen above the list and defaulting to the default
connection; roles can be edited on every connection, and on one that does not
point at the integrated Qdrant a note says that the stack's MCP server does not
enforce them there. It lists the collections
with their points, vector size, the embedding model recorded in the meta
collection and their roles. It creates a collection the way the ingester does
(name, vector size, an optional embedding model, initial roles) and deletes one
after its name is typed, together with its meta record and its roles. Each
collection takes any number of role names with the access level *read* or
*read + write*; the names are not checked against Keycloak. The roles are stored
exactly as `qdrant-mcp-rbac` stores them, one point per role and collection in
its ACL collection (`_rbac_acl`), so the MCP server enforces them without any
change; a change reaches it within about a minute, the length of its access
cache. The role `qdrant-ingest-operator` always has access to every collection:
it is shown locked on each one, and a global *manage* grant for it is kept in the
ACL collection (written with every change, and from a banner when it is missing).
The address and the api-key come from the selected connection. The manager reads
the two collection names (`EMBEDDING_META_COLLECTION` or
`QI_EMBED_META_COLLECTION`, and `RBAC_ACL_COLLECTION` or
`QI_RBAC_ACL_COLLECTION`) and the operator role (`QI_OIDC_OPERATOR_ROLE`) from
`ai/rag/.env` in the config directory, with the services' own defaults when a key
is missing; they are the same on every connection. While the connection store has
no default connection, or cannot be read, the default connection is answered from
`QDRANT_URL` and `QDRANT_JWT_SECRET`, so this page does not depend on the file.
The core does not pass these names on to the MCP server yet, so a changed name
takes effect for the ingester only; changing them is not supported before it
does. When Qdrant cannot be reached or refuses the key, the page says why instead
of showing a list.

**Embedding.** An admin-only page at `/embedding`, third under *RAG* and only
while the `rag` profile is active, that puts files into a collection through the
ingester, as a run in the background with its status on the page. A collection
has an *Embed files* link on the Collections page. Files come from two places.
An **upload** (files or a whole folder, from the browser) is kept in a staging
folder of its own, `ai/rag/documents/uploads/<owner>/<upload>/` (`/data/local/…`
in the ingester), so the uploads of different administrators and of different
occasions stay apart, and it is **deleted after a run that succeeded without a
failed document**, because it may be confidential. An upload that is left (a
failed or stopped run, a forgotten upload) is kept for a retry and deleted after
`INGEST_UPLOAD_TTL_HOURS` (24 by default); it can also be discarded by hand. The
**folder** tab picks files and whole folders that are already in the ingester's
documents folder; those belong to whoever put them there and are never deleted.
There are two modes. *Add and update* adds new files, replaces the chunks of a
file whose path is already in the collection and whose content changed, skips an
unchanged file and never deletes anything: a document is identified by its path
inside the upload (or the folder), so uploading `handbook/a.pdf` again updates
it. It needs an ingester with the `delete_vanished` run option and says so when
the ingester is older. *Replace the collection* drops everything in it and fills
it from the selection (its roles and its model record stay; it is also how the
embedding model is changed) and asks for the collection name to be typed. The
model is taken from the collection's meta record, or named when there is none.
One run works on a collection at a time, and none starts while a restore, an
upgrade or a stack action runs. The status is the ingester's own, so it survives
a restart of the manager; while a run works the page shows the chunks written so
far (counted in Qdrant), and its counts and messages when it ends.

The ingester can only run what `jobs.yaml` declares and has no API to create a
job or to take a file. The manager therefore keeps two jobs per connection and
collection in `ai/rag/catalog/jobs.yaml`, one for uploads and one for the folder,
with the id prefix `mgr-` (every other job is left exactly as it is), written
compare-and-swap like the connection store. It checks the rules the ingester
checks across jobs first, because the ingester refuses a catalog with one invalid
job as a whole, makes the ingester reload, checks that it serves what was
written, and takes its write back otherwise. Comments in `jobs.yaml` are lost on a
write (as when the ingester's own form saves a job); the previous file is kept as
`jobs.yaml.bak`. The ingester is reached on the stack's network at
`QDRANT_INGEST_URL` (default `http://qdrant-ingest:8300`) with the static
`QI_API_TOKEN` from `ai/rag/.env`. The documents folder must be where the core
puts it (`ai/rag/documents`) or inside the configuration or workspace directory:
a `QI_LOCAL_MOUNT` anywhere else is a folder the manager cannot see, and the page
says so. The limits are `INGEST_MAX_UPLOAD_MB` per file (200) and
`INGEST_MAX_BATCH_MB` per upload (2048). A backup of the configuration directory
contains an upload that is still staged while it runs.

**Ingest jobs.** Admin-only pages under `/ingest/`, fourth and fifth under *RAG*
(*Ingest Jobs* and *Ingest Runs*) and only while the `rag` profile is active, that
take over from the ingester's own web interface. A **job** says what the ingester
reads (a folder in the documents folder, S3, WebDAV, SFTP, SMB, FTP, Google Drive,
Azure Blob Storage or a web directory), which collection on which connection the
result goes to, what a run does with files that are new, changed or gone, and when
it runs. Jobs are kept in `ai/rag/catalog/jobs.yaml`, the file the ingester reads,
and the page is one more editor of it. Five tabs run across the top: *Jobs*,
*Runs*, *Credentials*, *Leftovers* and *Catalog file*.

The **job list** shows each job with its state (active, disabled, not loaded with
the ingester's reason, or running its previous version while the ingester keeps an
older catalog because another job is invalid), its source, collection and number of
files, the schedule in words with the next run, and the last run. It filters by
state, searches by name, source or collection, and polls only while a run works.
A job has an overview (what it does, in sentences, and how the last run went), its
runs, its **files** (what happened to each one: embedded, no text found, too large,
not a supported type, could not be read or embedded, with a search and a filter by
run) and its configuration (the job as written, with credentials as references and
the values it inherits from the catalog defaults). The **editor** has seven
sections (name, source, target, which files, what a run does, when, processing),
checks the job with the ingester's own rules while you type, shows the next runs of
a schedule, browses the documents folder, and has *Save and dry run*. A schedule is
chosen in plain terms (only when started, every N minutes, hours or days, hourly,
daily, weekly, monthly, or a cron expression); the manager writes weekdays as names
because the ingester counts them from Monday, and only what differs from the
catalog defaults ends up in the file. A job's id cannot change, since every point it
writes derives from it; *Duplicate* starts a new one from it. *Pause* is
`enabled: false`. The two jobs per collection that the Embedding page keeps (id
prefix `mgr-`) are listed and say where they come from, but are not edited here.

**Runs.** *Run* opens a dialog: a dry run (fetch, read and plan, write nothing; it is
preselected for a job that never ran), and the mode, either as the job is set up or
add only, keep in sync, rebuild the job's content (asks for confirmation) or replace
the whole collection (asks for the collection name), with a few more options such as
not fetching again. A run shows its phase (fetching the files, looking at them,
embedding them, removing what is gone), how many files are done, the file in hand, and *Abort*, which takes effect
between two files. When it ends it shows its counts, the ingester's messages
(problems first), the output of a failed fetch and a way forward for each way a run
can stop. The *Ingest runs* page lists the runs of all jobs, filtered by job and
state. Both poll only while a run works.

**Credentials.** A source refers to a credential by name, `${env:QI_SECRET_<NAME>}`
in `jobs.yaml`. The ingester answers it from its environment (`QI_SECRET_*` in
`ai/rag/.env`, which needs a restart) or from `ai/rag/catalog/secrets.yaml`, which
this page writes and the ingester reads when it needs a value, so a credential
stored here works at once. The file holds `version: 1` and entries of `name` and
`value`, each value `enc:1:` plus a Fernet token derived from `QI_CONNECTIONS_SECRET`
exactly as the connection store does it, and is written compare-and-swap like the
other two files. A value is never shown or returned: it can be replaced, not read.
A name from the environment is never shadowed by a stored one, and a credential that
a job still refers to cannot be deleted. This keeps a credential out of casual sight
(a copy of the file, a screenshot, a log); it does not protect it from someone who
can read the file and the `.env` next to it, and a backup contains both. The page
says so, and says when the ingester is too old to read the file.

**Leftovers and the catalog file.** What a deleted or renamed job embedded stays in
its collection with the ingester's records of it; *Leftovers* lists those and
removes them on request (the id of the job is typed to confirm). Deleting a job
offers to remove its content in the same step. *Catalog file* has the defaults every
job inherits (the embedding model, chunking, filters, the time zone of schedules, the
removal limit) as a form that checks every job against the new values before it
writes, and `jobs.yaml` as text, edited as typed with comments kept, checked before
it is saved.

Every write goes through the ingester's own checks and a compare-and-swap writer,
and nothing is trusted because it was written: the manager makes the ingester reload
and compares what it serves with what was written. A job is saved with the version
of its entry that the editor opened (the `etag`), so an edit made meanwhile by
someone else is refused instead of overwritten, while a change to another job is no
conflict. If the ingester refuses the manager's own job, the write is taken back; if
it refuses only other jobs, the write stays and a warning says that the ingester
keeps its previous catalog until those are fixed. With the ingester not reachable a
job is checked by the manager's own rules and saved, and the page says it could not
be confirmed. The previous file is kept as `jobs.yaml.bak`. Comments in `jobs.yaml`
are lost when the editor or the defaults form saves; editing the file as text keeps them.
Every change is audited (`rag.ingest.job.*`, `rag.ingest.run.abort`,
`rag.ingest.orphan.delete`, `rag.ingest.secret.*`, `rag.ingest.defaults.update`,
`rag.ingest.catalog.raw`); a credential's value is never part of an entry.

These pages use what the ingester reports it can do (`features` in its `/health`):
`run_progress` (phase and progress of a run, dry runs), `documents` (the files of a
job), `validate` (its rules for a job that is not saved yet) and `secret_store` (the
credentials file). An older ingester keeps working with each part missing and an
explanation where it would be: no progress, a files tab that says so, the manager's
own checks, and no stored credentials. 0.3.0 of the ingester has none of them.

**Services.** An admin-only page at `/services` showing what this deployment is
configured to run and how much of it is up. Containers are read from `docker ps`
and grouped by the `de.fidonis.module` label the Compose files put on every
service, so each module lists its own containers with their role, uptime,
healthcheck result and published ports; the module in the worst state sorts to
the top. A container without a healthcheck counts as healthy while it runs, and
a one-shot container that has finished its work is reported as completed rather
than dragging its module down — recognised by its restart policy, so a service
that was shut down still reads as stopped even though it exited just as cleanly.

Live containers alone cannot say what is *missing*, so the page reads the
declared state next to them. For the core stack that is the Compose fragments
listed in `papaia/src/docker-compose.yml`, filtered by the profiles enabled in
`COMPOSE_PROFILES`; for add-ons it is the active entries of `deployment.yaml`
and their own Compose files. A declared service with no container renders as
*not deployed* instead of being absent, which is what separates "LocalAI is
configured but was never started" from "this deployment has no LocalAI". A
torn-down stack therefore reports every module rather than an empty page —
`papaia-ctl down` removes containers, it does not stop them. An unreachable
Docker socket still reports nothing at all: not knowing is not the same as
knowing it is gone.

Add-ons appear in their own section below the core stack. They run in a separate
Compose project each, but use the same labels, so they group into modules exactly
like core services do. Bear in mind that few add-ons define healthchecks, so a
green add-on module says only that its containers are running.

**Service groups.** The page is also where the stack is started and stopped. The
unit is the Compose profile — a *service group* — because that is the only
granularity `papaia-ctl` accepts; there is no per-container verb. Each module
header carries its profile and a checkbox, and ticking one module ticks every
module the same profile brings up: `librechat-websearch` alone covers Firecrawl,
SearXNG, Jina and the Firecrawl MCP server, and stopping one without the others
is not something Compose can do. Several groups can be selected and started,
stopped or restarted in one run. Restart is a stop followed by a start, so it
picks up a changed configuration.

Stopping leaves the containers in place. Every confirmation dialog that stops
something — a stop, and the stop half of a restart — offers to remove them as
well (`--clean-up`, i.e. `docker compose down` instead of `docker compose stop`).
Volumes and `$PAPAIA_CONFIG_DIR` are untouched in either case. After a stop with
it the modules read as *not deployed* rather than *stopped*, because a removed
container is indistinguishable from one that was never created; after a restart
with it they are simply built again, which is what makes that a full recreate
rather than a stop and start. A start has no such flag and the API rejects the
field rather than ignoring it.

The whole stack can be started, stopped and restarted from the header menu, with
the same choice. That one takes the manager down with it, so it runs in a
separate container that outlives this one and reports its result back once the
page is reachable again — the same mechanism restore uses. Add-ons are left
running by a stack action, and are started, stopped and restarted individually
from their own rows. There is deliberately no action across all add-ons.

The profile serving this panel is the one group that cannot be selected. Stopping
it would remove the container handling the request, which could then never report
whether it worked.

The same data drives a status row in the sidebar of every page, visible to every
authenticated account regardless of role. Its popover keeps three rows apart: the
core stack, the add-ons and the host. Each carries counts only, so a user without
the admin role learns that something is unhealthy, but not which service, disk or
certificate. Core and add-ons stay separate so that a failing add-on out of a
customer catalogue does not report the stack itself as broken. Administrators get
links to the matching pages.

**Host.** An admin-only page at `/host` showing the state of the machine under the
deployment: memory (with swap), CPU load per core, each GPU's VRAM, utilisation and
temperature, whether the system clock is synchronised, free space on the config
and backup directories, and the days left on every certificate (the bundled ones
and Let's Encrypt). The manager measures nothing itself. It runs the core's
`papaia-ctl doctor`, limited to its `memory`, `cpu`, `gpu`, `time_sync`,
`disk_space` and `certs` checks, and shows the core's verdicts, so the page and a
shell on the host cannot disagree about a threshold. That needs a core that has
`doctor` (1.4.0 or newer); on an older one the page says so and the status row
carries on without a Host row, and a 1.4.0 core that predates a resource check
simply has no row for it. A resource check the core skips is listed as not
measurable instead of being left out: with a core that cannot read them inside a
container, that is the GPU and the clock. The free space of the Docker data root
has no row while the manager cannot see it (the container mounts the config and
backup directories, not `/var/lib/docker`); "Docker usage" says what Docker holds
instead. A reading is
cached for the refresh interval and shared between everyone who has a page open;
the page shows the interval and links to Settings, where it is set, and its
Re-check button asks for a fresh reading. What Docker's data takes (images,
containers, volumes and build cache, with what the daemon calls reclaimable) is
listed under "Docker usage" when the core has the `docker_usage` check. The
daemon has to size every volume to answer, so it is measured on its own, every
tenth interval and no sooner than every 5 minutes, and a slow or failing
measurement leaves the rest of the page as it is. The Host entry in the sidebar carries a
dot while a check warns or has failed.

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

**Backup schedule.** The Backup page can start backups by itself. The schedule is
one file, `$PAPAIA_CONFIG_DIR/manager/schedule.yaml`, edited in a dialog on the
page: every day, on chosen days, every few hours, or a cron expression, in a
timezone (the container's `TZ` if it names one, otherwise UTC), with a preview of
the next three runs. A scheduler inside the manager runs it, so it needs nothing
from the host — no systemd, no cron — and behaves the same on any operating system
that can run the stack. Expressions are standard cron (`1` is Monday), and runs
closer together than an hour are refused.

A scheduled run takes the same path as the *Create backup* button: the same job,
log and audit entry, as the user `scheduler`. While a restore, an upgrade or
another job is running, or the backup directory is not mounted, it is held back
and tried again every ten minutes for up to an hour. A strip above the restore
points shows the age of the newest successful backup and the next run, and turns to
a warning once that backup is older than one and a half times the longest gap
between two runs. The manager restarts on every upgrade, restore and host reboot
and can miss a run that way, so after a start one run is made up if the newest
successful backup is already older than that (the schedule can opt out).

The retention period is configurable and must be at least twice the longest gap
between runs. `papaia-ctl backup` prunes after every run, a failed one included,
and does not keep a last usable restore point, so a scheduled run passes the
retention only while the newest successful restore point is recent; otherwise it
backs up without pruning and the job log says why. Two things to know: a restore
replaces `$PAPAIA_CONFIG_DIR`, so the schedule returns to what it was in the
restored snapshot, and the scheduler assumes one manager process (the image runs
a single uvicorn worker; do not add `--workers`).

Restore is the exception to "everything is a job". `papaia-ctl restore` tears the
core stack down before unpacking archives, and the manager is a service of that
same stack — a restore run in-process would be killed by its own teardown step.
It therefore runs in a **detached container** cloned from the manager's own
container spec (same image, binds, user and groups, so path parity holds by
construction). Docker keeps the state: the runner is started without `--rm`, so
its status and log stay readable by the recreated manager container once the
stack is back up. The page loses its connection while that happens, reconnects on
its own, and reports the outcome.

**Core upgrades.** An admin-only `/upgrade` page that moves the whole deployment
to a newer papAIa release. It is two halves. The check resolves the target tag,
runs the add-on compatibility gate against a temporary worktree of it, and lists
the release migrations that would run — all of it read-only, and all of it
answered by `papaia-ctl`'s own machine-readable sub-commands so the page and a
shell on the host cannot disagree. Everything that would make `papaia-ctl
upgrade` refuse — a dirty checkout, an incompatible add-on, an unreachable
backup directory — is shown as a readiness row before the operator commits,
rather than discovered after the stack is already down.

The upgrade itself runs in a detached container, like restore and for a stronger
reason: it removes and recreates every container, this panel included, and
`papaia-manager` is upgraded along with the stack — the page that reports the
outcome is served by a different build than the one that started it. Progress is
shown as the phases papaia-ctl announces, over the raw log, with the outage
handled as a reconnect. papaia-ctl has no automatic rollback by design, so a
failed upgrade renders its recovery commands verbatim and links the restore point
taken beforehand.

Upgrades leave the previous versions of the stack's Docker images behind. The
upgrade dialog offers to remove them once the upgrade has succeeded (selected by
default), and the page lists whatever is still there afterwards — for the core
stack and the installed add-ons — so it can be removed later. An image counts as
outdated only if no container uses it, the installed release does not declare it,
and it belongs to a repository the stack declares; unrelated images are never
touched, and nothing is offered while the declared images cannot be resolved.

**Updates.** Refresh the catalog, diff the candidate manifest's
`.env.example` against the installed bundle (new `CHANGE_ME` keys prompt for
values before the job starts), stop the add-on, re-materialize the snapshot
at the new commit, reinstall, and start again.

**Audit log.** An admin-only `/audit` page lists every entry from the audit log,
filtered by user, action, result, a target substring and a date range, newest
first and paginated, with the filter facets drawn from the log itself. The same
filters drive `GET /api/v1/audit/export` (CSV or JSONL). `POST /api/v1/audit/prune`
permanently removes entries older than an operator-chosen cutoff date behind a
dry-run preview; the prune itself is recorded as its own audit entry.

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

GET    /api/v1/rag/connections             # connections, their types and fields; never a key
POST   /api/v1/rag/connections             # {name, type?, fields: {url}, api_key?}
POST   /api/v1/rag/connections/test        # {name?} or {fields, api_key?}; → {ok, detail, collections}
PUT    /api/v1/rag/connections/{name}      # {fields, api_key?, clear_api_key?, etag, confirm_jobs?}
DELETE /api/v1/rag/connections/{name}?etag=
POST   /api/v1/rag/connections/default/reset  # the integrated Qdrant, with the stack's api-key

# The collection routes take ?connection=<name> (default: "default")
POST   /api/v1/rag/collections             # {name, vector_size, embedding_model?, roles?: [{role, access}]}
PUT    /api/v1/rag/collections/{name}/roles   # {roles: [{role, access: "r"|"rw"}]}
DELETE /api/v1/rag/collections/{name}      # also removes its meta record and roles
POST   /api/v1/rag/collections/operator-grant  # → {written}

GET    /api/v1/rag/ingest/status           # ingester usable?, supports add?, documents folder
GET    /api/v1/rag/ingest/uploads          # the staged uploads
POST   /api/v1/rag/ingest/uploads          # {name?} a new, empty upload → 201
POST   /api/v1/rag/ingest/uploads/{id}/files  # multipart: file, path? (relative) → 201 {path, bytes, replaced}
DELETE /api/v1/rag/ingest/uploads/{id}     # discard it and delete its files
GET    /api/v1/rag/ingest/tree             # ?source=folder|upload, upload?, path? one level of a tree
# The run routes take ?connection=<name> (default: "default")
POST   /api/v1/rag/ingest/runs             # {collection, mode: "add"|"replace", source: {kind, batch?, paths}, model?, confirm_replace?, confirm_other_jobs?} → 202 {run_id, job_id, files, bytes}
GET    /api/v1/rag/ingest/runs             # ?collection= the latest runs of a collection
GET    /api/v1/rag/ingest/runs/{id}        # ?collection= state, counts, messages, chunks so far
DELETE /api/v1/rag/ingest/runs/{id}        # abort (the ingester stops between documents)

# Ingest jobs (jobs.yaml); an editor's write carries the etag of the entry it opened
GET    /api/v1/rag/ingest/ingester         # reachable?, version, features, dependencies, the catalog's state
GET    /api/v1/rag/ingest/form-spec        # what the editor needs: source types, fields, presets
GET    /api/v1/rag/ingest/collections      # ?connection= the collections a job can write to
GET    /api/v1/rag/ingest/jobs             # every job with its state, schedule and last run
POST   /api/v1/rag/ingest/jobs/validate    # {job, create?, original_id?} → {issues}; writes nothing
POST   /api/v1/rag/ingest/jobs             # {job} → 201
GET    /api/v1/rag/ingest/jobs/{id}/editor # the job as the editor holds it, with its etag
PUT    /api/v1/rag/ingest/jobs/{id}        # {job, etag?, original_id}
DELETE /api/v1/rag/ingest/jobs/{id}        # ?etag=&purge= also remove what it embedded
POST   /api/v1/rag/ingest/jobs/{id}/enable   # and /disable (enabled: false) and /resume (scheduling)
POST   /api/v1/rag/ingest/jobs/{id}/run    # {mode?, full_scope?, dry_run?, skip_sync?, delete_vanished?, confirm_*} → 202 {run_id}
GET    /api/v1/rag/ingest/jobs/{id}/files  # ?status=&q=&run_id=&order=&limit=&offset= what happened to each file
GET    /api/v1/rag/ingest/jobs/{id}/preview  # the files the saved filters match
GET    /api/v1/rag/ingest/jobs/{id}/runs   # the runs of one job
GET    /api/v1/rag/ingest/job-runs         # ?job_id=&status=&since=&limit= the runs of all jobs
GET    /api/v1/rag/ingest/job-runs/{id}    # one run: counts, phase, current file, messages
DELETE /api/v1/rag/ingest/job-runs/{id}    # abort
POST   /api/v1/rag/ingest/schedule/preview # {schedule} → description, cron and the next runs
POST   /api/v1/rag/ingest/reload           # make the ingester read jobs.yaml now
GET    /api/v1/rag/ingest/orphans          # what deleted jobs left behind
DELETE /api/v1/rag/ingest/orphans/{id}     # remove it (points and records)
GET    /api/v1/rag/ingest/secrets          # credential names, where each is kept and used; never a value
PUT    /api/v1/rag/ingest/secrets/{name}   # {value} store it encrypted
DELETE /api/v1/rag/ingest/secrets/{name}   # refused while a job uses it
GET    /api/v1/rag/ingest/catalog/raw      # jobs.yaml as text with its revision
POST   /api/v1/rag/ingest/catalog/validate # {text} → {issues}
PUT    /api/v1/rag/ingest/catalog/raw      # {text, revision}
GET    /api/v1/rag/ingest/catalog/defaults # the defaults every job inherits
PUT    /api/v1/rag/ingest/catalog/defaults # {defaults}

GET  /api/v1/addons
GET  /api/v1/addons/{name}
GET  /api/v1/addons/{name}/env-form
POST /api/v1/addons/{name}/install         # → 202
POST /api/v1/addons/{name}/start           # → 202
POST /api/v1/addons/{name}/stop            # {clean_up?} → 202
POST /api/v1/addons/{name}/restart         # {clean_up?} stop then start → 202
POST /api/v1/addons/{name}/remove          # → 202
POST /api/v1/addons/{name}/uninstall       # → 202
POST /api/v1/addons/{name}/update          # → 202
POST /api/v1/addons/{name}/save-config     # → 202
POST /api/v1/addons/{name}/check           # synchronous compatibility check

GET /api/v1/jobs
GET /api/v1/jobs/{id}
GET /api/v1/jobs/{id}/log

GET    /api/v1/audit                       # ?user,action,result,target,since,before,limit,offset
GET    /api/v1/audit/export                # {format=csv|jsonl} + same filters, streamed
POST   /api/v1/audit/prune                 # {before, dry_run?} → removed/kept counts

GET    /api/v1/maintenance/backup-dir
GET    /api/v1/maintenance/restore-points
GET    /api/v1/maintenance/restore-points/{id}
POST   /api/v1/maintenance/restore-points/delete  # {ids} → 202 {job_id}
POST   /api/v1/maintenance/backup                 # {retention_days?} → 202 {job_id}
GET    /api/v1/maintenance/schedule               # the schedule, next run, last backup, overdue
PUT    /api/v1/maintenance/schedule               # {cron, timezone?, retention_days?, enabled?, run_on_startup?}
DELETE /api/v1/maintenance/schedule               # remove it (also clears an unreadable file)
POST   /api/v1/maintenance/restore                # {restore_point, restart_clean?} → 202
GET    /api/v1/maintenance/restore/status
DELETE /api/v1/maintenance/restore                # acknowledge a finished restore

GET    /api/v1/stack/groups                  # the deployment's service groups
POST   /api/v1/stack/groups/start            # {groups} → 202 {job_id}
POST   /api/v1/stack/groups/stop             # {groups, clean_up?} → 202
POST   /api/v1/stack/groups/restart          # {groups, clean_up?} → 202
POST   /api/v1/stack/start                   # whole stack, detached runner
POST   /api/v1/stack/stop                    # {clean_up?}, detached runner
POST   /api/v1/stack/restart                 # {clean_up?}, detached runner
GET    /api/v1/stack/runner                  # its status and log
POST   /api/v1/stack/runner/clear            # acknowledge a finished action

GET    /api/v1/upgrade/status                # version, checkout and backup state
GET    /api/v1/upgrade/check                 # the last check, without running one
POST   /api/v1/upgrade/check                 # {version?} fetch tags and evaluate
POST   /api/v1/upgrade                       # {version, force?, no_backup?, prune_images?} → 202
GET    /api/v1/upgrade/runner                # its status and log
POST   /api/v1/upgrade/runner/clear          # acknowledge a finished upgrade
GET    /api/v1/upgrade/images                # outdated Docker images of the stack and add-ons
POST   /api/v1/upgrade/images/prune          # {images?} remove them (all, or the named ids)

GET    /api/v1/settings                      # revision, branding, host monitoring, effective branding
PUT    /api/v1/settings/branding             # {revision, name?, tagline?}
POST   /api/v1/settings/branding/logo        # multipart file: PNG, JPEG, WebP or SVG, up to 512 KB
DELETE /api/v1/settings/branding/logo        # remove the logo
POST   /api/v1/settings/branding/reset       # {revision} back to the defaults, logo removed
PUT    /api/v1/settings/host                 # {revision, refresh_seconds} 10 to 3600
GET    /brand/logo                           # the stored logo, for any signed-in user
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
│       │                   # services (container status), inventory (declared state),
│       │                   # backups (restore-point catalogue), runner (detached restore),
│       │                   # backup_run + schedule + scheduler (backup schedule),
│       │                   # host_health + docker_usage (host readings from the core's doctor),
│       │                   # rag (optional RAG system: tiles, links, settings) +
│       │                   # qdrant (REST client) + rag_collections (collections and roles),
│       │                   # vectordb/ (connection types and the ingester's connection store),
│       │                   # ingest/ (the ingester's jobs.yaml and secrets.yaml, its REST API,
│       │                   # job editor and schedules, runs, the Embedding page's managed
│       │                   # jobs, jailed browsing, staged uploads, background clean-up),
│       │                   # settings_store (settings.yaml and the logo)
│       ├── routers/        # auth, health, ui, api_catalogs, api_addons, api_jobs,
│       │                   # api_maintenance, api_stack, api_upgrade, api_audit,
│       │                   # api_tiles, api_settings, api_collections, api_connections, api_ingest,
│       │                   # api_ingest_jobs, ui_ingest
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
