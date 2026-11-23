#!/usr/bin/env bash
# Installs the tools of the design check into .pk/ (PLT-83, oberfl 3.8).
#
# pk-conform comes from the npm package of the kit, vendored with its SHA-256 in
# src/vendor/papaia-kit/js/kit-js.lock; no checkout of the kit repository and no
# credential is needed. The kit's TypeScript sources run with Node type stripping, which
# Node refuses below node_modules, so the package is unpacked to .pk/package and its peers
# playwright-core and axe-core (the versions of the kit's contract/specimen.yaml) go to
# .pk/node_modules, locked in scripts/design/package-lock.json together with the builds
# the image makes: Tailwind and daisyUI for app.css, htmx, the CSP build of Alpine and Sortable,
# whose files docker/Dockerfile pins by SHA-256 (tests/test_docker_assets.py). Then the
# page assets are built into src/app/static/ as the image builds them (docker/Dockerfile),
# and the Chromium build of that playwright-core is installed (skipped when present).
#
# Usage (from the repository root): scripts/design-tools.sh
set -euo pipefail

if [[ $# -ne 0 ]]; then
  echo "usage: scripts/design-tools.sh" >&2
  exit 2
fi
cd "$(dirname "$0")/.."

# Path of the npm package after its digest was checked against kit-js.lock.
tarball=$(cd src && uv run --frozen python -I - <<'PY'
import hashlib
import sys
import tomllib
from pathlib import Path

lock = tomllib.loads(Path("vendor/papaia-kit/js/kit-js.lock").read_text(encoding="utf-8"))
(entry,) = lock["files"]
digest = hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest()
if digest != entry["sha256"]:
    sys.exit(f"design-tools: {entry['path']} does not match its sha256 in kit-js.lock")
print(entry["path"])
PY
)
# The Tailwind preset of the installed kit wheel, as the image takes it.
preset=$(cd src && uv run --frozen python -I -c \
  'from importlib.resources import files; print(files("papaia_kit") / "_data/dist/tailwind/preset.cjs")')

rm -rf .pk
mkdir -p .pk/css/papaia-kit
tar -xzf "src/${tarball}" -C .pk
cp scripts/design/package.json scripts/design/package-lock.json .pk/
npm ci --prefix .pk --ignore-scripts --no-audit --no-fund

# app.css exactly as the css-builder stage of the image makes it.
cp "${preset}" .pk/css/papaia-kit/preset.cjs
cp docker/tailwind.config.js .pk/css/
cat docker/tailwind.brand.css docker/tailwind.app.css > .pk/css/tailwind.input.css
cp -R src/app/templates .pk/css/templates
(cd .pk/css && node ../node_modules/tailwindcss/lib/cli.js -c tailwind.config.js \
  -i tailwind.input.css -o ../../src/app/static/app.css --minify)
cp .pk/node_modules/htmx.org/dist/htmx.min.js src/app/static/htmx.min.js
cp .pk/node_modules/@alpinejs/csp/dist/cdn.min.js src/app/static/alpine-csp.min.js
cp .pk/node_modules/sortablejs/Sortable.min.js src/app/static/sortable.min.js

node .pk/node_modules/playwright-core/cli.js install chromium
