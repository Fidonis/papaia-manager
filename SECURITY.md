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
and uses arg arrays rather than shell string interpolation. The output of the
child is read in bounded chunks and one line is cut at 1 MiB before it reaches the
job log, so a process that prints without end cannot exhaust the manager's memory
or fill the log.

**Core stack lifecycle:** Two allowlists are kept apart, so an add-on name can
never be dispatched as a stack operation or the other way round. The stack-level
set holds `backup`, `backup-delete`, `start`, `stop`, `restore-scoped` and
`keycloak-role-sync`. `restore` is deliberately absent: it tears the core stack
down unconditionally, and the manager is a service of that same project.
`restore-scoped` is a different verb that requires a selection and refuses the
option that clears volumes in `papaia-ctl` itself, so it cannot replace
`$PAPAIA_CONFIG_DIR`; `backup-delete` runs no `docker compose` command and needs
an explicit restore point; `keycloak-role-sync` runs none either and only applies
what the realm template says to the running Keycloak. `start` and
`stop` are reachable only because every caller scopes
them to Compose profiles, and the profile names are validated against the set
read out of the deployment's own Compose fragments — not against a pattern
alone. The profile the manager itself runs under is rejected regardless of what
the caller passed. Stack-wide operations, which do include that profile, run in
a separate container rather than as a child of this process. Every one of these
routes requires the admin role and a session-bound CSRF token, and writes an
audit entry to `$PAPAIA_CONFIG_DIR/manager/audit.log`.

**Configuration writers:** The core's own commands that write, `setup` and
`module-purge`, have an allowlist of their own and are reachable only through
typed requests. They pass nothing but the options of the mail relay or of one
optional module (never `--force`, `--env`, a host name or any other part of the
stack's configuration); the core validates and stores the values, and refuses
`module-purge` while the module is switched on. The mail server's password reaches
the core in the environment of the child process only, never in an argument. A
change is refused with a 409 while a restore, an upgrade or a stack action runs.

**Token handling:** OIDC client secrets and catalog tokens never appear
in logs, process arguments, or HTTP responses. The same holds for the mail
server's password, the webhook URLs of notification recipients and the secrets of
the RAG system described next.

**Sign-out:** `POST /auth/logout` requires the session-bound CSRF token and ends
the Keycloak session with the session's refresh token before the manager's own
cookie is cleared; there is no `GET` variant that a link could trigger. A session
ended in Keycloak stops working in the manager when its access token is next
renewed.

**Users and roles:** The Users page and its API are reserved for
`MANAGER_IDENTITY_ADMIN_ROLE`, a tier above the admin role, and exist only where
the accounts live in the bundled Keycloak. The manager has no credential of its
own for them: Keycloak's Admin API is called with the access token of the
signed-in account, obtained from the session's refresh token and kept in memory
only (never in the session cookie, a response, an audit entry or a log line), so
Keycloak applies that account's rights. Nobody can disable their own account, take
the identity role from themselves or end all of their own sessions there. A
temporary password is returned once, with `Cache-Control: no-store`, and appears
in no audit entry or log line. The roles the stack ships are never changed or
deleted from there.

**Add-on catalogs:** A catalog is input from a third party, and so is the
`components` list of its manifests. It is read defensively: an entry of the wrong
shape is skipped, text is cut to a fixed length, at most 32 entries are read, and
only an `https://` source becomes a link. The templates escape everything they
show. Removing an add-on is refused while its `docker-compose.yml` is missing,
because the core's Compose call would then address the core project instead of the
add-on's.

**Language preference and bundled images:** The language an account chose is kept
in `manager/preferences.yaml` by OIDC subject (mode 0600, replaced atomically) and
holds nothing else. `POST /account/language` requires the session-bound CSRF token,
accepts only a language the manager ships and redirects only to a path on its own
origin, never to a host. The icons of the default tiles are files in the image,
served from `/static/icons/`; a test refuses one that carries a script, an embedded
image or a link to another host.

**Mail and notifications:** The mail server's password is write-only: it is never
shown or returned, it is used only for the host, port and login it was stored with,
and it is kept out of the job log, the audit log and the responses. A webhook URL
of a notification recipient authorises whoever posts to it, so it is stored in
`manager/notifications.yaml` with mode 0600 (part of the configuration directory's
backups), shown only as `https://host/…` after it is saved, and absent from every
response, audit entry, log line and failure reason. Redirects are not followed. A
notification carries no job log and no error text. The test mail is limited to
administrators and rate limited.

**Overlay editing:** The overlay files can hold API keys, so their content is never
written to a log or an audit entry, and request bodies are read by hand so that a
validation error does not echo them. Targets are fixed keys, never paths. A write
is compare-and-swap on the file's SHA-256 revision, refuses a symbolic link, and
is preceded by a copy of the previous file in
`manager/overlay-history/` (owner-only, the latest 20 per file).

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
