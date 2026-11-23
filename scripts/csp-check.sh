#!/usr/bin/env bash
# Browser check of the content security policy of the manager (T-UI-03, oberfl 3.3).
#
# Serves the manager in the stand-in world of the tests on 127.0.0.1 (tests/design_serve.py,
# signed in as a test administrator), opens every page of ui/pages.yaml in Chromium and runs
# tests/csp_browser.mjs: a page whose template is converted (tests/csp_baseline.json) fails
# on any report of the policy, refused Alpine expression or script error; for the others the
# counts go to the report and, when given, to SUMMARY (the job summary in CI). Each
# CANDIDATE (LABEL=FILE, an Alpine build) adds the number of expressions per page that build
# cannot evaluate (tests/csp_browser.mjs). Needs the tools of scripts/design-tools.sh.
#
# Usage (from the repository root): scripts/csp-check.sh PORT REPORT [SUMMARY [CANDIDATE ...]]
#   PORT       free port on 127.0.0.1 for the manager
#   REPORT     where the JSON report is written
#   SUMMARY    file the Markdown table is appended to ("-" for none)
#   CANDIDATE  LABEL=FILE, an Alpine build to measure the pages against
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: scripts/csp-check.sh PORT REPORT [SUMMARY [CANDIDATE ...]]" >&2
  exit 2
fi
port=$1
report=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
summary=${3:--}
shift $(($# < 3 ? $# : 3))
candidates=()
for candidate in "$@"; do
  label=${candidate%%=*}
  file=${candidate#*=}
  if [[ "${label}" == "${candidate}" || ! -f "${file}" ]]; then
    echo "csp-check: candidate must be LABEL=FILE of an existing file: ${candidate}" >&2
    exit 2
  fi
  candidates+=(--candidate "${label}=$(cd "$(dirname "${file}")" && pwd)/$(basename "${file}")")
done
if [[ "${summary}" != "-" ]]; then
  summary=$(cd "$(dirname "${summary}")" && pwd)/$(basename "${summary}")
fi
cd "$(dirname "$0")/.."
if [[ ! -f .pk/node_modules/playwright-core/index.mjs || ! -f src/app/static/alpine-csp.min.js ]]; then
  echo "csp-check: tools or page assets missing, run scripts/design-tools.sh first" >&2
  exit 2
fi
if ! [[ "${port}" =~ ^[0-9]{4,5}$ ]] || ((port < 1024 || port > 65535)); then
  echo "csp-check: port must be between 1024 and 65535" >&2
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
  kill -0 "${server}" 2>/dev/null || { echo "csp-check: the manager did not start" >&2; exit 2; }
  sleep 0.3
done
curl -fsS -o /dev/null "http://127.0.0.1:${port}/health" || {
  echo "csp-check: the manager does not answer" >&2
  exit 2
}

args=(--base-url "http://127.0.0.1:${port}" --storage-state "${state}" --report "${report}")
while IFS= read -r page; do args+=(--page "${page}"); done < <(
  cd src && uv run --frozen python ../tests/design_serve.py --list-pages
)
if [[ "${summary}" != "-" ]]; then args+=(--summary "${summary}"); fi
node tests/csp_browser.mjs "${args[@]}" "${candidates[@]}"
