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

**Token handling:** OIDC client secrets and catalog tokens never appear
in logs, process arguments, or HTTP responses.

## Out of scope

- Vulnerabilities in third-party software listed under
  [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) — please report
  those to the respective upstream project.
- Issues that require attacker-controlled OIDC issuer configuration.
- Denial of service via resource exhaustion of the underlying Docker host.
