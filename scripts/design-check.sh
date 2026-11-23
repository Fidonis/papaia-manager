#!/usr/bin/env bash
# Design check of the manager with the kit's browser tool (PLT-83, oberfl 3.8).
#
# Serves the manager in the stand-in world of the tests on 127.0.0.1 (tests/design_serve.py),
# runs `pk-conform --design` of the vendored kit on every page of ui/pages.yaml in both
# themes, and lets scripts/check_design.py decide with ui/design-known.yaml. pk-conform
# comes from .pk/ (scripts/design-tools.sh), the contract files it reads (--kit-dir) from
# the installed kit wheel (papaia_kit/_data).
#
# Usage (from the repository root): scripts/design-check.sh PORT REPORT
#   PORT     free port on 127.0.0.1 for the manager
#   REPORT   where the JSON report of pk-conform is written
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: scripts/design-check.sh PORT REPORT" >&2
  exit 2
fi
port=$1
report=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
cd "$(dirname "$0")/.."
conform=.pk/package/bin/pk-conform.ts
if [[ ! -f "${conform}" || ! -f src/app/static/app.css ]]; then
  echo "design-check: tools or page assets missing, run scripts/design-tools.sh first" >&2
  exit 2
fi
if ! [[ "${port}" =~ ^[0-9]{4,5}$ ]] || ((port < 1024 || port > 65535)); then
  echo "design-check: port must be between 1024 and 65535" >&2
  exit 2
fi
kit_dir=$(cd src && uv run --frozen python -I -c \
  'from importlib.resources import files; print(files("papaia_kit") / "_data")')
if [[ ! -f "${kit_dir}/contract/specimen-expected.light.json" ]]; then
  echo "design-check: the installed kit carries no contract files" >&2
  exit 2
fi

work=$(mktemp -d)
state="${work}/storage-state.json"
server=""
cleanup() {
  if [[ -n "${server}" ]]; then kill "${server}" 2>/dev/null || true; fi
  rm -rf "${work}"
}
trap cleanup EXIT

(cd src && exec uv run --frozen python ../tests/design_serve.py \
  --port "${port}" --storage-state "${state}") &
server=$!
for _ in $(seq 1 100); do
  if [[ -s "${state}" ]] && curl -fs -o /dev/null "http://127.0.0.1:${port}/health"; then
    break
  fi
  kill -0 "${server}" 2>/dev/null || { echo "design-check: the manager did not start" >&2; exit 2; }
  sleep 0.3
done
curl -fsS -o /dev/null "http://127.0.0.1:${port}/health" || {
  echo "design-check: the manager does not answer" >&2
  exit 2
}

pages=()
while IFS= read -r page; do pages+=(--page "${page}"); done < <(
  cd src && uv run --frozen python ../tests/design_serve.py --list-pages
)

# Exit 1 of pk-conform means findings; the gate below decides on them. 2 and 3 are errors.
status=0
node "${conform}" --design \
  --base-url "http://127.0.0.1:${port}" "${pages[@]}" \
  --themes light,dark --lang de --storage-state "${state}" \
  --kit-dir "${kit_dir}" --report "${report}" || status=$?
if ((status > 1)); then
  echo "design-check: pk-conform failed with exit ${status}" >&2
  exit "${status}"
fi
(cd src && uv run --frozen python -I ../scripts/check_design.py \
  --report "${report}" --known ../ui/design-known.yaml)
