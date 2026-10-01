#!/usr/bin/env bash
# vega-talk.sh — operator client for the vega-inbox/outbox protocol.
#
# Lineage: hexwatch's bin/talk.sh (append to inbox, wait on outbox).
# talk.sh's weakness: it waits the full 60s even when the agent is dead.
# vega-talk.sh fixes that with a liveness pre-check: it reads
# state/heartbeat.json (ts + pid) and REFUSES to send when the heartbeat
# is missing, stale, or the pid is dead. No more blind waits.
#
# The vega-inbox protocol is file-per-command: inbox/<id>.json ->
# outbox/<id>.json, exactly one reply per command.
#
# Usage:
#   vega-talk.sh --root ./vega-root ping
#   vega-talk.sh --root ./vega-root echo text=hello world=yes
#   vega-talk.sh --root ./vega-root --json '{"cmd":"ping"}'
#   vega-talk.sh --root ./vega-root --timeout 10 status
#   vega-talk.sh --self-test
#
# Prints the reply on success (exit 0). Exit codes: 0 ok, 1 the dispatcher
# answered with ok:false or the wait timed out, 2 usage error, 3 the
# dispatcher is not live (missing/stale heartbeat or dead pid).
set -euo pipefail

TIMEOUT=30
MAX_STALE=30   # heartbeat older than this -> dispatcher considered dead
HB_PATH=""
ROOT=""

usage() {
  echo "usage: $0 --root ROOT [--timeout S] [--max-stale S] CMD [key=value ...]"
  echo "       $0 --root ROOT --json '{\"cmd\":\"ping\"}'"
  echo "       $0 --self-test"
}

# hb_value <key>: print heartbeat.json's <key> or empty on any failure.
hb_value() {
  HB_PATH="$HB_PATH" python3 -c "
import json,os
try:
    print(json.load(open(os.environ['HB_PATH'])).get('$1',''))
except Exception:
    print('')" 2>/dev/null
}

# liveness_check: 0 if the dispatcher is live, else print why and return 1.
liveness_check() {
  if [[ ! -f "$HB_PATH" ]]; then
    echo "[vega-talk] refusing: no heartbeat ($HB_PATH missing) — is vega-inbox running?" >&2
    return 1
  fi
  local ts pid now age
  ts="$(hb_value ts)"; pid="$(hb_value pid)"
  if ! [[ "$ts" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    echo "[vega-talk] refusing: heartbeat has no numeric ts" >&2
    return 1
  fi
  now="$(date +%s)"
  age="$(python3 -c "print(int($now - float('$ts')))" 2>/dev/null || echo 999999)"
  if [[ "$age" -gt "$MAX_STALE" ]]; then
    echo "[vega-talk] refusing: heartbeat is ${age}s old (max ${MAX_STALE}s) — dispatcher looks dead" >&2
    return 1
  fi
  if [[ -n "$pid" ]] && [[ "$pid" =~ ^[0-9]+$ ]]; then
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "[vega-talk] refusing: heartbeat pid $pid is dead" >&2
      return 1
    fi
  fi
  return 0
}

self_test() {
  set +e  # the tests below intentionally provoke non-zero exits
  local pass=0 fail=0
  check() { # check <label> <expected> <actual>
    if [[ "$2" == "$3" ]]; then pass=$((pass+1)); echo "  PASS $1";
    else fail=$((fail+1)); echo "  FAIL $1 (expected [$2] got [$3])"; fi
  }
  echo "[vega-talk self-test]"
  local td; td="$(mktemp -d)"
  mkdir -p "$td/state" "$td/inbox" "$td/outbox"

  # 1. no heartbeat at all -> refused, exit 3, nothing sent
  out="$(bash "$0" --root "$td" ping 2>&1)"; rc=$?
  check "missing heartbeat refused (exit 3)" "3" "$rc"
  check "nothing queued on refusal" "0" "$(ls "$td/inbox" | wc -l)"

  # 2. stale heartbeat -> refused
  SHELL_PID=$$ HB_PATH="$td/state/heartbeat.json" python3 -c "
import json,time,os; json.dump({'ts': time.time()-600, 'pid': 1}, open(os.environ['HB_PATH'],'w'))"
  out="$(bash "$0" --root "$td" ping 2>&1)"; rc=$?
  check "stale heartbeat refused (exit 3)" "3" "$rc"
  check "stale mentions age" "1" "$(echo "$out" | grep -c 'old')"

  # 3. fresh heartbeat, dead pid -> refused
  SHELL_PID=$$ HB_PATH="$td/state/heartbeat.json" python3 -c "
import json,time,os; json.dump({'ts': time.time(), 'pid': 99999999}, open(os.environ['HB_PATH'],'w'))"
  out="$(bash "$0" --root "$td" ping 2>&1)"; rc=$?
  check "dead pid refused (exit 3)" "3" "$rc"

  # 4. fresh heartbeat (our own pid = alive), fake dispatcher in background
  SHELL_PID=$$ HB_PATH="$td/state/heartbeat.json" python3 -c "
import json,time,os; json.dump({'ts': time.time(), 'pid': int(os.environ['SHELL_PID'])}, open(os.environ['HB_PATH'],'w'))"
  shopt -s nullglob
  (
    # fake dispatcher: answer every inbox file once
    for _ in $(seq 1 150); do
      for f in "$td"/inbox/*.json; do
        id="$(basename "$f" .json)"
        cmd="$(python3 -c "import json;print(json.load(open('$f')).get('cmd',''))")"
        printf '{"id":"%s","ok":true,"reply":"pong-%s"}\n' "$id" "$cmd" \
          > "$td/outbox/$id.json"
        mv "$f" "$td/done_$id.json"
      done
      sleep 0.2
    done
  ) &
  local fake=$!
  shopt -u nullglob
  out="$(bash "$0" --root "$td" ping 2>&1)"; rc=$?
  check "round trip succeeds (exit 0)" "0" "$rc"
  check "reply printed" "pong-ping" "$out"
  out="$(bash "$0" --root "$td" echo text=hi 2>&1)"; rc=$?
  check "key=value args round trip" "0" "$rc"
  check "echo reply printed" "pong-echo" "$out"
  out="$(bash "$0" --root "$td" --json '{"cmd":"status"}' 2>&1)"; rc=$?
  check "--json mode works" "pong-status" "$out"
  kill "$fake" 2>/dev/null; wait "$fake" 2>/dev/null || true

  # 5. live heartbeat but nobody answers -> timeout, returns promptly
  SHELL_PID=$$ HB_PATH="$td/state/heartbeat.json" python3 -c "
import json,time,os; json.dump({'ts': time.time(), 'pid': int(os.environ['SHELL_PID'])}, open(os.environ['HB_PATH'],'w'))"
  start="$(date +%s)"
  out="$(bash "$0" --root "$td" --timeout 2 ping 2>&1)"; rc=$?
  elapsed=$(( $(date +%s) - start ))
  check "timeout is an error (exit 1)" "1" "$rc"
  check "timeout returns promptly (<10s, no blind 60s wait)" "1" "$([ "$elapsed" -lt 10 ] && echo 1 || echo 0)"
  check "timeout says what happened" "1" "$(echo "$out" | grep -c 'timed out')"

  # 6. usage error
  out="$(bash "$0" 2>&1)"; rc=$?
  check "missing --root is usage error (exit 2)" "2" "$rc"

  rm -rf "$td"
  echo "[vega-talk self-test] $pass passed, $fail failed"
  [[ "$fail" -eq 0 ]]
}

if [[ "${1:-}" == "--self-test" ]]; then self_test; exit $?; fi

JSON_CMD=""
CMD_WORDS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)      ROOT="$2"; shift 2;;
    --timeout)   TIMEOUT="$2"; shift 2;;
    --max-stale) MAX_STALE="$2"; shift 2;;
    --json)      JSON_CMD="$2"; shift 2;;
    -h|--help)   usage; exit 0;;
    --) shift; while [[ $# -gt 0 ]]; do CMD_WORDS+=("$1"); shift; done;;
    -*) echo "unknown arg: $1" >&2; usage; exit 2;;
    *) CMD_WORDS+=("$1"); shift;;
  esac
done

[[ -n "$ROOT" ]] || { usage >&2; exit 2; }
[[ -d "$ROOT" ]] || { echo "not a directory: $ROOT" >&2; exit 2; }
HB_PATH="$ROOT/state/heartbeat.json"

if ! liveness_check; then exit 3; fi

# build the command object: {"cmd","args"} (+ id later)
if [[ -n "$JSON_CMD" ]]; then
  if ! JSON_CMD="$JSON_CMD" python3 -c "
import json,os
d=json.loads(os.environ['JSON_CMD'])
assert isinstance(d,dict) and 'cmd' in d" 2>/dev/null; then
    echo "[vega-talk] --json must be an object with a cmd key" >&2; exit 2
  fi
  cmd_json="$JSON_CMD"
else
  [[ ${#CMD_WORDS[@]} -ge 1 ]] || { usage >&2; exit 2; }
  cmd_json="$(python3 - "${CMD_WORDS[@]}" <<'EOF'
import json,sys
words=sys.argv[1:]
args={}
for w in words[1:]:
    if "=" in w:
        k,v=w.split("=",1); args[k]=v
    else:
        args[w]="true"
print(json.dumps({"cmd":words[0],"args":args}))
EOF
)"
fi

cid="$(date +%s)_$$_$RANDOM"
tmp="$ROOT/inbox/.$cid.json.tmp"
# write the command file atomically via env (no shell-quoting of JSON)
CMD_JSON="$cmd_json" CID="$cid" python3 - > "$tmp" <<'EOF'
import json,os
d=json.loads(os.environ["CMD_JSON"])
d.setdefault("id", os.environ["CID"])
print(json.dumps(d))
EOF
mv "$tmp" "$ROOT/inbox/$cid.json"

# wait for outbox/<id>.json
deadline=$(( $(date +%s) + TIMEOUT ))
while [[ $(date +%s) -lt $deadline ]]; do
  if [[ -f "$ROOT/outbox/$cid.json" ]]; then
    OUT_PATH="$ROOT/outbox/$cid.json" python3 - <<'EOF'
import json,os,sys
d=json.load(open(os.environ["OUT_PATH"]))
print(d.get("reply",""))
sys.exit(0 if d.get("ok") else 1)
EOF
    exit $?
  fi
  sleep 0.5
done
echo "[vega-talk] timed out after ${TIMEOUT}s waiting for outbox/$cid.json" >&2
exit 1
