# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes are generated automatically by [release-drafter](https://github.com/release-drafter/release-drafter)
based on merged pull requests; this file mirrors the published releases.

## [Unreleased]

<!-- Updated automatically by release-drafter as PRs are merged to `main`. -->

## [0.6.0] - 2026-09-02

### Added
- **Dashboard editor.** `/dashboard` is now its own editor for administrators: groups and
  tiles are created, renamed, reordered, moved between groups and deleted from the page
  instead of by editing `tiles.yaml` over SSH. Edits are staged as a client-side draft and
  one **Save changes** writes the whole document atomically; **Discard** reverts everything.
  The write is a whole-document `PUT` carrying a `revision` hash — an edit made on the host
  while the editor was open is refused with 409 rather than overwritten. Link and `{{KEY}}`
  placeholder validation moved into the tile dialog, which previews the resolved address and
  reports the problem before saving; placeholder resolution stays server-side behind
  `POST /api/v1/tiles/resolve` so no core `.env` value reaches the browser. A raw-YAML editor
  covers bulk edits, and drag-and-drop reordering uses SortableJS as a progressive
  enhancement over the existing menu commands.
- **Selective restore.** The single `Restore` menu entry becomes a three-step flow — choose,
  review impact, confirm. Restore points can be recovered per module ("LibreChat —
  conversations, uploads, search index and embeddings") instead of only as a whole; the
  impact step is computed from Compose profiles (what actually stops), and ticking Keycloak
  pulls the configuration archive in and explains why. A scoped restore runs as an ordinary
  queued job with a live log — `$PAPAIA_CONFIG_DIR` is never replaced — behind the new
  `POST /api/v1/restore/scoped` route and the `restore-scoped` core verb. Against an older
  core the wizard reports the point as restorable only as a whole.
- **Core upgrade.** A new admin-only **Update** page (`/upgrade`) moves the deployment to a
  newer papAIa release from the panel. A read-only check resolves the target tag, runs the
  add-on compatibility gate against a temporary `git worktree`, and lists the release
  migrations that would run — every version decision delegated to the core's own
  `upgrade-resolve` / `upgrade-plan` / `addon-check --json` sub-commands. The upgrade itself
  runs in a detached container (it removes the container serving the request and re-execs
  from the target tree), with `--version` always pinned and readiness rows shown before the
  operator commits. There is no automatic rollback: a failed upgrade renders the recovery
  commands verbatim.

### Fixed
- The manager session is now renewed against Keycloak before the access token expires,
  using a refresh token stored at login, so operators are no longer silently logged out
  after ~5 minutes. HTMX and `/api/` callers receive a bare `401` they can act on while only
  real navigations get a `307` redirect, which now carries a validated `?next=` so the
  callback returns to the originating page. Concurrent pollers share a single token
  round-trip.
- A `tiles.yaml` that cannot be parsed no longer answers `/dashboard` with a 500; the raw
  editor surfaces the parse error instead.

## [0.5.0] - 2026-08-18

### Added
- **Unified action placement.** Function buttons across every page now follow a three-tier
  contract — one page action in the sticky header, row actions right-aligned with every
  destructive verb behind an overflow menu, and a bulk action pinned to the bottom edge for as
  long as a selection exists — expressed as macros in the new `partials/_actions.html` instead
  of scattered per-page markup. The whole-stack menu, the per-group start/stop bar, add-on
  lifecycle verbs and `Create backup` all move onto it; `Delete`, `Remove`, `Uninstall` and
  `Restore` are reachable only through the overflow menu now.
- The group selection bar is `sticky bottom-4` over an opaque background instead of a static
  block below the service list, so acting on a selection no longer means scrolling past a dozen
  expanded modules first.
- **One service status chip.** The two header pills (core, add-ons) are replaced by a single
  fixed-width chip with a popover breaking the verdict down by section, an `n / m running` count
  per section and a local-time timestamp — the header no longer reflows as the pill text changes
  on every poll.
- **Server-tracked backup jobs.** `JobQueue.active_job()` is the single predicate behind the
  backup guard and the stack-action guard; the running state is no longer held only in the
  browser tab that started it, so a reload, a second tab or navigating away no longer forgets it,
  and the endpoint now answers 409 while a backup is already in flight instead of queueing a
  second one invisibly.
- A status strip on `/backup` reports what job is running and since when, links to its log, and
  reports the outcome of a job that finished while the operator was elsewhere before clearing
  itself after five minutes.
- A **Jobs** entry in the admin nav group opens `/jobs`, carrying an indicator dot visible from
  Add-Ons, Services and the dashboard whenever a job is in flight.
- **Brand-layer vendoring contract with `qdrant-ingest`'s operator UI.** `docker/tailwind.input.css`
  is split into `tailwind.brand.css` and `tailwind.app.css`, matching the layout already vendored
  into that interface, so the brand half can be diffed file-for-file against its copy; the
  Dockerfile concatenates them back at build time and the combined file is no longer committed.
  Every vendored file carries a `fidonis-brand: 1` stamp, checked by the new
  `tests/test_brand_layer.py`, and the cross-repo rule — bump the stamp in both places, in the
  same milestone — is documented in `CONTRIBUTING.md`.

### Changed
- `tailwind.brand.css` adopts relative `@font-face` URLs, closing the one documented deviation
  from the vendored copy; the stylesheet is always served from `/static/app.css`, so the
  resolved path is unchanged.

### Fixed
- The job log page rendered the raw `{"log": "…"}` JSON response into the page instead of plain
  text, and its poll never stopped once the job reached a terminal state.
- `.dockerignore` still listed the removed `tailwind.input.css` instead of the two files that
  replaced it, so the `css-builder` Docker stage failed with the split files missing from the
  build context.

## [0.4.0] - 2026-08-02

### Added
- **Service group control.** `/services` is no longer read-only: the page is now
  where the stack is started and stopped. The unit is the Compose profile — a
  *service group* — because that is the only granularity `papaia-ctl` accepts;
  there is no per-container verb. Each module header carries its profile and a
  checkbox, and ticking one module ticks every module the same profile brings up,
  so the coupling is visible rather than surprising: `librechat-websearch` alone
  covers Firecrawl, SearXNG, Jina and the Firecrawl MCP server. Several groups can
  be started, stopped or restarted in one job.
- The whole stack can be started, stopped and restarted from the header menu.
  That action removes the container serving the request, so it runs in a detached
  container that outlives the manager — the same mechanism restore already uses —
  and reports its outcome back through `/partials/stack-runner` once the page is
  reachable again.
- Add-ons gained a restart endpoint and their own per-add-on start / stop /
  restart controls on the services page. There is deliberately no bulk action
  across add-ons, and a stack action never passes `--addons`.
- `--clean-up` (`docker compose down` rather than `stop`) is offered by every
  confirmation dialog that stops something — a stop, and the stop half of a
  restart, where it turns the operation into a full recreate. A start rejects the
  field with 400 instead of ignoring it, since it stops nothing and honouring it
  would confirm an operation that did not happen. Volumes and
  `$PAPAIA_CONFIG_DIR` are untouched either way.
- A `/api/v1/stack/` surface backing all of the above: `groups` and
  `groups/{start,stop,restart}` for scoped actions, `{start,stop,restart}` for the
  whole stack, plus `runner` and `runner/clear` to read back and acknowledge a
  detached action.

### Changed
- `ALLOWED_CORE_VERBS` gains `start` and `stop`; `restore` stays out for the
  reason already documented there. Profile names are validated against the set
  `inventory.core_groups` reads out of the shipped Compose fragments — an
  allowlist, not a pattern — and the profile the manager itself runs under is
  refused regardless of what the caller passed, since stopping it would remove
  the container handling the request.
- `restart` is composed as stop → start rather than calling a `papaia-ctl` verb
  that does not exist; adding one would mean a change in the stack repo plus a
  minimum-version coupling. Nothing in `Fidonis/papaia` is touched by this
  release.
- `RunnerStatus.restore_point` is renamed to `target`, a runner no longer always
  being a restore. The JSON shape of the backup page is unchanged — the key is
  chosen per caller — and the legacy `de.fidonis.restore-point` label is still
  read back, because a restore recreates the manager mid-operation and the new
  code routinely inspects a runner the previous version started.

## [0.3.0] - 2026-07-31

### Added
- **Services.** An admin-only page at `/services` reporting what this deployment
  is configured to run and how much of it is up. Containers are read from a
  single unfiltered `docker ps -a`, partitioned by `com.docker.compose.project`
  into the core stack and the active add-ons, grouped by the `de.fidonis.module`
  label the Compose files put on every service, and scored from their
  healthcheck; the module in the worst state sorts to the top. Each module lists
  its containers with role, uptime, healthcheck result and published ports. The
  view is read-only — lifecycle control over core services stays with
  `papaia-ctl`.
- The declared state is read next to the live one, so a service that was
  configured but never started renders as *not deployed* rather than being
  absent. Core services come from the Compose fragments listed in
  `papaia/src/docker-compose.yml`, filtered by the profiles enabled in
  `COMPOSE_PROFILES`; add-on services from the active entries of
  `deployment.yaml` and their own Compose files. A torn-down stack therefore
  reports every module instead of an empty page, while an unreachable Docker
  socket still reports nothing at all — not knowing is not the same as knowing
  it is gone.
- Add-ons render in their own section with their own aggregate, so a failing
  add-on out of a customer catalogue does not report the stack itself as broken.
- Two status pills in the header of every page, one per section, visible to every
  authenticated account regardless of role. Each carries the aggregate only and
  links to the matching page for administrators, so a user without the admin role
  learns that something is unhealthy but not which service.
- **Collapsible sidebar.** The navigation panel collapses to a 4rem icon rail,
  peeks open on hover, pins back to full width, and remembers the choice across
  page loads. State is a single `data-sidebar` attribute on `<html>`, set before
  first paint the same way the theme already is, so a collapsed panel renders
  collapsed on the first frame. Below `lg` the rail is forced regardless of the
  stored value and the toggle drives a transient overlay drawer without writing
  the preference.
- The four administrative navigation entries are grouped under a divider and an
  "Administration" caption; the caption disappears with every other label when
  the panel collapses to a rail.
- An `asset_url()` template global appends a short content digest to the
  `/static` references in `base.html`, so a stylesheet held in a browser cache
  cannot outlive the markup it was generated from. The digest is keyed on the
  file's stat signature, so an asset rebuilt under a running process is picked up
  without a restart.

### Changed
- The Maintenance surface is now called **Backup / Restore** and is served at
  `/backup`, including its partials. The old paths answer 308, and the REST
  prefix stays `/api/v1/maintenance/` so the versioned surface is unaffected; it
  follows the UI name at the next major.
- `state.compute_status` takes its set of running Compose projects from the
  shared stack snapshot rather than issuing a `docker ps` of its own, so
  `/addons` and `/services` can no longer disagree about whether an add-on is up.
  The snapshot cache is invalidated whenever `papaia-ctl` returns, so a freshly
  started add-on does not read as stopped for another five seconds.

### Fixed
- A service shut down through `papaia-ctl` is reported as down rather than
  completed. Its exit is as clean as that of a one-shot container that finished
  its work, so the restart policy is read alongside the exit code: `unless-stopped`
  and `always` mark something that was meant to keep running, and its exit is an
  outage.
- The expanded sidebar geometry stays on the `w-64`/`ml-64` utilities in the
  markup, with only the deviations — rail, peek, drawer — defined in the
  stylesheet, so a stale or missing `app.css` build artifact degrades to the
  previous layout instead of rendering the content underneath the sidebar.

## [0.2.0] - 2026-07-30

### Added
- **Dashboard.** `/` now serves a tile overview of the deployed applications,
  configured through `$PAPAIA_CONFIG_DIR/manager/tiles.yaml` and seeded on first
  run with the applications the stack ships today. `{{KEY}}` placeholders in tile
  links resolve against the core `.env`.
- **Second access tier.** `MANAGER_USER_ROLE` (default `user`) grants
  dashboard-only access alongside `MANAGER_ADMIN_ROLE` (default `admin`).
  `/auth/callback` admits either role; accounts holding neither are rejected at
  login without a session being created.
- Per-tile `visibility: all | admin`, filtered server-side — an admin-only tile
  is absent from a regular user's response, not hidden by CSS. Groups left empty
  are dropped, an unrecognised `visibility` value restricts the tile rather than
  widening it, and a link that does not resolve to an `http(s)` or site-relative
  target is dropped instead of rendered.
- **Maintenance section** (admin-only) for stack-level operations, driving
  `papaia-ctl backup` and `papaia-ctl restore` so backups no longer require shell
  access on the host. Backup runs hot as an ordinary queued job with an optional
  retention period; restore runs in a detached container that survives the
  teardown of the stack the manager itself is part of.
- Restore points are listed from the catalogue `papaia-ctl` writes next to the
  snapshots. A restore point recorded as `failed` is listed and inspectable but
  not restorable.
- New API surface under `/api/v1/maintenance/` — `backup-dir`, `restore-points`,
  `restore-points/{id}`, `backup`, `restore`, `restore/status`, and a `DELETE`
  to acknowledge a finished restore.
- The papAIa backup directory is mounted at its host path (path parity, same as
  workspace and config). `PAPAIA_BACKUP_DIR` is documented in
  `docker/.env.example`; the manager itself reads the value from
  `$PAPAIA_CONFIG_DIR/.env` at request time so it always agrees with the stack.
- Catalogs can be edited from the UI — URL and branch for git sources, path for
  local ones, plus the enabled state — wired to the existing
  `PUT /api/v1/catalogs/{name}`.
- The "Add catalog" form has a **Branch** field for git sources (default `main`).

### Changed
- The add-on gallery moved from `/` to `/addons` and is labelled **Add-Ons**.
- Authorization is expressed as two route dependencies (`AdminUser`, `AnyUser`)
  backed by pure predicates in `auth/roles.py`, replacing the single
  `require_admin` dependency. Pages and the JSON API are gated by the same rule;
  a denied navigation renders an HTML error page while `/api/` keeps returning
  JSON.
- The `papaia-ctl` wrapper carries a second, separate allowlist for stack-level
  verbs containing `backup` only. `restore` is deliberately absent, so it cannot
  be dispatched as a child of the manager process.

### Fixed
- Refreshing a git catalog no longer swaps the raw job-queue JSON response into
  the page, and deleting a catalog removes its row without a manual reload. Both
  actions now use the app's job-polling pattern: busy spinner, live "view log"
  link, automatic list refresh, and a success/failure toast.
- The remaining German UI strings are now English. The dashboard's "Installierte
  Add-Ons" stat tile is now labelled "Add-Ons", since it counts all add-ons
  (installed and available), not just installed ones.

## [0.1.0] - 2026-07-27

### Added
- Install dialog now shows **all** `.env.example` variables (not just a filtered subset).
- Non-secret, non-marker fields are pre-filled with their `.env.example` default value.
- Add-on manifests can declare `type:` (`text`/`integer`/`decimal`/`url`), `secret:`,
  `min:`, `max:`, and `pattern:` per variable in `env_prompts`.
- Secret fields render masked by default; an eye button inside each field toggles
  plaintext reveal / hide.
- Declared types are validated server-side before the install/update/save-config job
  is enqueued; type mismatches return HTTP 422 synchronously.
- Newline and carriage-return injection in any env value is now rejected on all three
  write paths (`install`, `update`, `save-config`).
- Variables with a `GENERATE_*` or `REPLACE_WITH_*` placeholder in `.env.example` are
  now grouped under "Weitere Einstellungen" and shown with an appropriate badge.

### Changed
- **Breaking (security):** `GET /api/v1/addons/{name}/env-form` no longer returns
  `current_value` for secret fields. The field is always `null` — the value is never
  sent to the browser. `current_set: true` still indicates that a value is stored in
  the config bundle.
- Only changed values are submitted on install/update (diff against `.env.example`),
  preventing accidental overrides of auto-generated secrets.
- Add-on install now accepts `core_env` so the install form can pre-fill
  `default_from_core` values from the running core's environment.

### Fixed
- Local catalogs (`type: local`) now scan, display, and install end to end.
- The add-catalog dialog submits its form as JSON, matching the API's expected
  content type.
- The add-catalog dialog shows the path field when the catalog type is `local`.
- Add-ons with the same name in more than one enabled catalog are no longer
  silently dropped — each distinct version is now surfaced as its own entry,
  with same-version duplicates collapsed and annotated with the catalogs that
  shadow the primary one.

[0.6.0]: https://github.com/Fidonis/papaia-manager/releases/tag/v0.6.0

[0.5.0]: https://github.com/Fidonis/papaia-manager/releases/tag/v0.5.0

[0.4.0]: https://github.com/Fidonis/papaia-manager/releases/tag/v0.4.0

[0.3.0]: https://github.com/Fidonis/papaia-manager/releases/tag/v0.3.0

[0.2.0]: https://github.com/Fidonis/papaia-manager/releases/tag/v0.2.0

[0.1.0]: https://github.com/Fidonis/papaia-manager/releases/tag/v0.1.0

[Unreleased]: https://github.com/Fidonis/papaia-manager/compare/v0.6.0...HEAD
