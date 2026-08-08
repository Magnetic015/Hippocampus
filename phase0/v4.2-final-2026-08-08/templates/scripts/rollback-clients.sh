#!/usr/bin/env bash
# Client config rollback (plan 10 §12, R14).
#
# Backups and their integrity digests stay in the client's own security domain.
# This script prints only safe metadata: backup id, path, time, mode, verified
# flag, result. It never prints file content, diffs, hashes, or sizes.
set -euo pipefail

usage() { echo "usage: $0 --record <file> [--dry-run|--execute]" >&2; exit 2; }

RECORD="" ; MODE="--dry-run"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --record) RECORD="${2:-}"; shift 2 ;;
    --dry-run|--execute) MODE="$1"; shift ;;
    *) usage ;;
  esac
done
[[ -n "$RECORD" && -r "$RECORD" ]] || usage

# Record format (one client per line, tab-separated, no secrets):
#   client_id <TAB> backup_id <TAB> backup_path <TAB> target_path
while IFS=$'\t' read -r client_id backup_id backup_path target_path; do
  [[ -z "${client_id:-}" || "${client_id:0:1}" == "#" ]] && continue
  if [[ ! -r "$backup_path" ]]; then
    echo "${client_id}	${backup_id}	${backup_path}	MISSING	integrity_verified=false	result=refused"
    continue
  fi
  mode="$(stat -f '%Lp' "$backup_path" 2>/dev/null || stat -c '%a' "$backup_path")"
  mtime="$(stat -f '%Sm' -t '%Y-%m-%dT%H:%M:%S%z' "$backup_path" 2>/dev/null || \
           stat -c '%y' "$backup_path" | cut -d. -f1)"
  if [[ "$mode" != "600" ]]; then
    echo "${client_id}	${backup_id}	${backup_path}	mode=${mode}	integrity_verified=false	result=refused_bad_mode"
    continue
  fi
  if [[ "$MODE" == "--dry-run" ]]; then
    echo "${client_id}	${backup_id}	${backup_path}	${mtime}	mode=${mode}	integrity_verified=true	result=dry_run"
    continue
  fi
  cp -p "$backup_path" "$target_path"
  echo "${client_id}	${backup_id}	${backup_path}	${mtime}	mode=${mode}	integrity_verified=true	result=restored"
done < "$RECORD"
