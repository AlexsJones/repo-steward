#!/usr/bin/env bash
# Bounded retry wrapper for read-only GitHub queries used by a steward tick.
# Usage: bash gh_retry.sh gh api ...  (or gh issue/pr list/view ...)
# It deliberately does not decide whether a command is safe to retry: callers
# must use it only for reads. A GitHub rate limit exits immediately as 75.
set -uo pipefail

attempt=1
max_attempts=4
delays=(2 5 10)

while true; do
  output="$("$@" 2>&1)"
  rc=$?
  if [[ $rc -eq 0 ]]; then
    printf '%s\n' "$output"
    exit 0
  fi

  if grep -Eqi 'rate limit|secondary rate limit|HTTP 429' <<<"$output"; then
    printf '%s\n' "$output" >&2
    exit 75
  fi

  if ! grep -Eqi 'could not resolve host|temporary failure|network is unreachable|connection refused|connection reset|connection timed out|i/o timeout|TLS handshake timeout|unexpected EOF|EOF|HTTP 5(00|02|03|04)' <<<"$output" ||
     [[ $attempt -ge $max_attempts ]]; then
    printf '%s\n' "$output" >&2
    exit "$rc"
  fi

  delay="${delays[$((attempt - 1))]}"
  printf 'GitHub read failed transiently (attempt %s/%s); retrying in %ss\n' \
    "$attempt" "$max_attempts" "$delay" >&2
  sleep "$delay"
  ((attempt += 1))
done
