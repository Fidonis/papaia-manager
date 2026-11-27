# Contributing to papaia-manager

Thanks for considering a contribution to papaia-manager! This document describes how to file issues, propose changes, and what is expected from contributors.

## Code of Conduct

Please follow common sense: be kind, assume good intent, and keep discussions focused on the project.

## Where to ask questions

- **General questions, ideas, brainstorming** → [GitHub Discussions](https://github.com/Fidonis/papaia-manager/discussions)
- **Bugs, feature requests, documentation issues** → [GitHub Issues](https://github.com/Fidonis/papaia-manager/issues), using the [issue templates](https://github.com/Fidonis/papaia-manager/issues/new/choose)
- **Security vulnerabilities** → use [Private vulnerability reporting](https://github.com/Fidonis/papaia-manager/security/advisories/new) instead of a public issue

## Reporting bugs and requesting features

We use GitHub Issue Forms. When you click *New issue*, you'll see these entry points:

- **Bug report** — for reproducible bugs
- **Feature request** — for new functionality or enhancements
- **Documentation** — for missing, wrong, or unclear docs
- **Question / Discussion** (link) — redirects to Discussions

Each form prefills the right labels and structure, so please use them rather than blank issues.

## Pull request workflow

1. Open or comment on the issue you intend to work on, so duplicate effort can be avoided.
2. Create a feature branch from `main`. Branch naming:
   - `feat/<short-name>` — new features
   - `fix/<short-name>` — bug fixes
   - `docs/<short-name>` — documentation
   - `refactor/<short-name>` — refactoring without behavior change
   - `test/<short-name>` — adding or fixing tests
   - `ci/<short-name>` — CI/CD changes
   - `chore/<short-name>` — maintenance
3. Make your change in small, reviewable commits.
4. Open a pull request against `main`. The PR template is filled in automatically; please complete each section, especially **Linked issues**, **Type of change**, and **Test plan**.
5. CI runs the linters (Ruff, Yamllint), the type check (Mypy), the tests (Pytest), the license check and the PR-title check. Address any failures. Once green, request a review.
6. PRs are merged via **Squash & Merge**. The PR title becomes the squash commit message — make sure it follows Conventional Commits (see below).

## Commit and PR title convention

We use [Conventional Commits](https://www.conventionalcommits.org/) for PR titles, which are squashed into the merge commit.

Format: `<type>[(<scope>)][!]: <subject>`

| Type | Use for |
|---|---|
| `feat` | New user-facing feature (minor version bump) |
| `fix` | Bug fix (patch version bump) |
| `docs` | Documentation changes |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `perf` | Performance improvement |
| `style` | Formatting only, no code change |
| `test` | Adding or fixing tests |
| `ci` | CI configuration |
| `build` | Build system / dependencies |
| `chore` | Maintenance tasks |
| `revert` | Reverts a previous commit |

A `!` after the type or scope marks a **breaking change** and triggers a major version bump.

The subject must be lowercase, in imperative mood (*"add"*, not *"added"* or *"adds"*), without a trailing period.

## Code style

Linters run on every push and pull request:

- **Python** — [`ruff`](https://docs.astral.sh/ruff/) for linting and import ordering
- **YAML** — [`yamllint`](https://yamllint.readthedocs.io/)

Type checking with [`mypy`](https://mypy-lang.org/) (strict mode) is configured in `src/pyproject.toml`.

Run the checks locally before pushing:

```bash
# Lint — from the repository root, as CI runs it
yamllint .
uv run --project src ruff check .

# Type check and tests — from src/
cd src
uv run mypy .
uv run pytest -q
```

Some tests depend on where they run. `tests/test_ingester_contract.py` compares the manager's
mirror of the ingester's job schema with the ingester's own modules and is skipped unless
`QDRANT_INGEST_SRC` points at the `src` directory of a `qdrant-ingest` checkout; run it with the ingester's
latest release whenever either schema changes. `tests/test_mail_contract.py` and
`tests/test_config_contract.py` do the same for the mail relay and the optional modules against the papAIa
core (the real `setup` and `render-preview` run in a copy of the checkout): they are skipped unless
`PAPAIA_SRC` points at a checkout of the core that has them (papAIa 1.6.0 or newer), so run them when
either side's options or rules change. `tests/test_chat_history_contract.py` reads the summary of the
core's `librechat clear-history` the same way and needs a core that has the command (papAIa 1.9.0 or
newer). The tests that need a symbolic link or read a file mode are skipped
where the platform cannot provide one (run them on Linux).

Run `ruff` from the repository root, not from `src/`: `tests/` sits next to `src/`, and
`ruff check .` inside `src/` does not see it, so a line in a test that CI rejects would pass
locally. `--project src` only makes `uv` use the environment that `uv sync` created there.

## Local development

```bash
# Generate the lock file (only needed once, or after pyproject.toml changes)
cd src
uv lock

# Create the virtual environment
uv sync

# Copy and edit the env file
cp .env.example .env
# Edit .env with your local Keycloak settings

# Run the development server
uv run uvicorn app.main:create_app --factory --reload --port 8120
```

The application expects a running Keycloak instance with a configured `papaia-manager` client. The compose file under `docker/` builds and runs the manager only, with the workspace, config and backup directories mounted and no Docker socket; it brings no Keycloak and does not join the stack's Docker networks, so addresses such as `QDRANT_URL`, `QDRANT_INGEST_URL` and `DOCKER_GATE_URL` have to be set to something it can reach.

## Add or update a language

The interface is written in English and ships a German catalogue. A language is a directory
`src/app/locale/<code>/LC_MESSAGES/messages.po`; the application finds it at start-up, lists it in the
account menu under its own name and uses it for browsers that ask for it. Nothing else names a language,
so adding one needs no change to the code. The docstring of `src/app/i18n/__init__.py` describes how texts are
marked in templates, JavaScript and Python.

All commands run from `src/`:

```bash
cd src

# 1. Extract every text of the interface into a template (generated, not committed)
uv run pybabel extract -F babel.cfg --no-location --no-wrap -o app/locale/messages.pot app

# 2. A new language: create its catalogue (use the language code, for example fr or pt_BR)
uv run pybabel init -i app/locale/messages.pot -d app/locale -l fr

#    An existing language, after the source changed: add the new texts, drop the removed ones
uv run pybabel update -i app/locale/messages.pot -d app/locale --no-fuzzy-matching --no-wrap
```

Then translate every `msgstr` in `app/locale/<code>/LC_MESSAGES/messages.po`. Keep the placeholders
(`%(name)s`, `{name}`) and the HTML tags exactly as they are in the English text, give every plural form
a translation (the header's `Plural-Forms` line says how many there are), and do not leave `fuzzy`
entries behind. `tests/test_i18n_catalog.py` runs for every catalogue it finds and fails with the
texts that are missing, empty, fuzzy, stale or malformed, so a half-translated language does not pass
CI. Texts that stay in English on purpose (logs, audit entries, notification mails) are listed in
the docstring of `src/app/i18n/__init__.py` and are not part of a catalogue.

A change to a text of the interface is a change to every catalogue: extract, update, translate the
new entries, and run `uv run pytest -q ../tests/test_i18n_catalog.py`.

## Brand layer

Since stamp 3 the visual identity of the interface is the platform's: the colours, radii and fonts are
the design tokens of `papaia-kit`, and every page is the content of the kit's shared frame (brand, module
switcher, navigation, account menu, page header; see "Shared frame" in the README). The manager's files
only consume them: `docker/tailwind.config.js` builds the daisyUI themes from the kit's Tailwind preset
(taken from the vendored wheel in the image build, no value of its own), `docker/tailwind.brand.css` opens
the build, `docker/tailwind.app.css` holds the element defaults of the content (scoped to `#pk-main`, so the
frame's layered stylesheet is never overridden), `src/app/templating.py` the `asset_url()` fingerprinting
and `src/app/templates/base.html` the call of the frame. Each of these carries a `fidonis-brand: N` comment
stamp. A colour or font changes in the kit, never here.

Up to `qdrant-ingest` 0.3.0 its operator web interface carried copies of these files, and a brand change was
finished only when both repositories carried the same stamp. `qdrant-ingest` 1.0.0 removed that interface,
so there is no second copy to keep in step any more. The stamps remain as the version of the brand layer:
change it in every file at once. `tests/test_brand_layer.py` catches a half-finished edit, that is a file
that lost its stamp or stamps that disagree with each other.

## Design check

`ui/pages.yaml` lists every full page of the manager for the kit's checks. The CI job `design` runs the
kit's `pk-conform --design` over all of them, in light and dark, at desktop and phone width, with axe.
Locally (Node 22, uv; nothing is fetched beyond the locked npm packages and a Chromium build):

```bash
scripts/design-tools.sh                      # unpack the vendored kit tool, build app.css and the scripts
scripts/design-check.sh 57880 /tmp/design-report.json
```

`tests/design_serve.py` serves the manager in a stand-in installation on 127.0.0.1 (no real Keycloak,
Docker or LiteLLM) and signs a synthetic administrator in. Its `docker` is the stand-in command of
`tests/fake_docker.py` (first on `PATH` of the server): the image report of the Upgrade page reads a
small synthetic image set, every other call answers like an unreachable daemon, and the Docker of the
host running the checks is never asked. Nor its network: every service address of the manager (Qdrant,
the ingester, LiteLLM, the knowledge map, prefs, papaia-audit, Keycloak's endpoints) points at one
stand-in on 127.0.0.1 that closes each connection at once, so those pages show "unreachable" at once
on every host, and a guard refuses and logs every other name lookup or connection of the server
(`tests/design_net.py`, log in `PAPAIA_DESIGN_NET_LOG`). A page settles for the check: nothing in a
closed `<details>` animates, since the browser never finishes an animation there and the kit's
check waits for every one (`tests/test_settle_browser.py`). `scripts/check_design.py` decides: a finding
not in `ui/design-known.yaml` fails, and so does an entry that no longer matches a finding. Serious and
critical axe findings are listed there as `axe-<rule>` so they can only go away, never come in. A new
page goes into `ui/pages.yaml` and needs no entry. A finding is fixed in the page, not added to the list.

## License of your contributions (Inbound = Outbound)

This project is published under the [MIT license](LICENSE). By submitting a pull request, comment with a code suggestion, or any other contribution to this repository, **you agree that your contribution is licensed under the same MIT license** that the project itself uses ("Inbound = Outbound"). No separate Contributor License Agreement (CLA) is required.

You also confirm that:

- You have the right to license the contribution under MIT.
- Your contribution does not knowingly infringe a third party's copyright, patent or trademark.

### Copyleft licenses are not accepted

To keep the project freely redistributable under MIT, **contributions containing or directly depending on code under a copyleft license are not accepted** without prior written approval from the Fidonis management. This includes (non-exhaustive list):

- GNU General Public License (GPL), any version
- GNU Lesser General Public License (LGPL), any version
- GNU Affero General Public License (AGPL), any version
- Mozilla Public License (MPL) 2.0 when used in a way that would make the project a covered work
- Server Side Public License (SSPL)
- European Union Public Licence (EUPL)

## Trademarks

"Fidonis" and "papaia-manager" are trademarks of Fidonis GmbH (in Gründung). See [TRADEMARK.md](TRADEMARK.md) for the rules on how you may and may not use them.

Logos and other third-party graphics bundled under `src/app/static/icons/` are the property of their owners. A new file there needs an entry in `src/app/static/icons/NOTICE.txt` with its source, its licence and the owner of the mark it shows, and the licence text where the licence asks for it; `tests/test_icon_assets.py` fails without one. Take a file from its source unchanged and pin the source to a commit or tag, since a collection's licence statement does not always cover every file in it.

---

Thanks again for contributing!
