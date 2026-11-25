# papaia-manager

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
Maintained by **Fidonis** · See [TRADEMARK.md](TRADEMARK.md) for the trademark notice.

A web-based control plane for the papAIa stack's add-on lifecycle. It gives
non-technical operators a browser UI for discovering, installing, starting,
stopping, removing, and upgrading add-ons — without needing shell access to
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

The manager mounts **no Docker socket**. It reaches Docker only through the
core's `papaia-docker-gate` (see *Docker access* below). It mounts the papAIa
workspace, config and backup directories **at their host paths** (path parity — required so that
bind-mount sources in add-on compose files resolve identically whether
`docker compose` is invoked by the manager or by an operator on the host
directly). Because of this, the `manager` profile is **Linux-only**;
Windows/macOS Docker Desktop hosts don't preserve host-path parity through
the VM boundary.

## How it works

**Shared frame.** Every page is the content of the platform's shared frame from
`papaia-kit` (the vendored wheel under `src/vendor/`): the installation's name, the module
switcher, the navigation, the footer with the sidebar and appearance buttons and the account
menu (**Language**, **My area**, **Help**, **Sign out**), and the page header with the one
heading. The switcher lists the modules of the platform catalog
`$PAPAIA_CONFIG_DIR/platform/services.json` that the account may open; without a catalog it
names the manager alone, the brand leads to the dashboard and **My area** is `/me`, the
account's own page. With a catalog the brand and **My area** lead to Mein Bereich. A catalog
the frame cannot verify is left out, never half used. The frame is display only: every page
and API route checks the roles itself. Below 1024 px the sidebar is a drawer behind the menu
button. The frame's stylesheet, script, symbols and fonts are served from the wheel at
`/_kit/<kit version>/…`, the platform logo at `/_kit/brand/logo` while its SHA-256 matches the
catalog; nothing is loaded from another host. The footer forms (`POST /_kit/sidebar`,
`/_kit/appearance`, `/_kit/language`) need the session and its CSRF token, accept exactly one
of their values and write an audit entry each: the sidebar state is kept in the `pk_sidebar`
cookie; where the appearance (light, dark, or the system setting until one is chosen) and the
language are kept is described under **Language and appearance**. **Help** (`/help`) explains the manager's
terms from the platform glossary and names the contact of the installation.
`cd src && uv run python -m app.shell --input <file>` renders a shell input as the manager
does, for the kit's markup check. On every page the skip link to the content is the first
element the keyboard reaches, and the browser title reads `<page> – Manager – <installation>`
(`tests/test_page_frame_guard.py` checks every page of `ui/pages.yaml` and the state page
"Kein Zugang").

**Specimen page.** `/_kit/specimen` shows the reference content of the vendored kit inside the
frame, for signed-in persons only (a browser without a session is sent to the sign-in). It
carries `<meta name="papaia-kit">` with the version of `src/kit.lock` and nothing of the
manager's own styles or scripts except the CSP build of Alpine: it is the first page on the
strict policy (see **Content security policy**). The kit's design and conformance checks
read this page.

**Language and appearance.** The interface is available in English and German. Every account
chooses its own language in the account menu at the bottom of the sidebar (**Language**:
**Automatic**, **Deutsch** or **English**) and its appearance with the button above it (light or
dark; the system setting until one is chosen). The button names the appearance it turns on:
**Dark mode** while the page is light, **Light mode** while it is dark, also while the system
setting is followed. Neither is kept in the browser's storage.

- *On a platform with Mein Bereich* (the platform catalog lists `me`), both come from the
  platform's preference service `prefs`, the one place every module reads them from. The manager
  reads them when the account signs in (`GET /api/v1/preferences/me` at `PREFS_URL`, default
  `http://prefs:8080`) and keeps them in the session. A change made in another module (Studio,
  Flow, Mein Bereich) shows on the manager's next page without signing in again: that module sets
  the session cookie `pk_ui` (`<appearance>.<language>.<etag>`), and when its etag differs from the
  session's, the manager reads `prefs` again before it renders the page, so the page arrives in
  the new language and appearance (`<html lang=… data-theme=…>`). The footer and menu forms write
  `PUT /api/v1/preferences/me` with `If-Match`; after a change by someone else in between, the
  current record is read and the change sent once more. Each write leaves one `papaia.audit/1`
  row (catalog `prefs`, operation `write_own`, the field name, never the value) and an audit-log
  entry `kit.appearance` or `kit.language`, and sets `pk_ui` (no lifetime, not `HttpOnly`, no
  personal data). `PREFS_COOKIE_DOMAIN` (for example `papaia.example.org`) shares the cookie with
  the modules below that domain; unset, it is the manager's own host only. Every call carries the
  signed-in account's own token, exchanged with the manager's client for the audience
  `papaia-prefs`; the realm has to allow that exchange for the client `papaia-manager` (optional
  scope `aud-papaia-prefs`). **Automatic** follows the browser the way every module does
  (German when the browser asks for neither). The token's `locale` claim and the manager's
  language file are not read. When `prefs` cannot be reached, pages render with the values the
  session last held, else with the system appearance and the browser's language, and `prefs` is
  not asked again for that account for 30 seconds; a change cannot be saved then (503).
- *Without Mein Bereich*, the language is stored on the server with the account (by its OIDC
  subject in `manager/preferences.yaml`), so it is the same on every computer the account signs
  in from, and the appearance in the signed session. **Automatic**, which is also what an account
  has before it chooses, follows the browser's language and falls back to the installation's
  default (`MANAGER_DEFAULT_LOCALE`, English unless set).

Another language is a catalogue file under `src/app/locale/` and needs no change to the code; see
[CONTRIBUTING.md](CONTRIBUTING.md). Log lines, the audit log, job logs and the emails and webhooks
of notifications stay in English.

**Modules.** **Modules and add-ons → Modules** (`/modules`) lists the modules of the
suite (Studio, Data, Flow, Assistant) with the version of the suite bill of materials of the
core checkout (`compat/suite-<VERSION>.yaml`, repos with role `module`) and their state here:
*available* (not installed; the row says "<module> is not set up in this installation." and
names `papaia-ctl addon install <name>`, nothing has to be copied out of a module repository),
*running* with **Open** (the module's address) and, when its catalog entry names an
administration overview (`ui[].admin_url`), **Manage**, *not reachable* (installed but
stopped: a locked entry "<module> is currently not reachable.") or *installed*. Both
sentences are the platform kit's fixed texts, the same in every module. The addresses and the
state come from the platform catalog `$PAPAIA_CONFIG_DIR/platform/services.json`, read with
the platform kit's reader: a broken file keeps the last valid state and the page says so.
When the core pins a release key (`compat/release-key.pub`) the bill of materials must carry
a valid signature (`suite-<VERSION>.yaml.sig`) or it is not used; without a pinned key the
versions are marked as not verified. The page only reads.

**Administration places.** The platform kit's map of administration places
(`contract/admin-map.yaml`) assigns each administration task to one place. The manager's
places are rows of its navigation: installing and upgrading modules (`/modules`, `/upgrade`),
backup and restore (`/backup`), users and roles (`/users`), name, logo and contact
(`/settings`) and the audit log (`/audit`). A module's group **Verwaltung** leads into the
manager for the audit log and for assigning roles; `tests/test_admin_map.py` checks that every
such place is a page here.

**Terms and word check.** The interface uses the terms of the platform glossary (shipped in
the kit). `cd src && uv run python -m app.i18n.export` writes every interface text with its
kind (navigation, title, tab, button, text) to `i18n/export/{de,en}.json`;
`uv run python -m app.i18n.wordcheck` runs the kit's word check over that export and the
templates. `i18n/glossary-map.yaml` names the keys of the glossary's mandatory places (they
must read exactly as the glossary says) and the only keys allowed to use the restricted words
*Module*, *Add-on catalog* and *Review*. Findings that stay for a reason are listed with that
reason in `i18n/words-known.yaml`; the check fails on any finding that is not listed and on a
listed one that is gone, so the list only shrinks. `tests/test_word_check.py` runs both and
checks that the export is current.

**Delegations.** For an account with the `MANAGER_IDENTITY_ADMIN_ROLE`, the modules page
also shows the delegations (offline sessions at `flow-engine` for Flow and `assist-gate` for
Assistant): how many each person has granted per module, and nothing else (no session,
address, time, purpose or token). **Revoke** ends every delegation of that person at that
module by removing the person's consent at the module's client. The calls use the
administrator's own Keycloak token, so Keycloak decides: listing reads the clients and their
offline sessions, revoking changes the user's consents. A refusal by Keycloak is shown as such.

**Inventory.** The page **Inventory** (`/inventory`, German "Bestand") lists the work objects
built in the modules: apps and dashboards (Studio), workflows (Flow), metrics and dashboards
(Data), event rules and event reminders (Assistant), with owner, deputy, state, highest data
class, the effects of their bindings, last use and flags (`owner_inactive`, `deputy_missing`,
`unused_180d`, `confirmation_due`, `class_above_role`). Only the realm role
`MANAGER_AUDITOR_ROLE` (default `papaia-auditor`) sees it; the admin role does not include
it. The manager asks every module at `GET /internal/inventory` on each visit, over mutual
TLS with the client certificate of `AUDIT_TLS_CERT_FILE`, at the addresses in
`INVENTORY_SOURCES`, and keeps no copy. An answer counts only when it is a valid
`papaia.inventory/1` document of the platform kit's schema with objects of that module alone
and a correct checksum; a module that does not answer that way, or an installed module
without an address, is listed as `nicht erreichbar` and the page says that the list is
incomplete. Filters: flag, module, data class, effect, person (account id; owner, deputy or
approver). **Export (CSV)** and **Export (JSON)** hand out the filtered list; the JSON form is
the `papaia.inventory/1` document, the CSV form has one row per object and one row per module
that did not answer (`state` = `nicht erreichbar`), with formula-like cells quoted. Every
export, a refused one too, is one entry in the audit log and one `inventory_exported` row
with the checksum of what was handed out. The inventory never carries content: no rows, no
conditions, no reminder text. The page is drawn with the platform kit's patterns (table, badge,
alert, select field, empty state) and is listed in `ui/pages.yaml` as the page of the
auditor; the design check signs in as the list's test person of each page.

**Data subject requests.** Information and erasure for a person without an account
(`papaia-ctl person export|erase` on the host) need a second person: the request names her, and
she confirms or rejects it on the page **Data subject requests** (`/person-requests`, German
"Betroffenenanträge"). Only the realm role `MANAGER_CONFIRM_ROLE` (default `papaia-confirm`) or
`MANAGER_IDENTITY_ADMIN_ROLE` (default `papaia-admin`) sees the page; it signs in like the user
role and reaches no administration page. The page lists the requests of the last 7 days that
name the signed-in person, read on every visit from the core's `papaia-audit` at
`AUDIT_READ_URL` (default `https://papaia-audit:8443`) over mutual TLS with the client
certificate of `AUDIT_TLS_CERT_FILE` and the person's own token, exchanged for the audience
`papaia-audit`; the manager keeps no copy. Each row shows the request id, the kind
(information or erasure, with or without hand-over of owned objects), the person concerned and
the person who filed it as pseudonyms, the time it was filed and its state. **Confirm** needs a
sign-in of at most five minutes ago (**Confirm your sign-in**); papaia-audit checks it again.
The person who filed a request cannot confirm it: the manager refuses from papaia-audit's own
answer before sending anything, and papaia-audit refuses as well. Either person may
**Reject**. Confirming an information request shows the passphrase of the archive once, on the
answer to the confirmation (`Cache-Control: no-store`); it is not stored, logged or written
into an audit row, and reloading does not show it again. Every decision, a refused one too, is
one entry in the audit log and one `papaia.audit/1` row (`person_request.confirm` or
`person_request.reject`, `object_ref` `person_request` with the request id) under the
request's correlation id, which papaia-audit receives with the decision.

**Permission report.** The page **Permission report** (`/access-report`, German
"Berechtigungsbericht", navigation group Review) shows the platform's permission report
(format `papaia.access-report/1`) by person or by connected system. Only
`MANAGER_AUDITOR_ROLE` sees it; without the role page and download answer `403` before
anything else happens, and that refusal is one `inventory_exported` row with result
`denied`. The report is built by the core's papaia-audit, not by the manager: on every
request the manager exchanges the signed-in person's token for the audience `papaia-audit`
(token exchange with the manager's client, `audience=papaia-audit`, `scope=aud-papaia-audit`)
and calls `POST /v1/access-reports/build` at `AUDIT_READ_URL` (default
`https://papaia-audit:8443`) over mutual TLS with the client certificate of
`AUDIT_TLS_CERT_FILE`, with the request's correlation id. The exchanged token serves that
one call and is dropped; it never reaches the session, a log or the page. The manager holds
no Keycloak credential and no realm right for the report and calls neither the Admin API
nor Docker. By person a row shows realm and module roles, groups, owned and deputised
service accounts, delegations per module, a separation-of-duties conflict and the emergency
account; by system the clients with an exchange right and its effects, who asked for and
confirmed it, the service accounts with that audience and `im Zielsystem geführt` for the
rights inside the target system. Every row shows when it was last confirmed and, when the
recertification is due, the mark "Confirmation due"; a filter shows the due rows only. The
page shows the report's checksum. **Download (CSV)** and **Download (JSON)** hand out the
bytes papaia-audit answered, unchanged; the manager keeps no copy and writes no audit row of
its own for a retrieval, since papaia-audit records each one. A JSON answer is used only
when it is valid against the published schema (`src/app/core/schemas/`), of the kind asked
for, with sorted rows and matching row digests and checksum. A refusal of papaia-audit
(`403`) is shown with its own message and hint; a failed exchange, an unreachable
papaia-audit or an unusable answer shows an error state with the correlation id and no
partial data. Confirming rows (recertification) stays on the host (`papaia-ctl report
confirm`).

**Audit rows.** Besides the local audit log, every write, send and delete of the manager
leaves one `papaia.audit/1` row: who acted (the signed-in person, or the manager's own service
identity for scheduled work), the operation, its result and the request's
`X-Papaia-Correlation-Id` (a missing, oversized or token-shaped value is replaced with a new
one; the response carries the id back). A row never carries a target, a parameter or a name.
Rows are written with the platform kit's outbox writer to `AUDIT_OUTBOX_PATH` (default
`$PAPAIA_CONFIG_DIR/manager/audit/outbox.sqlite3`, mode 0600) and delivered to
`AUDIT_INGEST_URL` over mutual TLS with the client certificate in `AUDIT_TLS_CERT_FILE` /
`AUDIT_TLS_KEY_FILE`, checked against `AUDIT_TLS_CA_FILE`. A row leaves the outbox only once
papaia-audit acknowledged it; while papaia-audit is unreachable, or the certificate files are
not mounted, the rows wait and the manager keeps working.

**Settings and branding.** Administrators open **Settings** to change the
name and second line shown at the top of the sidebar (defaults: "papAIa
manager" / "by Fidonis") and to upload their own logo (PNG, JPEG, WebP or SVG,
up to 512 KB). With a platform catalog the frame shows the platform's branding
instead (name, second line and logo from `platform/services.json`), and the shared
frame shows a logo only from there: the uploaded logo is kept and served at
`/brand/logo`, but not shown in the sidebar. The values are stored in
`$PAPAIA_CONFIG_DIR/manager/settings.yaml`, one section per topic, and the logo
in `$PAPAIA_CONFIG_DIR/manager/branding/`, so both are part of a backup.
Without a settings file nothing changes. On papAIa core 1.10.0 and newer the brand of an
older manager is handed once to the installation's settings (see *Upgrades of the manager's
data* below). A second card, **Host monitoring**, sets
how often the Host page re-measures the machine (10 seconds to 60 minutes,
default 60 seconds); it applies to every administrator.

**Installation.** The tab **Installation** of Settings holds what applies to the whole
installation and every module, as the core keeps it (papAIa 1.10.0 and newer): the name, the
suffix and the logo (PNG, JPEG or WebP, up to 1 MiB) shown at the top of every module's
sidebar, the contact point (a name, and a mail address or phone number) that every module
names when someone has no access or a function is not allowed, the four-eyes principle for
approvals (`approvals.four_eyes`) and how long a delegation lives (days without use and days in
all, 1 to 365). The manager stores none of these values: each card hands its values to the
core's own commands (`papaia-ctl settings set|unset`, `papaia-ctl delegations lifetime`),
which check them again, apply the security profile, write their own audit row and render the
platform catalog. Every module reads the catalog at least once a minute and serves the logo
from its own origin under `/_kit/brand/logo`, so a change reaches all modules within 60
seconds. In the security profile `kritis` the four-eyes principle is shown as fixed (always
on) and the core refuses a delegation lifetime above 7 days without use or 30 days in all;
the refusal is audited and shown on the page. Without a contact point, a denial reads "No
contact point on record" ("Ansprechstelle nicht hinterlegt"). The tab needs the manager's
administrator role and the platform's administration role (`MANAGER_IDENTITY_ADMIN_ROLE`);
every change also leaves one entry in the manager's audit log, which names the settings but
never the contact point's values.

**Upgrades of the manager's data.** The files under `$PAPAIA_CONFIG_DIR/manager/` are read by
a newer manager as an older one wrote them; the tests hold 1.8.0 to a data set written by 1.6.0.
A value that a release moves to another owner is moved once, in the background, when the
manager starts (`app.core.migrations`), and `manager/migrations.json` (mode 0600) records what
became of each field: `applied`, `kept` (the new owner has a value of its own), `refused` or
`unsupported`. A move that cannot finish, because the core does not answer or publishes no
platform catalog yet, is tried again at the next start; a ledger that cannot be read stops every
move rather than repeating one. Each move that writes leaves one audit entry (`migration.<topic>`,
as `papaia-manager`) naming the fields, never their values. 1.8.0 has one move,
`1.8.0-branding`: the sidebar name, second line and logo of 1.7.0 and earlier become the
installation's name, suffix and logo in the core where the core has none yet (an SVG logo, which
the core does not take, stays with the manager). The old values stay in `settings.yaml`. The
upgrade steps are in the [changelog](CHANGELOG.md).

**Modules and overlay.** Settings has four tabs: **Manager** (the cards above),
**Installation**, **Modules** and **Overlay**. **Modules** is the one place that switches the
optional parts of the stack on and off: LocalAI, web search, the RAG system, voice
(local speech-to-text and text-to-speech, papAIa 1.9.0 and newer), the mail relay and
the AI map (the core's knowledge map, papAIa 1.9.0 and newer; switching it on also
generates its secrets on an installation set up before the module existed, and shows
the AI map page).
Switching a module on stores the choice with the core's own `setup`
(`--enable-local-ai`, `--enable-web-search`, `--enable-rag`, `--enable-voice`,
`--enable-knowledge-map`, `--enable-smtp`, and for LocalAI and voice the accelerator variant, which is chosen on
the page because GPU detection cannot see the host from inside the container; voice
offers CPU, NVIDIA with a driver for CUDA 12.8 or newer, and AMD ROCm) and starts the
module's profile; the RAG system and the AI map also recreate LibreChat, whose
configuration lists their tools, and voice recreates LiteLLM and LibreChat, which route speech to its
engines and offer it in the speech panel. Voice downloads several GB of models on its
first start, which takes longer than a switch waits: the switch finishes while they
load, and the Services page shows when voice is ready. The
RAG pages and the Mail page hold those modules' settings and no longer switch
anything. Switching a module on asks the questions `papaia-ctl setup` asks, inline under
its row (no dialog, so it reads the same on a phone), but only when the answers are missing (the module is new to this installation, or its settings
were deleted): the public URL of the RAG system and of LocalAI, prefilled with what the
core derives from the stack's address, and for the mail relay the mail server (host,
connection, port, login, password, sender), which **Test mail server** tries before the
relay is switched on without sending a mail or saving anything. A module whose settings exist switches on
without a dialog. The mail server can also be saved on the Mail page with the relay off.
The manager itself cannot be switched off.

Switching a module off removes its containers and asks what happens to what it
leaves behind: **keep the data and the settings** (switching it on again brings
everything back), **delete the data** (its Docker volumes) **and keep the settings**,
or **delete the data and the settings** (they go back to what the release ships).
Deleting asks you to type the module's name, and the dialog lists the volumes the
core reports. Settings alone are not offered: the RAG system's stored credentials are
encrypted with a secret that belongs to its data, and a module without its settings
but with its data would be of no use to anyone. The RAG documents folder is never
deleted. The AI map's own files (entries, overrides, gold set and governance records,
edited on the AI map page) are neither data nor settings: no choice above deletes them.
The dialog lists the ones that exist under **Your own files** with a separate,
unticked **Also delete these files**, which also removes their edit history and the map
built from them (papAIa 1.9.0 and newer). Deleting needs a core with the module operations (papAIa 1.6.0); an older
core still switches modules off and keeps everything. **Overlay** edits the customer overlay, the files under
`$PAPAIA_CONFIG_DIR/overlay/` that the core merges last into the LibreChat,
LiteLLM, Chatterbox (the speech synthesis of voice) and SearXNG configuration. The editor checks the YAML while you type,
shows the effective result (the core merges the draft in memory and the page
shows the difference to what the file renders to now; this needs a core with the
`render-preview` command, otherwise only the syntax check is available), and
**Apply** saves the file and recreates the services that read it, as a queued
job. Before every Apply, Discard and Roll back the previous file is copied to
`$PAPAIA_CONFIG_DIR/manager/overlay-history/` (the latest 20 per file, readable by
the owner only, part of a backup); the **Versions** tab shows them, restores one
as a draft or rolls back to it directly. If the services do not start with the
new file, the previous one is put back automatically and the job fails with the
reason. A file edited on the host between opening the page and Apply is detected
by its revision and refused with a conflict instead of being overwritten.
Overlay content is never written to the audit log.

**Dashboard and access tiers.** Keycloak realm roles gate the UI in three tiers.
`MANAGER_ADMIN_ROLE` (default `manager-admin`) reaches every surface except
Users; `MANAGER_IDENTITY_ADMIN_ROLE` (default `papaia-admin`) sits above it and
also opens Users (see below); `MANAGER_USER_ROLE` (default `user`) reaches the
dashboard only. Accounts holding none of the roles are rejected at login. The
dashboard at `/` (titled **Overview**, the glossary's name for a module's start page) is a tile overview of the deployed applications, held in
`$PAPAIA_CONFIG_DIR/manager/tiles.yaml` and seeded with the applications the
stack ships today on first run. Administrators edit it in place: **Edit
overview** turns the page into an editor for groups and tiles, with drag and
drop reordering, a live preview of each tile, and one **Save changes** that
writes the whole file. Hand editing the file keeps working, but a save from the
UI rewrites the document, so comments are not preserved; a concurrent change on
the host is detected and refused rather than overwritten. The raw file is also
editable from the editor's overflow menu. The default tiles (LibreChat, LiteLLM,
Keycloak, NGINX Proxy Manager and Qdrant) show an icon that ships with the
manager; a tile of an older `tiles.yaml` without an icon gets it by its name, and
an `icon` set on the tile wins. An `icon` is a path on the manager's own site
(`/…`) or a `data:image/` value of at most 64 KiB, because the enforced content
security policy loads images from nowhere else (`img-src 'self' data:`); an
address on another site is refused on save, and a stored one (from a file
written before 1.8.0) is not shown: the tile falls back to its bundled icon or
the lettered badge, and the file stays as it is. The files, their sources and licences are
listed in `src/app/static/icons/NOTICE.txt`; the logos stay the property of their
owners (see [TRADEMARK.md](TRADEMARK.md)). `{{KEY}}` placeholders in tile links
resolve against the core `.env`, and each tile's `visibility: all | admin` is
filtered server-side, so an admin-only tile is absent from a regular user's
response rather than hidden by CSS. Authorization is enforced by the route
dependencies, so the JSON API is restricted exactly like the pages.

**Widgets.** Besides links, a group can hold widgets: cards that show live
state and link to the page that explains it.

| Widget | Shows |
|---|---|
| `backup-age` | how long ago the newest successful backup was taken, and whether the schedule says it is overdue |
| `disk-space` | the disk that needs attention most, with what is free of it |
| `services-health` | how many of the stack's services run |
| `open-warnings` | the number of open findings of the notification checks, and their kinds |
| `core-version` | the installed release, and whether the last upgrade check found a newer one |
| `cert-expiry` | days until the nearest certificate the host check sees expires |

In the editor, **Add widget** opens a gallery of what this installation can show
(a widget whose requirement is not met, such as the host check on a core without
`doctor`, is listed with the reason and cannot be added), then a step with a live
preview where you choose a title, a width (small, medium or the full row) and who
sees it. A widget is removed from its menu; **Undo** in the bar brings it back
until you save. Each card refreshes on its own, every 30 to 60 seconds and not
while the tab is hidden, so a slow source never holds up the rest of the
dashboard. A card that cannot read its data says so: administrators are told why,
everyone else gets a plain sentence. Visibility works as for links, a widget set
to administrators is absent for the user role and its card answers 404, and the
cards read what the manager already knows (the backup catalogue, the host check,
the container snapshot and the last upgrade check) instead of starting
measurements of their own. In `tiles.yaml` a widget looks like this; a tile
without a `type` is a link, as before, and a widget without a `visibility` is for
administrators:

```yaml
- type: widget
  widget: backup-age   # one of the ids above
  id: 3f9c1a0e         # assigned on save; names the card the browser fetches
  title: Backups       # optional, defaults to the widget's name
  size: m              # s | m | l: one column, two, the whole row
  visibility: admin    # all | admin
```

**Users.** A third tier sits above the admin role: `MANAGER_IDENTITY_ADMIN_ROLE` (default `papaia-admin`, which already includes the admin role) opens **Users**, where accounts of the bundled Keycloak are managed without the Admin Console. The list shows each account's status and roles, with search and paging. From it an administrator creates an account (with the roles to start from, the dashboard role ticked, and a first password: a temporary one shown once, or a mailed link), enables or disables it (disabling also ends its sessions), changes its roles, resets its password, and views and ends its sessions. The **Roles** tab manages the roles themselves: it lists every realm role with what it contains, creates a role, changes its description and the roles it contains, and deletes it after showing which accounts hold it. The roles the stack ships (and the three the manager is configured with) are shown but not changed or deleted, because services refer to them by name; a role cannot be renamed, and cannot contain itself, directly or through another role. A temporary password makes Keycloak ask for a new one at the first sign-in. The mailed link needs a mail server on the realm and is offered only when there is one. Nobody can disable their own account, take the identity role from themselves or end all of their own sessions here, so the page cannot lock its last administrator out. There is no service account behind it: the manager calls Keycloak's Admin API with the access token of the signed-in account, obtained from the session's refresh token and kept in memory only, so Keycloak applies that account's own rights. The identity role therefore has to carry `manage-users`, `view-users`, `query-users`, `view-realm` and `manage-realm` of the realm's `realm-management` client (the last is what Keycloak asks for to create, change or delete a role); where it does not (yet), the page says so. A session ended in Keycloak stops working in the manager when its token is next renewed, within a few minutes. Every change is audited and no password is ever written to the log. The page does not exist where the accounts live in an external identity provider (`AUTH_PROVIDER=external_oidc`).

**Mail.** Administrators open **Mail** (System), which has two tabs: **Notifications** (the page the menu entry opens) and **Mail Server**.

**Notifications.** The first tab is where the manager tells people when something needs attention. A recipient is one email address or one webhook URL, and each recipient ticks which events it receives, in a matrix of recipients and events that saves at once. The events: a **job failed** (a job in the manager's queue ended with an error, reported when it ends); the **backup is overdue** (the newest good backup is older than the backup schedule allows, so it needs a schedule); a **service is unhealthy** (a service a check saw brought up is now stopped, unhealthy or gone, also after you stopped it in the manager, with or without “Clean up”; a check runs right after every job in the manager ends; at most once per debounce window for each service); an **update is available** (a newer papAIa release, or a newer version of an installed add-on in a catalog on disk; catalogs are refreshed by hand, so an add-on update shows up after the refresh); a **certificate is expiring** (within the lead time, among the certificates the host check sees); an **AI map finding** (a new or changed high finding of the AI map that no exception covers, from the core's render or from the manager's check of the models against LiteLLM; only while the `knowledge-map` profile runs). The check interval (default 5 minutes), the debounce (30 minutes) and the lead time (14 days) are the same for every recipient. Each event is reported once per occurrence, and the manager remembers what it reported (`manager/notifications-state.json`), so a restart sends nothing twice. Email goes through the mail relay: while the Mail Server is not set up, the tab says so and email recipients wait, and webhooks keep working. A webhook gets a JSON `POST` of `{event, severity, message, timestamp, details}`; redirects are not followed and a network error or a 5xx answer is tried once more. Its URL is kept in `manager/notifications.yaml` (mode 0600, part of the configuration directory's backups) and is never shown again after it is saved, only its host. **Send test** checks a recipient, and the tab lists what was sent lately. A notification carries no job log and no error text, only what is needed to find the job. Restores, upgrades and whole-stack actions run outside the job queue and are not reported.

**Mail Server.** The second tab sets up the stack's outbound mail relay, the optional `smtp` profile of papAIa 1.6.0 and newer. One internal relay holds the credentials of your mail server and forwards to it, and Keycloak, and add-ons that attach it, send through it. The page edits the mail server behind the relay, with the relay on or off: host, connection security (STARTTLS, TLS or none), port, login, password, sender address and sender name. The password is write-only: it is never shown, an empty field keeps the stored one, and a stored password is only used for the host, port and login it was stored with. **Save and apply** is a queued job: the core's own `setup` stores the values (so its validation, sticky values and password revision stay in one place), and while the relay is on `papaia-ctl start --profiles=smtp` recreates it and Keycloak's realm gets the relay as its mail server, so the Users page offers "email a link" from then on. With the relay off the button is **Save settings** and only stores the values. The relay is switched on and off in Settings > Modules. A job whose last step could not reach Keycloak fails with that reason instead of reporting success. **Test connection** talks to the mail server with the values in the form (greeting, STARTTLS or TLS, login) without sending a mail or saving anything, and **Send test mail** hands one message to the relay: "sent" means the relay took it, not that it arrived, because the relay queues and retries for a day. The page shows whether the relay runs and how many messages wait in its queue. Switching the relay off (in Settings > Modules) stops it, takes it out of the stack's profiles and removes the mail server it put into the realm; the settings and the password are kept unless you choose to delete them there. The page needs a core with the relay and tells you so otherwise.

**RAG system.** When the core runs its optional RAG system (the `rag` profile
in `COMPOSE_PROFILES` of the core `.env`), administrators get two more things.
The dashboard shows a **RAG** group with a *Qdrant* tile (the vector database's
dashboard), and the sidebar gets a **RAG** category between *Modules and add-ons* and
*System* with the pages described below: *Vector databases*, *Collections*,
*Embedding* and *Ingest Jobs*. The ingest service's own web interface (removed in
`qdrant-ingest` 1.0.0) has no tile and no menu entry: what it offered is on those
pages.
The link of the tile is built from `QDRANT_PUBLIC_URL` in the core `.env`. The
profile decides, not that key: the core keeps it while the system is switched
off, so a core without the profile, such as 1.4.0, shows nothing new. The tile
is administrator-only, because the Qdrant dashboard bypasses the MCP server's
role checks. It is computed when the dashboard is rendered and never written to
`tiles.yaml`, so it appears on an existing deployment too, and the tile editor
neither lists nor saves it; the price is that it cannot be reordered or removed
there. A tile of your own with the same name or link wins, and a group you call
*RAG* receives it.

The RAG pages need a core that ships the `rag` profile (papAIa 1.5.0 or newer). Their
ingest features need `qdrant-ingest` 1.0.0 or newer, the first release that reports
the features the Embedding and Ingest Jobs pages adapt to (see the end of *Ingest
jobs*); an older ingester keeps working, with the parts it lacks left out.

**Vector databases.** An admin-only page at `/connections`, first under *RAG* in the
sidebar and only while the `rag` profile is active, for the vector databases the
RAG system works with. A connection is a name, a type and the values that type
needs; Qdrant, the only type so far, takes an address and an optional api-key.

Connections are kept in the ingester's own store, so the ingest service uses
them without any change (the web interface of `qdrant-ingest` up to 0.3.0 edited
the same file; 1.0.0 has none): `ai/rag/catalog/connections.yaml` in the config
directory, `version: 1`, entries of `name`, `url` and an `api_key` stored as
`enc:1:` plus a Fernet token derived from `QI_CONNECTIONS_SECRET` in
`ai/rag/.env`. Nothing but those three keys is
ever written, because the ingester rejects an entry with any other key. The
connection named **default** is the integrated Qdrant (`http://qdrant:6333`, with
the stack's api-key `QDRANT_JWT_SECRET`). It is created when the manager starts
and on first use, never overwritten, and marked as the default. Its address and
key can be edited and an action resets it to the integrated Qdrant, but it cannot
be renamed or deleted. The other connections are created, edited, tested and
deleted on the page. Every change is audited (`rag.connection.*`), without the key.

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
of showing a list. Every change is audited (`rag.collection.*`).

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
it. It needs an ingester with the `delete_vanished` run option (`qdrant-ingest`
1.0.0 and newer) and says so when the ingester is older. *Replace the collection*
drops everything in it and fills it from the selection (its roles and its model record stay; it is also how the
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
write; the previous file is kept as `jobs.yaml.bak`. The ingester is reached on the
stack's network at `QDRANT_INGEST_URL` (default `http://qdrant-ingest:8300`) with the static
`QI_API_TOKEN` from `ai/rag/.env`. The documents folder must be where the core
puts it (`ai/rag/documents`) or inside the configuration or workspace directory:
a `QI_LOCAL_MOUNT` anywhere else is a folder the manager cannot see, and the page
says so. The limits are `INGEST_MAX_UPLOAD_MB` per file (200) and
`INGEST_MAX_BATCH_MB` per upload (2048). A backup of the configuration directory
contains an upload that is still staged while it runs. Uploads and runs are audited
(`rag.ingest.upload.*`, `rag.ingest.run.start`) with counts and ids, never file names.

**Ingest jobs.** Admin-only pages under `/ingest/`, fourth under *RAG*
(*Ingest Jobs*) and only while the `rag` profile is active, that
take over from the web interface `qdrant-ingest` had up to 0.3.0. A **job** says
what the ingester reads (a folder in the documents folder, S3, WebDAV, SFTP, SMB, FTP, Google Drive,
Azure Blob Storage or a web directory), which collection on which connection the
result goes to, what a run does with files that are new, changed or gone, and when
it runs. Jobs are kept in `ai/rag/catalog/jobs.yaml`, the file the ingester reads,
and the page is one more editor of it. Five tabs run across the top: *Jobs*,
*Runs*, *Credentials*, *Leftovers* and *Job file*.

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
prefix `mgr-`) are listed and say where they come from, but are not edited or paused
here; they can be deleted, and the Embedding page writes them again with its next run.

**Runs.** *Run* opens a dialog: a dry run (fetch, read and plan, write nothing; it is
preselected for a job that never ran), and the mode, either as the job is set up or
add only, keep in sync, rebuild the job's content (asks for confirmation) or replace
the whole collection (asks for the collection name), with a few more options such as
not fetching again. A run shows its phase (fetching the files, looking at them,
embedding them, removing what is gone), how many files are done, the file in hand, and *Abort*, which takes effect
between two files. When it ends it shows its counts, the ingester's messages
(problems first), the output of a failed fetch and a way forward for each way a run
can stop. The *Runs* tab (*Ingest Runs*) lists the runs of all jobs, filtered by job and
state. Both poll only while a run works.

**Deleting runs.** *Delete* on a job removes it from `jobs.yaml` and, in the same
step, its runs and their logs: the dialog says so and shows how many, because a job
that is gone never runs again and the ingester prunes a job's history only after one
of its own runs. The runs go once the ingester has stopped serving the job; if it
still serves it (it keeps its previous catalog while another job is invalid), or
cannot be reached, they are kept and the result says why. *Delete runs* in the same
menu, and on the *Ingest Runs* page, deletes the history of one job for a period: a
first and a last day (both included, counted in the ingester's time zone) or, with
neither, every run, which asks for the id of the job. It counts what it would delete
before it deletes anything, never touches a run that is still working, and never
touches what the job embedded. The page of a job that is gone is reached from the
job column of a run, which is how the runs of a job deleted earlier are cleaned up.
The time of a job's last run changes with it, so a job that is set to run when it was
missed starts again at the next start of the ingester once no successful run is left.

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
offers to remove its content in the same step. *Job file* has the defaults every
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
`rag.ingest.run.delete`, `rag.ingest.orphan.delete`, `rag.ingest.secret.*`, `rag.ingest.defaults.update`,
`rag.ingest.catalog.raw`); a credential's value is never part of an entry.

These pages use what the ingester reports it can do (`features` in its `/health`):
`run_progress` (phase and progress of a run, dry runs), `documents` (the files of a
job), `validate` (its rules for a job that is not saved yet), `delete_runs` (deleting a
job's history) and `secret_store` (the credentials file). An older ingester keeps
working with each part missing and an explanation where it would be: no progress, a
files tab that says so, the manager's own checks, no way to delete runs (deleting a
job leaves them in its history), and no stored credentials. `qdrant-ingest` 1.0.0 reports
all five; 0.3.0 has none of them.

**Docker access.** Whoever can talk to the Docker socket is root on the host, so the
manager does not mount it. It talks HTTP to `papaia-docker-gate`, the core's socket
broker, which runs with the `manager` profile on an internal network that holds the
gate and the manager only (`DOCKER_GATE_URL`, default `http://papaia-docker-gate:2375`).
The gate allows the status of the containers of the installation's own compose project
and `start` / `stop` of those containers; it refuses everything else (`exec`, creating
containers, other projects, images, volumes) with 403 and writes an audit row for every
start, stop and refusal. The manager sends the correlation id of the request or job
along (`X-Papaia-Correlation-Id`), so the gate's rows and the manager's share it.

Running the manager without the gate is **not supported**: the services page then says
that the gate does not answer and that running without it is not supported, and the
group actions answer 503. What needs more than the gate allows is `papaia-ctl` on the
host: creating containers (a service that was never started, `--clean-up`), add-on
projects, which the gate does not name, image clean-up and Docker's disk use.

**Services.** An admin-only page at `/services` showing what this deployment is
configured to run and how much of it is up. Containers are read through
`papaia-docker-gate` and grouped by the `de.fidonis.module` label the Compose files
put on every service, so each module lists its own containers with their role, uptime
and healthcheck result (the gate does not hand out published ports); the module in the
worst state sorts to the top. A container without a healthcheck counts as healthy while
it runs, and a one-shot container that has finished its work is reported as completed
rather than dragging its module down — recognised by the restart policy its Compose
file declares, so a service that was shut down still reads as stopped even though it
exited just as cleanly.

Live containers alone cannot say what is *missing*, so the page reads the
declared state next to them. For the core stack that is the Compose fragments
listed in `papaia/src/docker-compose.yml`, filtered by the profiles enabled in
`COMPOSE_PROFILES`; for add-ons it is the active entries of `deployment.yaml`
and their own Compose files. A declared service with no container renders as
*not deployed* instead of being absent, which is what separates "LocalAI is
configured but was never started" from "this deployment has no LocalAI". A
torn-down stack therefore reports every module rather than an empty page —
`papaia-ctl down` removes containers, it does not stop them. An unreachable
gate still reports nothing at all: not knowing is not the same as
knowing it is gone; the page shows why there is no reading.

Add-ons appear in their own section below the core stack. They run in a separate
Compose project each, but use the same labels, so they group into modules exactly
like core services do. The gate shows only the projects it names (the installation's
own); an add-on project with no container in its answer reads as *not visible through
papaia-docker-gate*, not as missing. Bear in mind that few add-ons define healthchecks, so a
green add-on module says only that its containers are running.

**Service groups.** The page is also where the stack is started and stopped. The
unit is the Compose profile — a *service group* — because that is the only
granularity `papaia-ctl` accepts; there is no per-container verb. Each module
header carries its profile and a checkbox, and ticking one module ticks every
module the same profile brings up: `librechat-websearch` alone covers Firecrawl,
SearXNG, Jina and the Firecrawl MCP server, and stopping one without the others
is not something Compose can do. Several groups can be selected and started,
stopped or restarted in one run. On a single host this goes through
`papaia-docker-gate`, one container at a time: a stop stops the group's running
containers, a start starts its stopped ones, a restart does both. A service of the
group without a container cannot be created through the gate; the job says so and
names `papaia-ctl start` on the host. In a distributed installation the groups still go
through `papaia-ctl` with the node that owns them.

Stopping leaves the containers in place. Every confirmation dialog that stops
something — a stop, and the stop half of a restart — offers to remove them as
well (`--clean-up`, i.e. `docker compose down` instead of `docker compose stop`).
The gate does not remove containers, so on a single host the API answers that with
409 and points to `papaia-ctl stop --clean-up` on the host.
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

The same data drives a status chip in the status row at the top of every page, visible
to every authenticated account regardless of role. Its popover keeps three rows apart: the
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
measurement leaves the rest of the page as it is. For administrators the status row also
says "Host needs attention" while a check warns or has failed, and in the same way when a
job is running, an add-on upgrade or a newer papAIa release is available.

**Models & Usage.** An admin-only page at `/models`, in the *Monitor* group after Host, that
shows which models LiteLLM serves and what they consumed. A window of days is chosen at the top:
the last 7 days (the default), 30 or 90 days, this month, or a custom range of up to a year. The
**Overview** tab adds up all models: tokens (with input and output), spend in USD and requests,
as a bar per day stacked by model, with a switch between the three, a tooltip per day (also with
the arrow keys), a legend and each model's share. The **Per model** tab lists every model
LiteLLM serves, including the ones nobody used, with its type, its price per million tokens, its
figures and a small trend line, and a click opens a page for that model with input and output
tokens apart. A table of the days is under every chart. The page only reads: models, prices,
teams, budgets and keys are changed in LiteLLM's own Admin UI, which the page links to when the
stack knows its address. Days are UTC days, because that is the finest grain LiteLLM keeps, and
the newest requests can take about a minute to show up. A model without a price in LiteLLM (a
local one) counts tokens and requests but no spend. The manager reads LiteLLM at `LITELLM_URL`
(default `http://litellm:4000`) with the master key from `ai/litellm/.env`, which it never shows,
logs or sends anywhere else; a LiteLLM that is down, or a key it refuses, is a message on the
page and not an error. A reading is kept for a minute and shared between everyone who has the
page open.

**AI map.** An admin-only page at `/knowledge-map` with its own sidebar entry, shown while the
core runs its `knowledge-map` profile (without it, the page links to **Settings > Modules**, where the AI map is switched on). It shows which data the AI reaches, for whom, over which access path
and with which model. The **Map** tab draws the rendered map as a layered graph (topics,
sources, access paths, agents) and can show it as a chosen role; the **Entries** tab lists every
entry with all its access paths and their roles; **Findings** lists what the core's render
reports. **Test routing** asks the map server which source would answer a question for a set
of roles, without recording the question. **Usage** shows what the map server records (its
learning mode, how long it keeps records and how many distinct profiles a need must come from
before it is listed) and the gold-set runs (the **Usage & quality** tab); **Suggestions** offers refinements the map server
mined from use, to accept or reject: a suggestion that carries text people asked is only
accepted with the confirmation that it names no person. **New entry** and **Edit** open an
entry as a form (what it is, topics and which of them it is authoritative for, example
questions, "not here, but there", access paths, who may know it exists) with a quality checklist
and a probe that asks the map server where each example question would land with the entry as
it stands in the form, each question tested without itself among the examples; nothing is
saved or recorded by the probe. An entry of your own is saved in `sources.yaml` and can be
removed; an entry an add-on, a module or the inventory declares is saved as an override of
only the fields that differ, each changed field marked and resettable to the declaration. An
undescribed MCP server opens as a new entry for the source behind it. **Files** edits the operator files under
`overlay/knowledge-map/` (own entries, overrides, gold set). The **Governance** tab documents
owner, classification, personal data, data subjects and the record-of-processing reference of
each source and the tier of each model (`overlay/knowledge-map/governance.yaml`), confirms an
add-on's proposal or a model's suggested tier with one click, lists the agents with their model
tier and what they reach, and shows the findings with what to change, each acceptable until a
date; the render evaluates the rules, the page shows its results in the reader's language and
adds a live check of the model tiers against LiteLLM. The facts can be exported as
`facts.json` or `facts.csv` for a GRC tool, a CMDB or a wiki. Every change is a queued job that
checks the file, writes it, renders the map and puts the previous file back when the render
refuses it. The page reads the map from the configuration directory and the rest from the map
server's admin API at `KNOWLEDGE_MAP_URL` (default `http://knowledge-map-mcp:8200`) with the
bearer `KM_ADMIN_TOKEN` from `ai/knowledge-map/.env`, which it never shows, logs or sends
anywhere else; a map server that can't be asked is a message on the page. Neither the map nor
the page restricts data access or model use; the target systems, LiteLLM and the chat's agent
bindings do.

**Catalogs.** A catalog is a source of add-ons — a public or private Git
repository, or a local directory — registered at runtime (not versioned in
this repo). Each catalog is scanned for top-level `papaia-app.yaml`
manifests. Installing an add-on materializes a pinned snapshot
(`git archive` at a specific commit) so that a later catalog refresh never
moves code out from under a running container. Every version gets a
directory of its own, `addons/_managed/<catalog>/<revision>/<add-on>`, which
is written once and never changed; the revision is the first twelve digits of
the commit (`local-<timestamp>` for a local catalog). Of one add-on the
directory it runs from, the one a rollback goes back to and one more stay on
disk, and an uninstall removes them all.

**Status model.** Each add-on resolves to one of five states, merged from
the catalog scan, `deployment.yaml`, and live Docker container labels:
`available`, `installed`, `running`, `inactive`, `unmanaged` (an
operator-managed checkout outside the manager's own snapshot directory).
If the same add-on name exists in more than one enabled catalog, each
distinct version is surfaced as its own entry rather than one silently
hiding the other.

**Add-on components.** An add-on's own version counts changes to its bundle,
not to the software in it. When the catalog's manifest lists the bundled
`components`, the gallery card names the primary ones (`Paperless-ngx 3.0.5`,
or `3.0.5 → 3.1.0` when an update changes one) and the detail page has a table
of all of them: role, version, the version an update brings, a badge for a tag
the catalog leaves floating and one for a tag the operator pinned in the
add-on's `.env`. The pinned tags are read from that file, so the pages need no
Docker. The list is also `components` in the JSON of an add-on. A catalog whose
manifests have no `components` shows nothing new.

**Requirements.** An add-on can require other add-ons (`requires.addons` in its
manifest, by name or with a version range; papAIa 1.9.0 and newer). The gallery card
and the add-on page show each requirement as a badge that links to the required
add-on and says whether it is active, inactive, missing, too old or could not be
checked. While one is not met, *Install* and *Reactivate* are disabled with the reason;
in the core's warn mode the button stays usable, because `papaia-ctl` then installs
anyway. The other direction is shown too: the page of a required add-on lists the
active add-ons that require it under **Required by**, and while there are any, *Remove*
and *Uninstall* are disabled and refused, because `papaia-ctl` would refuse them as
well. `papaia-ctl` stays the gate: what the manager cannot read never disables a button.

**Maintenance actions.** An add-on can declare maintenance actions in its manifest
(`actions`; papAIa 1.9.0 and newer), for example a re-index or a reset. While the add-on
is installed and active, its page lists them as buttons under **Maintenance actions**
with their descriptions, and a click runs `papaia-ctl addon run <add-on> <action>` as a
job. A destructive action is outlined in red and asks for confirmation first; when the
job ends, the page reports the outcome with a link to its log. The request names the
action only, never a command, and an action the manifest does not declare is refused.
A distributed installation offers no actions, since the core does not run them there.

**Jobs.** Installs, upgrades, and lifecycle verbs that shell out to
`papaia-ctl` can take minutes (image pulls, container starts). These run as
queued `Job` objects processed one at a time by a single worker; the UI
polls for live status and streamed log output. A line of output longer than
1 MiB is cut and ends in `... [truncated]`, so a progress meter that never
prints a newline cannot grow the job log without bound.

**Remove.** *Remove* takes an add-on's containers down and then marks it
inactive; its volumes and configuration stay. `papaia-ctl addon remove` alone
only marks it inactive and leaves the containers running, so the job stops them
first (`addon stop --clean-up`) and leaves the add-on active if that fails. It is
refused (409) while the add-on's `docker-compose.yml` is missing: `papaia-ctl`
then aims its Compose call at the core project instead of the add-on's. It is
also refused (409), like *Uninstall*, while an active add-on requires it, and the
job checks that again before it stops anything.

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
taken beforehand. A successful upgrade can still have skipped a provisioning step
at the end of its start (the NPM proxy hosts or the Keycloak role sync), because
papaia-ctl only warns about those; the result panel then lists each such step with
the line papaia-ctl printed and the command that runs it again on the host. The
runner logs carry stderr as well as stdout, so these warnings reach the panel.
The panel reads the end of the runner's log; when the image pull fills that part,
the phases before the first one shown count as done ("earlier than the part of the
log shown"), and a backup skipped with `--no-backup` still reads as skipped.

Upgrades leave the previous versions of the stack's Docker images behind. The
upgrade dialog offers to remove them once the upgrade has succeeded (selected by
default), and the page lists whatever is still there afterwards — for the core
stack and the installed add-ons — so it can be removed later. An image counts as
outdated only if no container uses it, the installed release does not declare it,
and it belongs to a repository the stack declares; unrelated images are never
touched, and nothing is offered while the declared images cannot be resolved.

**Upgrades.** An add-on is offered an upgrade when a catalog on disk has a
higher version than the one installed (SemVer precedence; a version that is not
SemVer counts when it differs). The Add-Ons page counts them, lists them with a
link to the review (and can narrow the cards to them), and marks the Add-Ons
entry of the sidebar; **Refresh
catalogs** fetches every Git catalog first, because the page reads the
catalogs as they are. **Review upgrade** (`/addons/<name>/upgrade`) stages the
catalog's version in a directory of its own and shows what `papaia-ctl addon
upgrade --dry-run` reports: the versions, whether this core accepts the new one,
the components that change, the new configuration keys (generated, asking for a
value, or a Keycloak client secret), the keys that are gone or whose default
changed, new services and Keycloak clients to set up by hand, and the sections
of the add-on's own `CHANGELOG.md` for the versions in between (as plain text,
when it has one). A value the new version asks for is entered there. The upgrade itself is a job that runs
`papaia-ctl addon upgrade`: it takes a restore point of the add-on first (the
**Take a restore point** box; an add-on without volumes gets none), repoints
the add-on to the new directory, adds the new keys to its configuration and
recreates its containers when it was running. The upgrade runs against the
version that was reviewed, and is refused if the catalog has changed since. A
refusal of the compatibility gate or a missing value is an error of the
request, not a failed job; values reach papaia-ctl through a private file that
is deleted when the job ends, never on a command line. There is no automatic
rollback. When the new version has been put in place the add-on page lists the
version before it, and **Roll back** offers two ways back: the code and
configuration only (the data stays as it is), or the code and the data of the
restore point taken before the upgrade (what the add-on wrote since is lost).
That restore point holds the add-on only: on the Backup / Restore page it is
marked as such, can be restored by selection only and does not count as the
last backup of the stack.
It is one step: afterwards there is no earlier version to return to. Upgrading
and rolling back need papAIa core 1.8.0 or newer; an older core shows a hint
instead.

**Nodes.** On a distributed installation (papAIa 1.8.0 and newer, an experimental mode of the core) an admin-only `/nodes` page lists the machines with their services, address, status, version and link health. A node that does not answer stays in the list and says so. Adding a node registers it and writes its join archive; the page shows the commands to run on the node and the host keys it offers, for the administrator to compare and confirm. Removing a node and confirming a host key are on its detail page. Each of these changes needs a fresh sign-in at Keycloak, valid for five minutes, on top of the admin role, and each is audited. On a single host the page and its sidebar entry do not exist. The image carries an SSH client for this, and its entrypoint gives the host's user id a name, which OpenSSH requires; the keys stay in the configuration directory, managed by the core.

On such an installation the other pages follow the nodes. Services has a section per node, read from the core's merged `status`, and names a node that does not answer; an action on a group runs on the node that owns it. Host offers a node selector and shows the core's `nodes` and `links` checks. The dashboard's service, disk and certificate cards look at every node and name the one that needs attention, and a node without a reading keeps a card from saying everything is fine. Modules, the overlay editor and the mail settings name the node a module runs on, and a change runs on that node and then on the nodes of the proxy and of Keycloak, whose provisioning steps add proxy hosts, roles and the realm's mail server. A failure on one of those two nodes does not undo the change, which is in place on the module's own node: the job ends with a warning that names the node and the `papaia-ctl start --node=<node>` that catches it up. The mail relay is the exception for Keycloak's node, since giving the realm its mail server is what its start is for; only the proxy's node is skipped there. Switching a module off keeps its data and its settings there, since the core does not delete volumes across nodes yet; the dialog offers no deletion and the API refuses `data` and `all`. The mail relay's state comes from its node's health reading, so the Mail page adds no `doctor` run of its own. Users & Roles and Models & Usage keep dialling `keycloak:8443` and `litellm:4000`, which the link gateway carries.

Backups keep going to the controller, as in the core: the Backup page can back up one node or all, and a restore can pick a remote node as a whole, which touches no other node. The page says that a restore point is not one moment across nodes. An upgrade moves all nodes through the same detached runner; the Upgrade page shows the release per node and each node's latest step. While an upgrade has not finished or a node runs another release, every admin page says so, and starting the stack or a backup is refused with that reason. `stop`, the upgrade and a restore stay possible.

**Audit log.** An admin-only `/audit` page lists every entry from the audit log,
filtered by user, action, result, a target substring and a date range, newest
first and paginated, with the filter facets drawn from the log itself. The same
filters drive `GET /api/v1/audit/export` (CSV or JSONL). `POST /api/v1/audit/prune`
permanently removes entries older than an operator-chosen cutoff date behind a
dry-run preview; the prune itself is recorded as its own audit entry.

## Content security policy

Every HTML response carries this policy, **enforced**:

```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self';
  img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self';
  form-action 'self'; frame-ancestors 'self'
```

No directive allows `unsafe-inline`, `unsafe-eval` or `unsafe-hashes`, and there is no
exception for `style` attributes: the browser runs only the manager's own script files, applies
only its own stylesheets, and lets only the manager's own host frame a page. A script that gets
into a page by way of a value (a display name, a collection name, the text of a notification)
is not run. There is no report endpoint, so the policy adds no input to the server. A
response with an enforced policy of its own keeps it (the logo at `/_kit/brand/logo`). The
middleware is `src/app/csp.py`. Images load from the manager's own site and from `data:` only,
so a dashboard tile's `icon` is held to the same rule (see **Dashboard and access tiers**).

**How to check it.**

- In a browser: open any page, then the developer tools. The response headers of the page show
  `Content-Security-Policy` as above (not `-Report-Only`), and the console shows no
  "Refused to ..." message.
- From the command line: `curl -sI -b 'papaia_manager_session=<cookie>' https://<manager>/ |
  grep -i content-security-policy` prints the policy line.
- In the repository: `tests/test_csp_policy.py` checks the header on every page of
  `ui/pages.yaml`, the no-access page and the partials; `tests/test_csp_zero.py` checks that
  no template has anything the policy refuses; `scripts/csp-check.sh` (below) opens every page
  in a browser in both colour schemes and fails on any violation; and
  `tests/test_csp_hostile_browser.py` puts markup and script into a display name, a collection
  name and a notification and checks that each is shown as text, with no dialog and no
  violation; it also opens the dashboard and the tile editor with tiles whose stored icon is on
  another site and checks that no such image is built.

**Rules for every page.** A page has no inline script, no inline event handler
(`onclick`, ...), no inline style and no Alpine expression that needs `eval`:

- Page code is an ES module under `src/app/static/js/pages/<group>.js`, shared helpers are
  under `src/app/static/js/core/`; both are served as they are from `/static/js/`, without a
  bundler. Every page built on `base.html` loads `js/core/main.js`. A page loads its own
  module in the block `page_modules` of `base.html`, which comes after `main.js` and before
  htmx and Alpine: modules and deferred scripts run in document order, so the stores and
  components a module registers on `alpine:init` exist when Alpine starts. A module imports
  what it uses (`fmt`, `csrfToken`, `readIsland`, ...) instead of reaching for the transition
  globals; the page's toaster, failure report and actions are the one instance of
  `js/core/page.js` (`pageCore()`), which `main.js` uses as well through `js/core/runtime.js`. `main.js` is never imported
  again, and it puts no name on `window`: the transition globals are gone.
- Global functions become methods of an Alpine store or of an `Alpine.data` component the
  module registers; `on*` attributes become Alpine event bindings. An expression is a plain
  call or property (no arrow function, template literal, several statements, `window`,
  `document`, or globals such as `Math`, `Number`, `Object`, `Boolean` or `JSON`), and
  `x-html` gives way to `x-text` or to nodes the module builds with `textContent`. Script a
  partial needs after an htmx swap runs from the module, through an `x-data` component's
  `init()` or `htmx:afterSettle`, never as script in the fragment.
- Server values reach a module as a JSON data island, never inside a script: the macro
  `data_island(id, value)` of `templates/partials/_csp.html` writes
  `<script type="application/json">` (escaped with `tojson`, so `<`, `>`, `&` and `'` never end
  it) and `readIsland(id)` of `js/core/islands.js` reads it. A partial that htmx swaps into a
  page uses `swapped_island(id, value)` instead, which writes
  `<template data-island="application/json">`: htmx drops every script element of a swapped
  fragment, data islands included, but keeps a template. `readIsland(id)` reads both.
- Alpine runs in its CSP build on every page, the only build the image ships: the macros
  `strict_marker` and `alpine_build` of `templates/partials/_csp.html` write the marker the
  browser check reads and the build.
- A page module that registers Alpine components (`Alpine.data` on `alpine:init`) is loaded
  in the block `page_modules` of `base.html`, after `js/core/main.js` and before Alpine; module
  and deferred scripts run in document order, so its components exist when Alpine starts.
  Expressions call plain methods of the component: no arrow function, template literal,
  `window`, `document`, `Math` or `JSON`. A sentence whose translation carries markup of its
  own is built with `fillMarked` of `js/core/marked.js` (its tags as elements, every value as
  text) instead of `x-html`. An element shown by `x-show` starts hidden with `x-cloak`
  (stylesheet rule) instead of `style="display:none"`; a size computed per render is a
  `data-*` attribute the module sets as a style property.
- htmx evaluates nothing (`<meta name="htmx-config">` with `allowEval: false`), runs no
  script of a swapped fragment (`allowScriptTags: false`) and injects no style (`includeIndicatorStyles: false`; the indicator rules are in the stylesheet). A poll
  that should pause while the tab is hidden carries `data-poll-visible` and the trigger
  `visibilitychange from:document` instead of an htmx trigger filter; `js/core/poll.js`
  cancels its requests while the page is hidden.
- Buttons of a page and of the partials htmx swaps into it name what they do in a data
  attribute (for example `data-backup-action` or `data-ingest-action`), and one listener of
  the page module on the document runs it, so a swapped row needs nothing run for it. The
  values of a row are `data-*` attributes. A dialog's cancel button names its dialog in `data-dialog-close`.
- What a page module shares with the page script (texts, toaster, report, actions) it imports
  from `js/core/runtime.js`.
- A sentence that carries markup and a value is rendered by the server instead of `x-html`:
  `bound(_('Roles of <span class="font-mono">{username}</span>'), username='username')`
  (`src/app/csp.py`) writes each value as `<span x-text="...">`, so the browser fills it in as
  text. The expression is a property path or a method call without arguments. The ingest
  pages do the same for one value with the macro `filled` of
  `templates/partials/_ingest_texts.html`.
- An element that `x-show` hides at the start carries `x-cloak` instead of
  `style="display:none"`; the rule is in `docker/tailwind.app.css`.

**Converted pages.** The dashboard (`dashboard.html` with the partials `tile_gallery.html` and
`tile_editor.html`): the editor is `js/pages/dashboard.js`, loaded for administrators only
together with Sortable. Its texts come from the island `dashboard-texts`, and the draft from
the island `tiles-editor` of the editor partial, which the component `tileEditor` hands to the
store `tiles` as the partial comes in. What the editor hides until Alpine starts carries
`x-cloak`. `tests/test_dashboard_scripts.py` checks the templates and the islands, and
`tests/test_dashboard_browser.py` the browser path in both colour schemes: the tiles load and
refresh, the editor adds, moves and removes a tile, the layout is saved.

Users, Roles and Audit log (`users.html`, `roles.html`, `audit.html`
with `partials/audit_list.html`; modules `js/pages/users.js`, `roles.js`, `audit.js`). The
audit list hands its filter choices to the page in the island `audit-facets`;
`tests/test_accounts_browser.py` walks the three pages in a real browser on the CSP build.

`kit_specimen.html` (`/_kit/specimen`), on the strict policy from the start. It
renders the kit's reference content only, so it loads neither `app.css`, htmx nor
`js/core/main.js`; it loads the CSP build of Alpine, and the browser check confirms that the
build starts on it without a report, refused expression or script error
(`tests/test_kit_specimen.py`). The Manager and Core features tabs of the settings
(`settings.html` with the branding and host partials, `settings_modules.html` with the job
strip it swaps in) run `js/pages/settings.js` and `js/pages/settings-modules.js`; their values
come from the islands `mgr-settings`, `mgr-settings-branding`, `mgr-settings-host` and
`mgr-settings-modules` and from `data-*` attributes, and the modules add their listeners
themselves (`tests/test_settings_scripts.py`, with the browser check of both pages and the
paths of saving the host interval, the branding, a logo upload and its preview, and switching
a module on and off in `tests/settings_browser.mjs`).
`knowledge_map.html` (`/knowledge-map`): its component lives in
`js/pages/knowledge-map/component.js`, registered by `js/pages/knowledge-map.js`; the styles
of its graph are in the stylesheet. `tests/test_knowledge_map_browser.py` walks the page in
Chromium in both themes (map, "View as", search, detail by click and keyboard, every tab)
and measures each of its Alpine expressions against the CSP build.
The release upgrade (`upgrade.html` with the partials it swaps in: status, check, runner,
images) runs `js/pages/upgrade.js`. The header button is the component `upgradeActions`; the
status partial announces what it found in the data island `upgrade-state` and the runner in
`data-active`, `data-running` and `data-finished` of `#upgrade-runner`, and the module turns
both into the window events the button listens for. The buttons of the partials name what
they do in `data-upgrade-action`, and the sentences of the confirmation are built as DOM nodes
with text, never as markup. The add-on upgrade (`addon_upgrade.html`) runs
`js/pages/addon-upgrade.js` with the island `mgr-addon-upgrade`; both use `x-cloak`.
`tests/test_upgrade_scripts.py` walks both pages in Chromium in
both themes: check for upgrades, start and follow an upgrade, dismiss it, choose a release,
remove outdated images, and review and apply an add-on upgrade.

**Connections** (`connections.html` with the list it swaps in, `partials/connection_list.html`)
and **Mail → Notifications** (`notifications.html`, `/mail`) run `js/pages/connections.js` and
`js/pages/notifications.js`. Their dialogs and the Test button of a connection row are
`Alpine.data` components of these modules; the sentences come from the islands
`mgr-connections` and `mgr-notifications` (which also carries the recipients and events), the
connection types from the island `mgr-connection-types` of the list. A translated sentence with
markup around a value is rendered by the server with `bound(sentence, name='expression')`
(`src/app/csp.py`): the value becomes `<span x-text="...">` and is filled in as text, which
replaces `x-html`. An element hidden until Alpine starts carries `x-cloak` (rule in
`docker/tailwind.app.css`) instead of `style="display:none"`, and the floating row menu of
`overflow_menu(float=True)` gets `position: fixed` from the stylesheet.
`tests/test_connections_notifications_scripts.py` checks the templates, islands and modules,
runs the browser check on both pages and walks the paths in
`tests/connections_notifications_browser.mjs`: add, test, edit and delete a connection; add,
tick, edit, test (to a webhook on the loopback) and delete a recipient, and save the event
settings, in light and dark.

`backup.html` (`/backup`) with the partials it swaps in (`backup_job_status.html`,
`backup_schedule.html`, `backup_schedule_preview.html`, `restore_point_list.html`,
`restore_status.html`): its script is `js/pages/backup.js`, with the restore wizard's
selection in `js/pages/backup-scope.js` (unit tests in `tests/js/backup.test.mjs`); its
sentences come from the data island `backup-texts`. The job strip carries its state in
`data-job-*` attributes, and the page module announces it as `papaia-job-state` whenever htmx
has settled a new strip. `tests/test_backup_browser.py` walks the page in Chromium in both
colour schemes (start a backup and follow its job, change the schedule, list and open restore
points, select and clear them, start and confirm a restore) in the stand-in world with
restore points (`tests/design_serve.py --with-backups`) and fails on any report of the
policy, refused Alpine expression or script error.

The ingest lists: `ingest_jobs.html` (`/ingest/jobs`), `ingest_job.html`
(`/ingest/jobs/{id}`), `ingest_runs.html` (`/ingest/runs`), `ingest_run.html`
(`/ingest/runs/{id}`), `ingest_orphans.html` (`/ingest/orphans`) and `ingest_catalog.html`
(`/ingest/catalog`), with their dialogs (`partials/_ingest_dialogs.html`) and the partials
htmx swaps into them. Their script is `js/pages/ingest.js`; the verbs and dialogs are in
`js/pages/ingest-components.js` with unit tests in `tests/js/ingest.test.mjs`. Each page hands
its module the data island `ingest-page` (macro `ingest_island` of
`partials/_ingest_texts.html`): which page it is, its values and the sentences of the verbs.
Buttons name their verb in `data-ingest-action`; a row opens a dialog with `$dispatch` and what
the dialog needs to know. `tests/test_ingest_browser.py` walks the pages in Chromium in both
colour schemes (filter the jobs, disable and enable one, start a run and abort it, delete runs,
switch the tabs of a job, delete a job, remove a leftover, save the defaults and check the job
file, a list that cannot be loaded) against the fake ingester of the page tests
(`tests/design_serve.py --with-ingest`) and fails on any report of the policy, refused Alpine
expression or script error.

The job editor (`ingest_job_edit.html`, `/ingest/new` and `/ingest/jobs/{id}/edit`) and the
credentials page (`ingest_secrets.html`, `/ingest/secrets`, with the list
`partials/ingest_secret_list.html`), which share the credential dialog
`partials/_ingest_credential_dialog.html`: their script is `js/pages/ingest-editor.js`, with
the editor and the two credential dialogs in `js/pages/ingest-editor-components.js` (unit tests
in `tests/js/ingest-editor.test.mjs`); the request helpers and the save outcome are imported
from `js/pages/ingest-components.js`. Each page hands its module the data island
`ingest-editor` (macro `editor_island` of `partials/_ingest_texts.html`): which page it is, the
editor's start values and the sentences. The buttons of the page frame name their verb in
`data-ingest-editor-action`. The same browser walk then creates a job in the editor, edits one
(a link out of unsaved changes asks in the app's dialog), stores a credential from the editor,
and stores, replaces and deletes one on the credentials page.

Embedding (`embedding.html` with the body, tree, uploads and run strip partials), Collections
(`collections.html` with `partials/collection_list.html`) and Models & Usage (`models.html` with
`partials/models_body.html`) run `js/pages/embedding.js`, `js/pages/collections.js` and
`js/pages/models.js`. Their sentences come from the islands `embed-texts` and
`collections-texts`, the state of the Embedding body from the island `embed-state`; the run
strip announces whether its run works as `papaia-embed-run` (`data-announce`); the chart's
tooltips are its `data-tips` attribute and the bar geometry its `data-pct-*` attributes
(`tests/test_rag_page_scripts.py`). `tests/test_rag_browser.py` walks the three pages in
Chromium in both colour schemes (create and delete a collection, replace a collection with
another embedding model and abort the run, read the chart by pointer and keyboard) against the
stand-in RAG system of `tests/rag_serve.py` and fails on any report of the policy, refused
Alpine expression or script error.

**Zero rule.** `tests/test_csp_zero.py` counts per template the executable inline scripts,
`on*` attributes, Alpine attributes the CSP build rejects, `<style>` blocks and `style`
attributes (`tests/csp_scan.py`); every count of every template must be zero.
`python3 tests/csp_scan.py` prints the templates with a finding and exits 1 if there is one.
It replaced the ratchet of the conversion, which started from 46 inline scripts, 123 `on*`
attributes, 109 Alpine attributes the CSP build rejects, 1 `<style>` block and 253 `style`
attributes.

**Browser check.** `scripts/csp-check.sh PORT REPORT [SUMMARY [LABEL=FILE ...]]` (CI job
`csp`, after `scripts/design-tools.sh`) serves the manager in the stand-in world of the tests,
signed in as the test person of each page, and opens every page of `ui/pages.yaml` in
Chromium, in the light and the dark colour scheme. A page fails on any violation of the
policy (`securitypolicyviolation`), refused Alpine expression or script error, and when it
answers without the enforced policy; the counts are written to the summary. The pages of the
list render every full page of the template folder (`tests/test_csp_policy.py`). Each
`LABEL=FILE` is an Alpine build:
every Alpine expression of every page is evaluated by that build in a blank page, and the
expressions it cannot evaluate are counted per page.

**Choice of the Alpine CSP build.** Measured on 23 November 2026 with that check over the 39
pages of `ui/pages.yaml` (1,909 Alpine attributes in the templates): the CSP build of the
newest release, `@alpinejs/csp` 3.17.5, needs 45 distinct expressions rewritten (53 summed
over the pages), the CSP build of the release the manager used before, 3.14.9, needs 489
(637); the standard build 3.17.5, as a control, rejects none. 3.14.9 accepts property and
method names only, so every comparison, assignment, call with arguments and object literal
would have to move into a component; 3.17.5 parses expressions and refuses `x-html`, arrow
functions, template literals, global objects, statement sequences, `new`, `typeof`, optional
chaining and methods inside `x-data` objects. The image therefore ships `@alpinejs/csp`
3.17.5; the standard build left the image when the policy was enforced.
The forms `tests/csp_scan.py` counts are the ones the 3.17.5 build refused; over all 1,909
attributes of the templates the scanner and the build agree on all but one, an index the
measurement's stand-in values cannot resolve.

| Page | `@alpinejs/csp` 3.14.9 | `@alpinejs/csp` 3.17.5 |
|---|---:|---:|
| `/services` | 33 | 3 |
| `/addons` | 3 | 1 |
| `/addons/{name}` | 1 | 0 |
| `/addons/{name}/upgrade` | 37 | 0 |
| `/modules` | 5 | 0 |
| `/catalogs` | 6 | 2 |
| `/embedding` | 15 | 3 |
| `/ingest/jobs` | 39 | 8 |
| `/ingest/new` | 189 | 1 |
| `/ingest/runs` | 39 | 8 |
| `/ingest/secrets` | 11 | 0 |
| `/ingest/orphans` | 7 | 2 |
| `/ingest/catalog` | 11 | 1 |
| `/backup` | 17 | 4 |
| `/upgrade` | 24 | 8 |
| `/users` | 72 | 6 |
| `/users/roles` | 35 | 3 |
| `/mail` | 30 | 1 |
| `/audit` | 1 | 1 |
| `/settings/overlay` | 62 | 1 |
| 19 other pages | 0 | 0 |
| **Total** | **637** | **53** |

**Shared page script.** `base.html` carries no executable inline script. What every page
shares lives in `src/app/static/js/core/`: `format.js` (filling and escaping sentences, plurals),
`csrf.js`, `menus.js` (floating row menus), `session.js` (session end, refused htmx requests),
`jobs.js` (job polling), `actions.js` (add-on, stack and catalog actions and the JSON request
of the knowledge pages), `errors.js` (three-part error messages), `toasts.js` and
`announce.js`. Their texts come from the data island `mgr-core` in the language of the page.
`runtime.js` builds what a page shares once (texts, toaster, the three-part report, the
actions); `main.js` and the page modules import it from there, so a page module calls
`showToast` or `actions.ragRequest` as imports, never as globals. A fragment htmx swaps in
announces its state without inline script: it names a window event in `data-announce`
(`papaia-...`) and its state in `data-active`, and `announce.js` dispatches the event with
`{active}` after every settle. The core modules put no name on `window`: the transition
module `core/legacy.js`, which did while the pages moved into modules, is gone, and
`tests/test_js_core.py` checks that no module assigns one of its former names. A template
expression that formats a sentence (`fmt`, `fmtPlural`) calls a method of its component.

**Page modules.** A page puts its module into the block `page_modules` of
`base.html` (`<script type="module" src="...">`). It loads after `core/main.js` and before
htmx and Alpine, so a component it registers with `Alpine.data` on `alpine:init` exists when
Alpine starts. The module takes the page's toaster and actions from `core/runtime.js`, or from
`pageCore()` of `core/page.js` on first use when unit tests import it; both are the instances
`main.js` uses. It imports the helpers of `core/format.js` instead of reaching for the
transition globals. Its texts and values come from data islands. An expression of the page
calls only what the component offers, such as `fmt` and `fmtPlural`. An element that is hidden
until Alpine starts carries `x-cloak` (a rule of `app.css`) instead of `style="display:none"`.
The job strips of the configuration and mail pages (`partials/config_job_status.html`,
`partials/mail_job_status.html`) announce `papaia-config-job` and `papaia-mail-job` through
`data-announce` and `data-active`. Converted this way, among others: Settings › Overlay
(`pages/settings-overlay.js`, component `overlayEditor`) and Mail › Server (`pages/mail.js`,
listeners on the form instead of `on*` attributes).

**Add-ons, catalogs, nodes and services.** The add-on pages (`pages/addons.js`) register
`catalogRefresh` and `addonControls`; the macros of `partials/_addon_controls.html` call its
methods (`run`, `confirmRun`, `install`, `maintain`, `rollback`, `saveConfig`) and name the
view a verb redraws (`detail` or `gallery`), which the component maps to the shared action and
refuses any other. The catalogs page (`pages/catalogs.js`) binds its dialogs by id and
`data-dialog-open` / `data-dialog-close` and registers `catalogRow` for the swapped-in list.
Services (`pages/services.js`) register `stackControls`, `serviceGroups`, `serviceAddon` and
`stackRunner`; the 15 s swap of the list hands the group map to the selection through
`x-init="setGroups($el)"` and `data-group-map`. Nodes (`pages/nodes.js`, after a fresh sign-in
only) bind the form "Add a node" and the buttons marked `data-node-action`. Modules
(`pages/modules.js`) register `delegationOverview`. The Host page needs no module: the local
time of `[data-checked-at]` comes from `core/format.js`. A floating row menu
(`overflow_menu(float=True)`) is fixed by a rule of `app.css` instead of an inline style. The
shared add-on, stack and catalog actions are no globals any more.

**Messages.** Toasts go to the frame's live regions `#pk-toasts` (one `role="status"`, one
`role="alert"`, both present before the first message) and are the kit's toasts. Success,
info and warning messages stand at least 6 s and as long as the pointer rests on them or the
focus is inside; error messages stay until closed with their labelled close button. A refused
request is told in three parts in this order: what happened, what to do, and "Reference:
<correlation id>". The texts are the kit's per error class, so "connection stopped" (no answer,
502, 503, 504), "right missing" (403), "input invalid" (400, 422) and "session expired" (401)
read the same in every module; a plain sentence of the server may stand as part 1 of an input
or conflict refusal, never a status code, a stack trace or a key name. Every request of a page
sends a correlation id of its own (`X-Papaia-Correlation-Id`), and the manager logs a refused
request with that id (`request refused: METHOD PATH answered STATUS, correlation id ID`), so
the reference leads to the log line. A session end shows one message whose "Sign in again"
reloads the page. `tests/test_toast_browser.py` checks all of this in Chromium.

**Pinned libraries.** `docker/Dockerfile` downloads htmx, both Alpine builds and Sortable
pinned by version and checks each file against the SHA-256 written next to it
(`docker/verify-sha256.sh`); a file that does not match fails the image build. The browser
checks install the same versions (`scripts/design/package.json`).

## REST API

All mutating routes require the `MANAGER_ADMIN_ROLE` and a CSRF header, except those of the Users page (`/api/v1/users`, `/api/v1/roles`), which require the `MANAGER_IDENTITY_ADMIN_ROLE` instead.
Long-running operations return `202` with a job id. The RAG routes
(`/api/v1/rag/`) are admin-only too and, after the role check, answer 404 while the
`rag` profile is not active.

```
GET  /health                              # unauthenticated

GET    /api/v1/modules                    # modules: {bom, catalog_error, modules}; admin, read-only
GET    /api/v1/delegations                # counts per module and person; identity admin
DELETE /api/v1/delegations/{module}/persons/{user_id}   # revoke; module flow|assistant
GET    /api/v1/inventory                  # papaia.inventory/1, ?flag=&module=&data_class=&effect=&person=; auditor
GET    /api/v1/inventory/export?format=csv|json   # same filters; inventory_exported row; auditor
GET    /access-report?by=person|system&due=1     # permission report page (papaia-audit); auditor
GET    /access-report/download?by=person|system&format=csv|json   # answer of papaia-audit, unchanged; auditor

GET    /api/v1/catalogs
POST   /api/v1/catalogs                   # {name, type, url|path, ref?, auth?}
PUT    /api/v1/catalogs/{name}
DELETE /api/v1/catalogs/{name}
POST   /api/v1/catalogs/{name}/refresh     # → 202 {job_id}

GET    /api/v1/tiles                       # the whole tile configuration, unfiltered, with its revision
PUT    /api/v1/tiles                       # {revision, version?, groups}; a tile is a link or
                                           #   {type: "widget", id?, widget, title?, size?, visibility?}
GET    /api/v1/tiles/raw                   # tiles.yaml as text, with its revision
PUT    /api/v1/tiles/raw                   # {revision, yaml}
POST   /api/v1/tiles/resolve               # {values} resolve {{KEY}} links for the editor's preview

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
DELETE /api/v1/rag/ingest/jobs/{id}        # ?etag=&purge= also remove what it embedded; its runs go too
POST   /api/v1/rag/ingest/jobs/{id}/enable   # and /disable (enabled: false) and /resume (scheduling)
POST   /api/v1/rag/ingest/jobs/{id}/run    # {mode?, full_scope?, dry_run?, skip_sync?, force?, delete_vanished?, confirm_*} → 202 {run_id}
GET    /api/v1/rag/ingest/jobs/{id}/files  # ?status=&q=&run_id=&order=&limit=&offset= what happened to each file
GET    /api/v1/rag/ingest/jobs/{id}/preview  # the files the saved filters match
GET    /api/v1/rag/ingest/jobs/{id}/runs   # the runs of one job
POST   /api/v1/rag/ingest/jobs/{id}/runs/delete  # {since?, until?, dry_run?} whole days, both included; counts or deletes the history
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

GET  /api/v1/addons                        # each add-on with its status, its bundled `components`, its `requirements`
                                           #   (`requirements_met`, `requirements_enforced`) and `required_by`
GET  /api/v1/addons/{name}
GET  /api/v1/addons/{name}/env-form
POST /api/v1/addons/{name}/install         # → 202
POST /api/v1/addons/{name}/start           # → 202
POST /api/v1/addons/{name}/stop            # {clean_up?} → 202
POST /api/v1/addons/{name}/restart         # {clean_up?} stop then start → 202
POST /api/v1/addons/{name}/remove          # stop and remove the containers, then deactivate → 202; 409 without its docker-compose.yml
                                           #   or while an active add-on requires it
POST /api/v1/addons/{name}/uninstall       # → 202; 409 while an active add-on requires it
POST /api/v1/addons/{name}/actions/{action} # run an action the manifest declares → 202; 404 if undeclared or the add-on is not active, 409 in a distributed installation
GET  /api/v1/addons/upgrades                # {supported, upgrades: [{name, installed, available, catalog}]}
POST /api/v1/addons/{name}/upgrade-plan     # {revision?} stage the catalog's version, ask papaia-ctl what upgrading would do → {revision, plan, form}
POST /api/v1/addons/{name}/upgrade          # {env?, revision?, backup?} → 202; 409 if the gate refuses, 422 for a missing value
POST /api/v1/addons/{name}/rollback        # {restore_data?} → 202; 409 without an earlier version
POST /api/v1/addons/{name}/save-config     # → 202
POST /api/v1/addons/{name}/check           # synchronous compatibility check

GET /api/v1/jobs
GET /api/v1/jobs/{id}
GET /api/v1/jobs/{id}/log

POST   /api/v1/nodes                       # Distributed only; needs a fresh sign-in
DELETE /api/v1/nodes/{name}                # Distributed only; needs a fresh sign-in
POST   /api/v1/nodes/{name}/bundle         # Distributed only; needs a fresh sign-in: hand the node its release and bundle
POST   /api/v1/nodes/{name}/pin            # Distributed only; needs a fresh sign-in
GET    /api/v1/audit                       # ?user,action,result,target,since,before,limit,offset
GET    /api/v1/audit/export                # {format=csv|jsonl} + same filters, streamed
POST   /api/v1/audit/prune                 # {before, dry_run?} → removed/kept counts

GET    /api/v1/maintenance/backup-dir
GET    /api/v1/maintenance/restore-points
GET    /api/v1/maintenance/restore-points/{id}
GET    /api/v1/maintenance/restore-points/{id}/selectors  # what it can restore in part, and what that stops
POST   /api/v1/maintenance/restore-points/delete  # {ids} → 202 {job_id}
POST   /api/v1/maintenance/backup                 # {retention_days?, node?} → 202 {job_id}; node: one node of a distributed installation
GET    /api/v1/maintenance/schedule               # the schedule, next run, last backup, overdue
PUT    /api/v1/maintenance/schedule               # {cron, timezone?, retention_days?, enabled?, run_on_startup?}
DELETE /api/v1/maintenance/schedule               # remove it (also clears an unreadable file)
POST   /api/v1/maintenance/restore                # {restore_point, restart_clean?} → 202; 409 for a restore point of one add-on
POST   /api/v1/maintenance/restore/scoped         # {restore_point, only} part of a snapshot → 202 {job_id}
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

GET    /api/v1/models                        # the models LiteLLM serves: type, host, price per million tokens
GET    /api/v1/models/usage                  # ?range=7d|30d|90d|month|custom&from=&to= (UTC days):
                                             #   totals, per day and per model; 422 for a window that
                                             #   makes no sense, 503 when LiteLLM cannot be read
GET    /api/v1/knowledge-map                 # the rendered map: entries, findings, warnings, operator files
GET    /api/v1/knowledge-map/server          # the map server's own view; 503 when it can't be asked
POST   /api/v1/knowledge-map/simulate        # {need, roles?} route a question as a role set (not recorded)
GET    /api/v1/knowledge-map/stats           # usage aggregates and what the server records
GET    /api/v1/knowledge-map/eval/history    # the gold-set runs
POST   /api/v1/knowledge-map/eval            # run the gold set
GET    /api/v1/knowledge-map/suggestions     # ?days= refinements mined from use
POST   /api/v1/knowledge-map/suggestions/{id}  # {decision: accepted|rejected, days?, confirm_no_personal_data?}
                                             #   accepted → 202 {job_id}; 422 when the merged file fails the check
GET    /api/v1/knowledge-map/entries/{id}      # one entry as a form: fields, origin, changed fields, revision
POST   /api/v1/knowledge-map/entries           # {fields, revision} a new own entry → 202 {job_id}
PUT    /api/v1/knowledge-map/entries/{id}      # {fields, reset?, revision} own: sources.yaml, declared: override → 202
DELETE /api/v1/knowledge-map/entries/{id}      # {revision} an own entry → 202 {job_id}
POST   /api/v1/knowledge-map/entries/probe     # {fields, questions, replaces?} where the questions would land (not saved)
POST   /api/v1/knowledge-map/files/{key}/check # {content} → {ok, problems}; key: sources|overrides|goldset|governance
PUT    /api/v1/knowledge-map/files/{key}     # {content, revision} → 202 {job_id}
GET    /api/v1/knowledge-map/governance      # sources, models, agents and findings of the Governance tab
PUT    /api/v1/knowledge-map/governance/entries/{id}   # {revision, owner?, classification?, personal_data?,
                                             #   data_subjects?, ropa_ref?} → 202 {job_id}
POST   /api/v1/knowledge-map/governance/entries/{id}/confirm-proposal  # {revision} → 202 {job_id}
PUT    /api/v1/knowledge-map/governance/models/{name}  # {revision, tier?, role?}; no tier removes it → 202
PUT    /api/v1/knowledge-map/governance/exceptions     # {revision, exceptions} → 202 {job_id}
GET    /api/v1/knowledge-map/governance/export/{name}  # facts.json or facts.csv
GET    /brand/logo                           # the stored logo, for any signed-in user
POST   /account/language                     # form fields locale (auto or a shipped language), csrf_token, next?: the
                                             #   signed-in account's own language, for every role; 303 back to the page

GET    /api/v1/users                         # ?search&first&limit: a page of accounts with their roles
GET    /api/v1/users/roles                   # the roles a new account can be given
POST   /api/v1/users                         # {username, email?, first_name?, last_name?, credential?, roles?}
                                             #   credential: temporary | email | none; a temporary password
                                             #   is in this one response only
PUT    /api/v1/users/{id}/enabled            # {enabled}; disabling also ends the sessions
GET    /api/v1/users/{id}/roles              # the roles, marked with what the account holds
PUT    /api/v1/users/{id}/roles              # {roles} the account's own realm roles
POST   /api/v1/users/{id}/password/temporary # a new temporary password, in this response only
POST   /api/v1/users/{id}/password/email     # mail the account Keycloak's update-password link
GET    /api/v1/users/{id}/sessions           # where the account is signed in
DELETE /api/v1/users/{id}/sessions/{sid}     # end one session
DELETE /api/v1/users/{id}/sessions           # end all of them

GET    /api/v1/roles                         # every realm role with what it contains
GET    /api/v1/roles/{name}                  # one role, with the accounts that hold it
POST   /api/v1/roles                         # {name, description?, members?}
PUT    /api/v1/roles/{name}                  # {description, members}: the realm roles it contains
DELETE /api/v1/roles/{name}                  # not a built-in role

GET    /api/v1/mail                          # ?fresh: the stored settings (never the password) and the relay's status
PUT    /api/v1/mail                          # {revision, host, port?, security, user?, password?, from_address, from_name?}
                                             #   store the mail server and, while the relay is on, apply it; 202 with a job.
                                             #   An empty password keeps the stored one. Switching the relay on or off is
                                             #   /api/v1/config/modules/mail/{enable,disable}
POST   /api/v1/mail/probe                    # {host?, port?, security?, user?, password?} try the mail server, send nothing
POST   /api/v1/mail/test-message             # {to} hand one message to the relay

GET    /api/v1/config/modules                # the optional modules, their state, the LocalAI and voice variants (variants, voice_variants), the revision
GET    /api/v1/config/modules/{name}/info    # localai | web-search | rag | voice | mail | knowledge-map: the volumes and settings a switch-off
                                             #   could delete, the knowledge map's operator files and whether each exists, and the open setup questions (public URL)
                                             #   a switch-on asks
POST   /api/v1/config/modules/{name}/enable  # {revision, variant?, public_url?, mail?} 202 with a job. variant: localai | voice; public_url: rag | localai; mail: the mail
                                             #   server when none is stored. The RAG system needs no clashing add-on
POST   /api/v1/config/modules/{name}/disable # {revision, scope?: keep|data|all, operator_files?, confirm?}; 202 with a job. A deletion needs the module name in
                                             #   confirm, and a single host. operator_files: knowledge-map only, also deletes the files written on the AI map page
GET    /api/v1/config/overlay                # the four editable overlay files and the other files under overlay/
GET    /api/v1/config/overlay/{key}          # librechat | litellm | chatterbox | searxng: content, revision, versions
POST   /api/v1/config/overlay/{key}/check    # {content} syntax check, nothing is written
POST   /api/v1/config/overlay/{key}/preview  # {content} the effective result as a diff (needs the core's render-preview)
PUT    /api/v1/config/overlay/{key}          # {content, revision} save, back up and recreate; 202 with a job, 409 if stale
DELETE /api/v1/config/overlay/{key}          # ?revision= remove the file (backed up first); 202 with a job
GET    /api/v1/config/overlay/{key}/versions            # the saved versions, newest first
GET    /api/v1/config/overlay/{key}/versions/{id}       # one version and its difference to the file now
POST   /api/v1/config/overlay/{key}/rollback            # {version, revision} make a saved version the overlay; 202 with a job

GET    /api/v1/notifications                 # the settings, the events, the recipients (a webhook URL is shown
                                             #   as https://host/…, never in full), and whether email can go out
POST   /api/v1/notifications/recipients      # {revision, name, kind: email|webhook, target, enabled?, events?}
PUT    /api/v1/notifications/recipients/{id} # {revision, name?, target?, enabled?, events?}; no target keeps the stored one
PUT    /api/v1/notifications/recipients/{id}/events  # {revision, events}: what one recipient receives
DELETE /api/v1/notifications/recipients/{id} # {revision}; its subscriptions go with it
PUT    /api/v1/notifications/settings        # {revision, check_interval_minutes, debounce_minutes, cert_lead_days}
POST   /api/v1/notifications/recipients/{id}/test  # send a test notification to one recipient
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
│       ├── i18n/           # Languages found under locale/, the request's language, Jinja install
│       ├── locale/         # One directory per language: <code>/LC_MESSAGES/messages.po
│       ├── auth/           # OIDC + PKCE login, CSRF, role dependencies, the user's own token
│       ├── core/           # ctl (whitelisted papaia-ctl calls), papaia_lib (core handshake),
│       │                   # catalogs, snapshots, status, env-forms, jobs, audit, tiles,
│       │                   # components (the software an add-on bundles),
│       │                   # requirements (the add-ons an add-on requires, and which require it),
│       │                   # actions (the maintenance actions an add-on declares),
│       │                   # addon_upgrades + semver (reviewing, applying and rolling back an add-on upgrade),
│       │                   # user_prefs (what each account chose: its language),
│       │                   # keycloak, keycloak_users + users_service + roles_service (the Users page),
│       │                   # resolve (cross-catalog add-on dedup),
│       │                   # services (container status), inventory (declared state),
│       │                   # backups (restore-point catalogue), restore_scope (partial restore),
│       │                   # runner (detached restore, stack and upgrade runs),
│       │                   # upgrade + images (core upgrade check, outdated images),
│       │                   # backup_run + schedule + scheduler (backup schedule),
│       │                   # host_health + docker_usage (host readings from the core's doctor),
│       │                   # nodes (a distributed installation: the registry, the executors, the lock),
│       │                   # litellm + model_usage + usage_view + charts (Models & Usage),
│       │                   # knowledge_map + knowledge_map_client + knowledge_map_jobs +
│       │                   # knowledge_map_entries (the entry editor) +
│       │                   # knowledge_map_governance (the AI map page),
│       │                   # rag (optional RAG system: dashboard tile, sidebar gate, settings
│       │                   # read from the RAG module's .env) +
│       │                   # qdrant (REST client) + rag_collections (collections and roles),
│       │                   # vectordb/ (connection types and the ingester's connection store),
│       │                   # ingest/ (the ingester's jobs.yaml and secrets.yaml, its REST API,
│       │                   # job editor and schedules, runs, the Embedding page's managed
│       │                   # jobs, jailed browsing, staged uploads, background clean-up),
│       │                   # settings_store (settings.yaml and the logo),
│       │                   # migrations (one-time moves of older releases' data, migrations.json),
│       │                   # config/ (Settings > Modules and Overlay: the optional modules, the
│       │                   # overlay files, their saved versions, the jobs that apply a change),
│       │                   # mail/ (the mail relay: stored settings and their rules, SMTP probes,
│       │                   # status from the core's doctor, the jobs that apply a change),
│       │                   # notifications/ (recipients and what each receives, the checks that
│       │                   # find what to report, delivery by email and webhook, the background task),
│       │                   # widgets/ (the dashboard's widgets: the payload they produce, the registry,
│       │                   # reading and caching, the built-in types)
│       ├── shell.py        # the input of the platform's shared frame (papaia-kit shell contract)
│       ├── routers/        # auth, health, kit (the frame's assets, forms, help), ui, api_catalogs,
│       │                   # api_addons, api_jobs,
│       │                   # api_maintenance, api_stack, api_upgrade, api_audit,
│       │                   # api_tiles, api_settings, api_collections, api_connections, api_ingest,
│       │                   # api_ingest_jobs, ui_ingest, rag_deps (RAG role, profile and store),
│       │                   # api_users, api_roles, users_deps (identity role, Keycloak client),
│       │                   # api_config, ui_config, api_mail, ui_mail, api_notifications,
│       │                   # ui_notifications, ui_widgets, account (the account's language),
│       │                   # api_models, ui_models, models_deps (Models & Usage),
│       │                   # api_knowledge_map, api_knowledge_map_governance, ui_knowledge_map,
│       │                   # knowledge_map_deps (the AI map page),
│       │                   # api_nodes, ui_nodes (a distributed installation),
│       │                   # stack_guard (the refusals the configuration routes share)
│       ├── templates/      # Jinja2 pages + HTMX partials
│       └── static/         # htmx.min.js, alpine-csp.min.js, sortable.min.js,
│                           # app.css (Tailwind build), icons/ (the default tiles' logos, with
│                           # NOTICE.txt), js/core/ and js/pages/ (ES modules, no bundler)
├── tests/                  # pytest suite (sibling to src/); test_csp_zero.py keeps every
│                           # template free of what the content security policy refuses
└── docker/
    ├── Dockerfile          # multi-stage build; installs Docker CLI + compose plugin and an SSH client
    ├── entrypoint.sh       # names the host's user id for OpenSSH, then runs the command
    ├── docker-compose.yml  # local development compose
    ├── git-askpass.sh      # GIT_ASKPASS helper for private catalog auth
    ├── verify-sha256.sh    # build-time check of the pinned browser libraries
    ├── tailwind.config.js, tailwind.brand.css, tailwind.app.css   # stylesheet sources
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
uv run uvicorn app.main:create_app --factory --reload --port 8120
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
- The platform kit `papaia-kit` 0.1.0 is vendored under `src/vendor/papaia-kit/`
  (wheel, its `LICENSE` and the npm package of the design check, pinned by
  SHA-256 in `src/kit.lock` and `src/vendor/papaia-kit/js/kit-js.lock`). It is
  MIT-licensed and ships no `NOTICE`.
- See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the *Inbound = Outbound (MIT)*
  rule and the list of license categories that contributions may not
  introduce without prior approval.
