# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes are generated automatically by [release-drafter](https://github.com/release-drafter/release-drafter)
based on merged pull requests; this file mirrors the published releases.

## [Unreleased]

<!-- Updated automatically by release-drafter as PRs are merged to `main`. -->

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

[0.1.0]: https://github.com/Fidonis/papaia-manager/releases/tag/v0.1.0

[Unreleased]: https://github.com/Fidonis/papaia-manager/compare/v0.1.0...HEAD
