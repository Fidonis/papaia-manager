#!/usr/bin/env bash
# Fails unless FILE has the SHA-256 the Dockerfile pins for it, so a library that changed
# under its pinned version stops the image build instead of being shipped.
#
# Usage: verify-sha256 FILE SHA256
#   exit 0 the digest matches, 1 it does not (or FILE cannot be read), 2 usage
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: verify-sha256 FILE SHA256" >&2
  exit 2
fi
file=$1
expected=$2
if ! [[ "${expected}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "verify-sha256: expected a SHA-256 of 64 lowercase hex digits for ${file}" >&2
  exit 2
fi
if [[ ! -f "${file}" ]]; then
  echo "verify-sha256: ${file} is missing" >&2
  exit 1
fi
actual=$(sha256sum "${file}")
actual=${actual%% *}
if [[ "${actual}" != "${expected}" ]]; then
  echo "verify-sha256: ${file} does not match its pinned SHA-256 (got ${actual})" >&2
  exit 1
fi
