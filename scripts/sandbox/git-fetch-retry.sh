#!/usr/bin/env bash
# `git fetch` that survives transient remote failures.
#
# GitHub answers bursts of anonymous git-over-HTTPS traffic with HTTP 429
# ("This request was rate-limited due to too many requests"). The install /
# update E2E matrix fans ten legs out at once, each fetching the upstream
# repository from a shared runner egress, and has lost legs to exactly that
# (run 34128019604: three legs, all 429). A ref that genuinely does not exist
# is NOT retried, so callers keep their own fallbacks (fetch main and resolve
# the SHA locally) and unresolvable refs still fail fast.
#
# Usage: git_fetch_retry <repo-dir> <fetch args...>
#   runs `git -C <repo-dir> fetch -q <fetch args...>`
#
# Knobs (mainly for tests; the defaults wait up to 150s in total):
#   HERMES_SANDBOX_FETCH_RETRIES      attempts, default 5
#   HERMES_SANDBOX_FETCH_RETRY_DELAY  first backoff in seconds, doubled each
#                                     retry, default 10

# Messages git prints for failures worth another try: rate limiting, server
# errors, dropped or half-open connections, DNS or TLS hiccups.
GIT_FETCH_TRANSIENT_PATTERN='returned error: (429|5[0-9][0-9])|rate-limited|rate limited|early EOF|connection reset|could not resolve host|couldn.t connect to server|timed out|remote end hung up|rpc failed|unexpected disconnect|gnutls recv error|ssl_read|transfer closed'

git_fetch_retry() {
  local repo=$1
  shift
  local attempts="${HERMES_SANDBOX_FETCH_RETRIES:-5}"
  local delay="${HERMES_SANDBOX_FETCH_RETRY_DELAY:-10}"
  local attempt=1 err=""
  while :; do
    if err="$(git -C "$repo" fetch -q "$@" 2>&1)"; then
      return 0
    fi
    if ! printf '%s' "$err" | grep -qiE "$GIT_FETCH_TRANSIENT_PATTERN"; then
      # Permanent (unknown ref, bad URL): report once and let the caller decide.
      [ -n "$err" ] && printf '%s\n' "$err" >&2
      return 1
    fi
    if [ "$attempt" -ge "$attempts" ]; then
      printf '%s\n' "$err" >&2
      echo "error: git fetch still failing after $attempts attempts" >&2
      return 1
    fi
    echo "[sandbox] git fetch failed (attempt $attempt/$attempts), retrying in ${delay}s: $(printf '%s' "$err" | tail -n 1)" >&2
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
  done
}
