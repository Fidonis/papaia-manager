# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes are generated automatically by [release-drafter](https://github.com/release-drafter/release-drafter)
based on merged pull requests; this file mirrors the published releases.

## [Unreleased]

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

<!-- Updated automatically by release-drafter as PRs are merged to `main`. -->

[Unreleased]: https://github.com/Fidonis/papaia-manager/compare/HEAD...main
