# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes are generated automatically by [release-drafter](https://github.com/release-drafter/release-drafter)
based on merged pull requests; this file mirrors the published releases.

## [Unreleased]

<!-- Updated automatically by release-drafter as PRs are merged to `main`. -->

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

[0.2.0]: https://github.com/Fidonis/papaia-manager/releases/tag/v0.2.0

[0.1.0]: https://github.com/Fidonis/papaia-manager/releases/tag/v0.1.0

[Unreleased]: https://github.com/Fidonis/papaia-manager/compare/v0.2.0...HEAD
