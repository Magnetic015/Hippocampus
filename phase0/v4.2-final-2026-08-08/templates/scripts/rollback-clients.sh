#!/usr/bin/env bash
# Client config rollback (plan 10 §12, R14).
#
# Backups and their integrity manifests stay in the client's own security domain.
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

file_mode() {
  stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1"
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -- "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 -- "$1" | awk '{print $1}'
  else
    echo "refusing: no SHA-256 utility available" >&2
    exit 1
  fi
}

record_mode="$(file_mode "$RECORD")"
if [[ "$record_mode" != "600" ]]; then
  echo "refusing: rollback manifest must have mode 0600" >&2
  exit 1
fi

# Manifest format (one client per line, tab-separated, local security domain):
#   client_id <TAB> backup_id <TAB> backup_path <TAB> target_path <TAB> sha256
while IFS=$'\t' read -r client_id backup_id backup_path target_path expected_sha256 extra; do
  [[ -z "${client_id:-}" || "${client_id:0:1}" == "#" ]] && continue
  if [[ -n "${extra:-}" || ! "${expected_sha256:-}" =~ ^[0-9a-f]{64}$ ]]; then
    printf '%s\t%s\t%s\tintegrity_verified=false\tresult=refused_bad_manifest\n' \
      "$client_id" "$backup_id" "$backup_path"
    continue
  fi
  if [[ ! -f "$backup_path" || ! -r "$backup_path" ]]; then
    echo "${client_id}	${backup_id}	${backup_path}	MISSING	integrity_verified=false	result=refused"
    continue
  fi
  mode="$(file_mode "$backup_path")"
  mtime="$(stat -f '%Sm' -t '%Y-%m-%dT%H:%M:%S%z' "$backup_path" 2>/dev/null || \
           stat -c '%y' "$backup_path" | cut -d. -f1)"
  if [[ "$mode" != "600" ]]; then
    echo "${client_id}	${backup_id}	${backup_path}	mode=${mode}	integrity_verified=false	result=refused_bad_mode"
    continue
  fi
  actual_sha256="$(sha256_file "$backup_path")"
  if [[ "$actual_sha256" != "$expected_sha256" ]]; then
    printf '%s\t%s\t%s\t%s\tmode=%s\tintegrity_verified=false\tresult=refused_digest_mismatch\n' \
      "$client_id" "$backup_id" "$backup_path" "$mtime" "$mode"
    continue
  fi
  if [[ "$MODE" == "--dry-run" ]]; then
    echo "${client_id}	${backup_id}	${backup_path}	${mtime}	mode=${mode}	integrity_verified=true	result=dry_run"
    continue
  fi
  cp -p "$backup_path" "$target_path"
  echo "${client_id}	${backup_id}	${backup_path}	${mtime}	mode=${mode}	integrity_verified=true	result=restored"
done < "$RECORD"
