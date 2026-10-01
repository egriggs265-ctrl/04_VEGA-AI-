#!/usr/bin/env bash
# vega-logrotate.sh — size + age log rotation for run logs.
#
# Lineage: hexshield-log-rotate (size-based: >128KB -> archive + keep last
# 400 lines, retain 5 backups) + sentinel-log-rotate (keep 5 newest by mtime)
# + sentinel-clean (>5MB truncate, >14d delete). Adapted to server paths,
# Termux-isms removed, dry-run mode added.
#
# Rules (top-level regular files in LOGDIR only):
#   - older than --keep-days      -> deleted
#   - older than --compress-after -> gzipped (original removed)
#   - *.gz, the manifest itself, and non-regular files are never touched
#
# Writes rotate-manifest.json (atomically via tmp+mv) in real mode.
# In --dry-run mode nothing is changed; the manifest goes to stdout.
#
# Usage:
#   vega-logrotate.sh LOGDIR [--keep-days 14] [--compress-after 7] [--dry-run]
#   vega-logrotate.sh --self-test
set -euo pipefail

MANIFEST_NAME="rotate-manifest.json"

mtime_epoch() { # portable: GNU stat, then BSD stat
  local f="$1" t
  t="$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || echo "")"
  if [[ "$t" =~ ^[0-9]+$ ]]; then echo "$t"; else echo 0; fi
}

usage() {
  echo "usage: $0 LOGDIR [--keep-days N] [--compress-after M] [--dry-run]"
  echo "       $0 --self-test"
}

self_test() {
  local pass=0 fail=0
  check() { # check <label> <expected> <actual>
    if [[ "$2" == "$3" ]]; then pass=$((pass+1)); echo "  PASS $1";
    else fail=$((fail+1)); echo "  FAIL $1 (expected [$2] got [$3])"; fi
  }
  echo "[vega-logrotate self-test]"
  local td; td="$(mktemp -d)"
  # fixture: new.log (now), mid.log (8d old), old.log (15d old), arch.gz (20d old)
  echo "new" > "$td/new.log"
  echo "mid" > "$td/mid.log"
  echo "old" > "$td/old.log"
  echo "arch" > "$td/arch.log"; gzip -f "$td/arch.log"
  touch -d "8 days ago"  "$td/mid.log"
  touch -d "15 days ago" "$td/old.log"
  touch -d "20 days ago" "$td/arch.log.gz"

  # dry-run changes nothing
  bash "$0" "$td" --keep-days 14 --compress-after 7 --dry-run >/dev/null
  check "dry-run keeps new.log"  "1" "$([ -f "$td/new.log" ] && echo 1 || echo 0)"
  check "dry-run keeps mid.log"  "1" "$([ -f "$td/mid.log" ] && echo 1 || echo 0)"
  check "dry-run keeps old.log"  "1" "$([ -f "$td/old.log" ] && echo 1 || echo 0)"
  check "dry-run keeps arch.log.gz" "1" "$([ -f "$td/arch.log.gz" ] && echo 1 || echo 0)"
  check "dry-run writes no manifest" "0" "$([ -f "$td/$MANIFEST_NAME" ] && echo 1 || echo 0)"

  # real run
  bash "$0" "$td" --keep-days 14 --compress-after 7 >/dev/null
  check "new.log untouched"      "1" "$([ -f "$td/new.log" ] && echo 1 || echo 0)"
  check "mid.log gzipped"        "1" "$([ -f "$td/mid.log.gz" ] && echo 1 || echo 0)"
  check "mid.log original gone"  "0" "$([ -f "$td/mid.log" ] && echo 1 || echo 0)"
  check "old.log deleted"        "0" "$([ -f "$td/old.log" ] && echo 1 || echo 0)"
  check "arch.log.gz untouched"  "1" "$([ -f "$td/arch.log.gz" ] && echo 1 || echo 0)"
  check "manifest written"       "1" "$([ -f "$td/$MANIFEST_NAME" ] && echo 1 || echo 0)"
  local scanned compressed deleted
  scanned="$(python3 -c "import json;print(json.load(open('$td/$MANIFEST_NAME'))['scanned'])")"
  compressed="$(python3 -c "import json;print(json.load(open('$td/$MANIFEST_NAME'))['compressed'])")"
  deleted="$(python3 -c "import json;print(json.load(open('$td/$MANIFEST_NAME'))['deleted'])")"
  check "manifest scanned==3 (.gz excluded by design)" "3" "$scanned"
  check "manifest compressed==1"  "1" "$compressed"
  check "manifest deleted==1"     "1" "$deleted"
  # gzipped content intact
  check "gzip round-trip ok" "mid" "$(gzip -dc "$td/mid.log.gz")"
  rm -rf "$td"
  echo "[vega-logrotate self-test] $pass passed, $fail failed"
  [[ "$fail" -eq 0 ]]
}

if [[ "${1:-}" == "--self-test" ]]; then self_test; exit $?; fi
[[ $# -ge 1 ]] || { usage; exit 2; }

LOGDIR="$1"; shift
KEEP_DAYS=14
COMPRESS_AFTER=7
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-days)      KEEP_DAYS="$2"; shift 2;;
    --compress-after) COMPRESS_AFTER="$2"; shift 2;;
    --dry-run)        DRY_RUN=1; shift;;
    -h|--help)        usage; exit 0;;
    *) echo "unknown arg: $1" >&2; usage; exit 2;;
  esac
done

[[ -d "$LOGDIR" ]] || { echo "not a directory: $LOGDIR" >&2; exit 2; }

now="$(date +%s)"
scanned=0; compressed=0; deleted=0
declare -a actions=()

while IFS= read -r -d '' f; do
  base="$(basename "$f")"
  [[ "$base" == "$MANIFEST_NAME" ]] && continue
  [[ "$base" == *.gz ]] && continue
  scanned=$((scanned+1))
  mt="$(mtime_epoch "$f")"
  age_days=$(( (now - mt) / 86400 ))
  if [[ "$age_days" -gt "$KEEP_DAYS" ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then actions+=("would delete: $base (${age_days}d)");
    else rm -f "$f"; actions+=("deleted: $base (${age_days}d)"); fi
    deleted=$((deleted+1))
  elif [[ "$age_days" -gt "$COMPRESS_AFTER" ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then actions+=("would gzip: $base (${age_days}d)");
    else gzip -f "$f"; actions+=("gzipped: $base (${age_days}d)"); fi
    compressed=$((compressed+1))
  fi
done < <(find "$LOGDIR" -maxdepth 1 -type f -print0)

manifest="$(printf '{"ts":%s,"logdir":%s,"keep_days":%s,"compress_after":%s,"dry_run":%s,"scanned":%s,"compressed":%s,"deleted":%s}' \
  "$now" "$(printf '%s' "$LOGDIR" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')" \
  "$KEEP_DAYS" "$COMPRESS_AFTER" "$([ "$DRY_RUN" -eq 1 ] && echo true || echo false)" \
  "$scanned" "$compressed" "$deleted")"

if [[ "$DRY_RUN" -eq 1 ]]; then
  printf '%s\n' "$manifest"
  printf '%s\n' "${actions[@]}"
else
  tmp="$LOGDIR/$MANIFEST_NAME.tmp"
  printf '%s\n' "$manifest" > "$tmp"
  mv "$tmp" "$LOGDIR/$MANIFEST_NAME"
  printf '%s\n' "${actions[@]}"
  echo "[vega-logrotate] scanned=$scanned compressed=$compressed deleted=$deleted"
fi
