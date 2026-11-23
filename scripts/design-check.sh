#!/usr/bin/env bash
# Design check of the manager with the kit's browser tool (PLT-83, oberfl 3.8).
#
# Serves the manager in the stand-in world of the tests on 127.0.0.1 (tests/design_serve.py),
# runs `pk-conform --design` of the vendored kit on every page of ui/pages.yaml in both
# themes, and lets scripts/check_design.py decide with ui/design-known.yaml. pk-conform
# comes from .pk/ (scripts/design-tools.sh), the contract files it reads (--kit-dir) from
# the installed kit wheel (papaia_kit/_data).
#
# Every page is checked signed in as its test person of the list (Anna for the
# administration, Emil for the inventory): one pk-conform run and one gate per person.
#
# Usage (from the repository root): scripts/design-check.sh PORT REPORT
#   PORT     free port on 127.0.0.1 for the manager
#   REPORT   where the JSON report of pk-conform is written (Anna's pages; the report of
#            every other person goes next to it as <REPORT without .json>.<person>.json)
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
server=""
cleanup() {
  if [[ -n "${server}" ]]; then kill "${server}" 2>/dev/null || true; fi
  rm -rf "${work}"
}
trap cleanup EXIT

persons=()
while IFS= read -r person; do persons+=("${person}"); done < <(
  cd src && uv run --frozen python ../tests/design_serve.py --list-persons
)
states=()
for person in "${persons[@]}"; do states+=(--storage-state "${person}=${work}/${person}.json"); done
last_state="${work}/${persons[${#persons[@]}-1]}.json"

(cd src && exec uv run --frozen python ../tests/design_serve.py \
  --port "${port}" "${states[@]}") &
server=$!
for _ in $(seq 1 100); do
  if [[ -s "${last_state}" ]] && curl -fs -o /dev/null "http://127.0.0.1:${port}/health"; then
    break
  fi
  kill -0 "${server}" 2>/dev/null || { echo "design-check: the manager did not start" >&2; exit 2; }
  sleep 0.3
done
curl -fsS -o /dev/null "http://127.0.0.1:${port}/health" || {
  echo "design-check: the manager does not answer" >&2
  exit 2
}

failed=0
for person in "${persons[@]}"; do
  if [[ "${person}" == anna ]]; then
    out="${report}"
  else
    out="${report%.json}.${person}.json"
  fi
  pages=()
  while IFS= read -r page; do pages+=(--page "${page}"); done < <(
    cd src && uv run --frozen python ../tests/design_serve.py --list-pages --person "${person}"
  )
  # Exit 1 of pk-conform means findings; the gate below decides on them. 2 and 3 are errors.
  status=0
  node "${conform}" --design \
    --base-url "http://127.0.0.1:${port}" "${pages[@]}" \
    --themes light,dark --lang de --storage-state "${work}/${person}.json" \
    --kit-dir "${kit_dir}" --report "${out}" || status=$?
  if ((status > 1)); then
    echo "design-check: pk-conform failed with exit ${status} for ${person}" >&2
    exit "${status}"
  fi
  (cd src && uv run --frozen python -I ../scripts/check_design.py \
    --report "${out}" --known ../ui/design-known.yaml) || failed=1
done
exit "${failed}"
