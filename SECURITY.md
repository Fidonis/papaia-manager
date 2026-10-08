# Security policy

We take the security of `papaia-manager` seriously. Thanks for helping
us keep it safe.

## Supported versions

Security fixes are issued for the latest published `1.x` release.
Older releases receive only critical-severity fixes on a best-effort basis.

| Version | Status |
|---|---|
| `1.x` (latest) | ✅ supported |
| Older `1.x` | 🟡 critical fixes only |

## Reporting a vulnerability

**Do not open a public issue for security problems.**

Please report vulnerabilities through GitHub's
[Private Vulnerability Reporting](https://github.com/Fidonis/papaia-manager/security/advisories/new).
This routes the report directly to the maintainers in a private advisory.

If for some reason you cannot use the private reporting flow, contact
the maintainers at `security@fidonis.de` and we will open the private
advisory on your behalf.

Please include:

- A clear description of the vulnerability and its impact
- Steps to reproduce (a minimal proof of concept is ideal)
- The version / commit affected
- Any suggested mitigation or fix, if you have one

## What to expect

- **Acknowledgement** within 3 working days of your report.
- **Initial triage** (severity assessment, confirmation, scope) within
  10 working days.
- **Coordinated disclosure**: once a fix is ready, we publish a GitHub
  Security Advisory and a patched release. Embargo periods are agreed
  with the reporter on a case-by-case basis; 90 days is the default
  upper bound.
- **Credit**: with your permission, your name (or handle) is listed in
  the advisory and the release notes.

## Security design considerations

**docker.sock access:** The manager container mounts the Docker socket at
`/var/run/docker.sock`, which grants root-equivalent access to the host
daemon. This is required for Compose operations and is intentional; the
`manager` Compose profile is disabled by default. Only enable it where
a web control plane is explicitly desired and access is gated by Keycloak.

**Subprocess isolation:** All calls to `papaia-ctl` are routed through
`app/core/ctl.py`, which validates the operation verb against an allowlist
and uses arg arrays rather than shell string interpolation.

**Core stack lifecycle:** Two allowlists are kept apart, so an add-on name can
never be dispatched as a stack operation or the other way round. The stack-level
set holds `backup`, `backup-delete`, `start`, `stop` and `restore-scoped`.
`restore` is deliberately absent: it tears the core stack down unconditionally,
and the manager is a service of that same project. `restore-scoped` is a different
verb that requires a selection and refuses the option that clears volumes in
`papaia-ctl` itself, so it cannot replace `$PAPAIA_CONFIG_DIR`; `backup-delete`
runs no `docker compose` command and needs an explicit restore point. `start` and
`stop` are reachable only because every caller scopes
them to Compose profiles, and the profile names are validated against the set
read out of the deployment's own Compose fragments — not against a pattern
alone. The profile the manager itself runs under is rejected regardless of what
the caller passed. Stack-wide operations, which do include that profile, run in
a separate container rather than as a child of this process. Every one of these
routes requires the admin role and a session-bound CSRF token, and writes an
audit entry to `$PAPAIA_CONFIG_DIR/manager/audit.log`.

**Token handling:** OIDC client secrets and catalog tokens never appear
in logs, process arguments, or HTTP responses. The same holds for the secrets of
the RAG system described next.

**RAG system:** The RAG pages and `/api/v1/rag/` exist only while the core's
optional `rag` profile is active (they answer 404 otherwise, after the role check),
require the admin role and, for changes, a session-bound CSRF token, and every
change is audited. The secrets they handle are write-only and never appear in logs,
responses or audit entries: the Qdrant api-keys (`QDRANT_JWT_SECRET` and the keys
stored in connections), `QI_CONNECTIONS_SECRET`, the ingester's `QI_API_TOKEN` and
the values of ingest credentials. Stored api-keys and credentials are Fernet tokens
(`enc:1:`) whose key is derived from `QI_CONNECTIONS_SECRET`, which lives in
`ai/rag/.env` next to the files that hold them. That keeps them out of casual sight
(a copied file, a screenshot, a log) but not away from anyone who can read both,
and a backup contains both. A stored api-key is sent only to the address it was
stored with. Uploaded files are staged per administrator, removed after a
successful run and after a time limit otherwise, and the folders the pages browse
are confined to the documents folder; a symbolic link is refused. The dashboard
tile for Qdrant is shown to administrators only, because Qdrant's own dashboard
bypasses the MCP server's role checks.

## Out of scope

- Vulnerabilities in third-party software listed under
  [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) — please report
  those to the respective upstream project.
- Issues that require attacker-controlled OIDC issuer configuration.
- Denial of service via resource exhaustion of the underlying Docker host.
