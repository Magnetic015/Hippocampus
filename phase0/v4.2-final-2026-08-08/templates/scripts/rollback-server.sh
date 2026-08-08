#!/usr/bin/env bash
# Server rollback (plan 10 §12, fix F2).
#
# The script has no built-in project name: it acts only on what an environment
# rollback manifest declares, and only when the live Compose objects carry the
# manifest's project and BOTH labels. That is what lets Phase 0 rehearse
# --execute against a disposable project while the production manifest is
# still refused there.
#
# Never runs `down -v`: named volumes and their data are always preserved.
set -euo pipefail

usage() { echo "usage: $0 --manifest <file> [--dry-run|--execute]" >&2; exit 2; }

MANIFEST="" ; MODE="--dry-run"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) MANIFEST="${2:-}"; shift 2 ;;
    --dry-run|--execute) MODE="$1"; shift ;;
    *) usage ;;
  esac
done
[[ -n "$MANIFEST" && -r "$MANIFEST" ]] || usage

# shellcheck disable=SC1090
set -a; source "$MANIFEST"; set +a
: "${ROLLBACK_PROJECT:?manifest missing ROLLBACK_PROJECT}"
: "${ROLLBACK_LABEL_MANAGED:?manifest missing ROLLBACK_LABEL_MANAGED}"
: "${ROLLBACK_LABEL_PLAN:?manifest missing ROLLBACK_LABEL_PLAN}"
: "${ROLLBACK_COMPOSE_FILE:?manifest missing ROLLBACK_COMPOSE_FILE}"

fail() { echo "REFUSED: $*" >&2; exit 1; }

containers="$(docker ps -aq \
  --filter "label=com.docker.compose.project=${ROLLBACK_PROJECT}" \
  --filter "label=${ROLLBACK_LABEL_MANAGED}" \
  --filter "label=${ROLLBACK_LABEL_PLAN}" | sort)"
all_in_project="$(docker ps -aq \
  --filter "label=com.docker.compose.project=${ROLLBACK_PROJECT}" | sort)"

[[ -n "$all_in_project" ]] || fail "no containers for project ${ROLLBACK_PROJECT}"
[[ "$containers" == "$all_in_project" ]] || \
  fail "project ${ROLLBACK_PROJECT} has containers missing ${ROLLBACK_LABEL_MANAGED} / ${ROLLBACK_LABEL_PLAN}"

echo "manifest      : ${MANIFEST}"
echo "project       : ${ROLLBACK_PROJECT}"
echo "labels        : ${ROLLBACK_LABEL_MANAGED} ${ROLLBACK_LABEL_PLAN}"
echo "containers    : $(echo "$containers" | wc -l | tr -d ' ')"
echo "volumes kept  : $(docker volume ls -q --filter "label=${ROLLBACK_LABEL_PLAN}" | tr '\n' ' ')"

cmd=(docker compose -p "${ROLLBACK_PROJECT}" -f "${ROLLBACK_COMPOSE_FILE}")
[[ -n "${ROLLBACK_ENV_FILE:-}" ]] && cmd+=(--env-file "${ROLLBACK_ENV_FILE}")
cmd+=(down --remove-orphans)   # deliberately no -v

if [[ "$MODE" == "--dry-run" ]]; then
  echo "DRY-RUN       : ${cmd[*]}"
  exit 0
fi

printf '%s\n' "${cmd[*]}" | grep -q -- ' -v' && fail "refusing: command contains -v"
"${cmd[@]}"

if [[ -n "${ROLLBACK_EXPECT_PORT:-}" ]]; then
  if command -v ss >/dev/null && ss -ltn | grep -q "${ROLLBACK_EXPECT_PORT}"; then
    fail "port ${ROLLBACK_EXPECT_PORT} still listening after down"
  fi
  echo "listener gone : ${ROLLBACK_EXPECT_PORT}"
fi
echo "volumes retained (no -v used)"
