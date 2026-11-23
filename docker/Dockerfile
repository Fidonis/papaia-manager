# syntax=docker/dockerfile:1

# ---- builder: resolve and install dependencies into an isolated venv ----
FROM python:3.12-slim AS builder

# Pinned uv release so dependency resolution is reproducible across rebuilds.
COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Copy only the dependency manifests first so this layer stays cached when
# application code changes but dependencies do not.
COPY src/pyproject.toml src/uv.lock ./
# The platform kit has no package index yet: uv.lock binds the vendored wheel by path and
# SHA-256 (src/kit.lock).
COPY src/vendor ./vendor

RUN uv sync --frozen --no-dev

# ---- assets: download single-file frontend libraries ----
FROM python:3.12-slim AS assets

# Versions are pinned; update deliberately (not via Dependabot, since these
# are vendored into the image rather than declared as package dependencies).
ARG HTMX_VERSION=2.0.4
ARG ALPINE_VERSION=3.14.9
ARG SORTABLE_VERSION=1.15.6

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /static \
    && curl -fsSL \
        "https://unpkg.com/htmx.org@${HTMX_VERSION}/dist/htmx.min.js" \
        -o /static/htmx.min.js \
    && curl -fsSL \
        "https://unpkg.com/alpinejs@${ALPINE_VERSION}/dist/cdn.min.js" \
        -o /static/alpine.min.js \
    && curl -fsSL \
        "https://unpkg.com/sortablejs@${SORTABLE_VERSION}/Sortable.min.js" \
        -o /static/sortable.min.js

# ---- css-builder: compile Tailwind + daisyUI (Node only in build, not runtime) ----
FROM node:20-slim AS css-builder

WORKDIR /build

# Pin versions so the CSS output is reproducible.
RUN npm install --save-dev tailwindcss@3.4.17 daisyui@4.12.23 2>/dev/null

# The design tokens: papaia-kit's Tailwind preset carries the daisyUI themes with the
# platform's values (docker/tailwind.config.js). Taken from the vendored kit wheel the
# builder installed, so the manager keeps no copy. The fonts are the kit's as well and are
# served from the wheel at run time (/_kit/<version>/fonts/).
COPY --from=builder /opt/venv/lib/python3.12/site-packages/papaia_kit/_data/dist/tailwind/preset.cjs ./papaia-kit/preset.cjs

# Tailwind config: scan all Jinja2 templates for class names.
COPY docker/tailwind.config.js ./tailwind.config.js
COPY docker/tailwind.brand.css ./tailwind.brand.css
COPY docker/tailwind.app.css ./tailwind.app.css
COPY src/app/templates/ ./templates/

# The brand layer and the application shell are separate files so the brand
# half can be diffed against qdrant-ingest's copy; Tailwind takes one input,
# so they are concatenated here. Order matters: the @tailwind directives lead.
RUN cat tailwind.brand.css tailwind.app.css > tailwind.input.css \
    && npx tailwindcss \
        -c tailwind.config.js \
        -i tailwind.input.css \
        -o /static/app.css \
        --minify

# ---- runtime: minimal image with Docker CLI, git, and the venv ----
FROM python:3.12-slim AS runtime

# Version of the vendored platform kit in this image (src/kit.lock).
LABEL de.fidonis.platform-kit="0.1.5"

# Install Docker CE CLI + Compose plugin and runtime system deps. `openssl` is
# for the core's `doctor`, which reads certificate expiry with the CLI; it is
# listed rather than left to arrive with ca-certificates.
# The Docker APT repo is the only source that provides docker-compose-plugin.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        git \
        ca-certificates \
        curl \
        gnupg \
        libnss-wrapper \
        openssh-client \
        openssl \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
        https://download.docker.com/linux/debian \
        $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-ce-cli \
        docker-compose-plugin \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /bin/bash app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY src/app/ /app/app/
COPY --from=assets /static/ /app/app/static/
COPY --from=css-builder /static/app.css /app/app/static/app.css

# GIT_ASKPASS helper: echoes $GIT_TOKEN; used for private catalog authentication.
# The token appears in no argv and no log — only in the subprocess environment.
COPY docker/git-askpass.sh /app/bin/git-askpass
RUN chmod 755 /app/bin/git-askpass

# The core runs this image as the host's user, an id `/etc/passwd` here does not know, and
# ssh and ssh-keygen stop without a name for it. Runners clone the image and the user.
COPY docker/entrypoint.sh /app/bin/entrypoint
RUN chmod 755 /app/bin/entrypoint
ENTRYPOINT ["/app/bin/entrypoint"]

USER app

EXPOSE 8000

# /health is auth-exempt; used by Docker Compose and load-balancers.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python3", "-c", \
         "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"]

CMD ["uvicorn", "app.main:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000"]
