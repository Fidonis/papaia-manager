#!/usr/bin/env bash
# Browser check of the content security policy of the manager (T-UI-03, oberfl 3.3).
#
# Serves the manager in the stand-in world of the tests on 127.0.0.1 (tests/design_serve.py),
# opens every page of ui/pages.yaml in Chromium, signed in as the list's test person of that
# page (Anna for the administration, Emil and Erika for the review pages), in the light and
# the dark colour scheme, and runs tests/csp_browser.mjs: a page fails on any violation of the
# enforced policy, refused Alpine expression or script error, and when it answers without the
# enforced policy. The counts go to the report and, when given, to SUMMARY (the job summary in
# CI). Every full page of the template folder is a page of the list (tests/test_csp_policy.py,
# test_the_pages_of_the_check_list_render_every_full_page_template). Each
# CANDIDATE (LABEL=FILE, an Alpine build) adds the number of expressions per page that build
# cannot evaluate (tests/csp_browser.mjs). Needs the tools of scripts/design-tools.sh.
#
# Usage (from the repository root): scripts/csp-check.sh PORT REPORT [SUMMARY [CANDIDATE ...]]
#   PORT       free port on 127.0.0.1 for the manager
#   REPORT     where the JSON report is written (Anna's pages; the report of every other
#              person goes next to it as <REPORT without .json>.<person>.json)
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
  kill -0 "${server}" 2>/dev/null || { echo "csp-check: the manager did not start" >&2; exit 2; }
  sleep 0.3
done
curl -fsS -o /dev/null "http://127.0.0.1:${port}/health" || {
  echo "csp-check: the manager does not answer" >&2
  exit 2
}

# Exit 1 is findings on a page, 2 a usage error or a page that does not load; the
# worst exit of all persons is the result.
result=0
for person in "${persons[@]}"; do
  if [[ "${person}" == anna ]]; then
    out="${report}"
  else
    out="${report%.json}.${person}.json"
  fi
  args=(--base-url "http://127.0.0.1:${port}" --storage-state "${work}/${person}.json"
    --report "${out}")
  while IFS= read -r page; do args+=(--page "${page}"); done < <(
    cd src && uv run --frozen python ../tests/design_serve.py --list-pages --person "${person}"
  )
  if [[ "${summary}" != "-" ]]; then args+=(--summary "${summary}"); fi
  status=0
  node tests/csp_browser.mjs "${args[@]}" "${candidates[@]}" || status=$?
  if ((status > result)); then result=${status}; fi
done
exit "${result}"
