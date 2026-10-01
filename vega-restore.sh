#!/usr/bin/env bash
# vega-restore.sh — golden snapshot/restore for the vega working dirs.
#
# Snapshots run/ + state/ (the mutable agent dirs) into a tarball plus a
# JSON manifest, atomically (temp + mv — never a half-written snapshot).
# Restore extracts over run/ + state/ the same way. Before any
# self-modifying program touches its own tree, snapshot first.
#
# Lineage: sentinel-restore-golden (golden/latest restoration).
#
# Usage:
#   vega-restore.sh --root ROOT snapshot <name>
#   vega-restore.sh --root ROOT restore <name>
#   vega-restore.sh --root ROOT list
#   vega-restore.sh --self-test
#
# Snapshot names: [A-Za-z0-9._-] only. Snapshots live in ROOT/snapshots/.
set -euo pipefail

ROOT=""
SNAPDIR=""

usage() {
  echo "usage: $0 --root ROOT snapshot <name> | restore <name> | list"
  echo "       $0 --self-test"
}

valid_name() { [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]]; }

snap_tar() { echo "$SNAPDIR/$1.tar.gz"; }
snap_meta() { echo "$SNAPDIR/$1.json"; }

do_snapshot() {  # do_snapshot <name>
  local name="$1" tar meta
  valid_name "$name" || { echo "[vega-restore] bad snapshot name: $name" >&2; return 2; }
  mkdir -p "$SNAPDIR" "$ROOT/run" "$ROOT/state"
  tar="$(snap_tar "$name")"; meta="$(snap_meta "$name")"
  tar -czf "$tar.tmp" -C "$ROOT" run state
  local files; files="$(cd "$ROOT" && find run state -type f | sort)"
  ROOT="$ROOT" NAME="$name" FILES="$files" python3 - > "$meta.tmp" <<'EOF'
import json, os, time
d = {"name": os.environ["NAME"], "ts": time.time(),
     "root": os.environ["ROOT"],
     "files": [f for f in os.environ["FILES"].splitlines() if f]}
print(json.dumps(d, indent=2))
EOF
  mv "$tar.tmp" "$tar"
  mv "$meta.tmp" "$meta"
  echo "[vega-restore] snapshot '$name' saved ($(wc -l < "$meta" | tr -d ' ') manifest lines)"
}

do_restore() {  # do_restore <name>
  local name="$1" tar tmpd
  valid_name "$name" || { echo "[vega-restore] bad snapshot name: $name" >&2; return 2; }
  tar="$(snap_tar "$name")"
  [[ -f "$tar" ]] || { echo "[vega-restore] no such snapshot: $name" >&2; return 1; }
  tmpd="$SNAPDIR/.restore.$$"
  rm -rf "$tmpd"; mkdir -p "$tmpd"
  tar -xzf "$tar" -C "$tmpd"
  [[ -d "$tmpd/run" && -d "$tmpd/state" ]] || { echo "[vega-restore] snapshot is corrupt" >&2; rm -rf "$tmpd"; return 1; }
  rm -rf "$ROOT/run" "$ROOT/state"
  mv "$tmpd/run" "$ROOT/run"
  mv "$tmpd/state" "$ROOT/state"
  rm -rf "$tmpd"
  echo "[vega-restore] restored snapshot '$name'"
}

do_list() {
  shopt -s nullglob
  local t n=0
  for t in "$SNAPDIR"/*.tar.gz; do
    local name ts
    name="$(basename "$t" .tar.gz)"
    ts="$(python3 -c "import json;print(json.load(open('$(snap_meta "$name")')).get('ts','?'))" 2>/dev/null || echo '?')"
    echo "$name  ts=$ts"
    n=$((n+1))
  done
  shopt -u nullglob
  echo "[vega-restore] $n snapshot(s)"
}

self_test() {
  set +e
  local pass=0 fail=0
  check() {
    if [[ "$2" == "$3" ]]; then pass=$((pass+1)); echo "  PASS $1";
    else fail=$((fail+1)); echo "  FAIL $1 (expected [$2] got [$3])"; fi
  }
  echo "[vega-restore self-test]"
  local td; td="$(mktemp -d)"
  local R="$td/root"

  # 1. snapshot creates tarball + manifest
  mkdir -p "$R/run" "$R/state"
  echo "v1" > "$R/run/a.txt"; echo "s1" > "$R/state/b.json"
  out="$(bash "$0" --root "$R" snapshot gold 2>&1)"; rc=$?
  check "snapshot exits 0" "0" "$rc"
  check "tarball exists" "1" "$([[ -f "$R/snapshots/gold.tar.gz" ]] && echo 1 || echo 0)"
  check "manifest exists" "1" "$([[ -f "$R/snapshots/gold.json" ]] && echo 1 || echo 0)"
  check "manifest names 2 files" "2" "$(python3 -c "import json;print(len(json.load(open('$R/snapshots/gold.json'))['files']))")"

  # 2. list shows it
  out="$(bash "$0" --root "$R" list 2>&1)"
  check "list shows gold" "1" "$(echo "$out" | grep -c '^gold ')"

  # 3. mutate, restore, verify exact contents
  echo "v2" > "$R/run/a.txt"; echo "junk" > "$R/run/junk.txt"; rm "$R/state/b.json"
  out="$(bash "$0" --root "$R" restore gold 2>&1)"; rc=$?
  check "restore exits 0" "0" "$rc"
  check "run/a.txt back to v1" "v1" "$(cat "$R/run/a.txt")"
  check "junk file gone" "0" "$([[ -f "$R/run/junk.txt" ]] && echo 1 || echo 0)"
  check "state/b.json back" "s1" "$(cat "$R/state/b.json")"

  # 4. second snapshot, list shows two
  bash "$0" --root "$R" snapshot gold2 >/dev/null 2>&1
  out="$(bash "$0" --root "$R" list 2>&1)"
  check "two snapshots listed" "2" "$(echo "$out" | grep -cE '^(gold|gold2) ')"

  # 5. errors
  out="$(bash "$0" --root "$R" restore nosuch 2>&1)"; rc=$?
  check "restore of missing snapshot fails (exit 1)" "1" "$rc"
  out="$(bash "$0" --root "$R" snapshot 'bad/name' 2>&1)"; rc=$?
  check "bad snapshot name rejected (exit 2)" "2" "$rc"
  out="$(bash "$0" --root "$R" bogus 2>&1)"; rc=$?
  check "unknown command is usage error (exit 2)" "2" "$rc"

  rm -rf "$td"
  echo "[vega-restore self-test] $pass passed, $fail failed"
  [[ "$fail" -eq 0 ]]
}

if [[ "${1:-}" == "--self-test" ]]; then self_test; exit $?; fi

CMD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    -*) echo "unknown arg: $1" >&2; usage; exit 2;;
    *) CMD="$1"; NAME="${2:-}"; shift; [[ -n "${NAME:-}" ]] && shift || true; break;;
  esac
done

[[ -n "$ROOT" ]] || { usage >&2; exit 2; }
[[ -d "$ROOT" ]] || { echo "not a directory: $ROOT" >&2; exit 2; }
SNAPDIR="$ROOT/snapshots"

case "$CMD" in
  snapshot) [[ -n "${NAME:-}" ]] || { usage >&2; exit 2; }; do_snapshot "$NAME";;
  restore)  [[ -n "${NAME:-}" ]] || { usage >&2; exit 2; }; do_restore "$NAME";;
  list)     do_list;;
  *) usage >&2; exit 2;;
esac
