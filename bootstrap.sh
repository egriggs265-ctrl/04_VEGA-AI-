#!/usr/bin/env bash
# bootstrap.sh — the ONE startup script for the vega toolkit.
#
# Hit it and everything works. Detects the OS/distro (Kali, Parrot,
# Debian/Ubuntu, Fedora, Arch, Termux, macOS), checks python3 >= 3.10,
# creates the working dirs, and runs every program's --self-test with a
# clear PASS/FAIL summary.
#
# Universal code (Elliot's rule): nothing is assumed about the machine.
# Everything is probed — distro, python, systemd — and every path is
# resolved at runtime. No hardcoded paths, no sudo, no network.
#
# Usage:
#   bash bootstrap.sh [--root DIR]            # probe + create dirs + run suite
#   bash bootstrap.sh --check                 # probe only: report, change nothing
#   bash bootstrap.sh --install [--root DIR]  # + 24/7 persistence (user-level)
#   bash bootstrap.sh --uninstall             # remove persistence
#   bash bootstrap.sh --self-test             # test the bootstrapper itself
#
# Self-test fakes (hermetic, no real system touched):
#   VEGA_FAKE_OS_RELEASE  path to a fake os-release file
#   VEGA_FAKE_PREFIX      fake Termux $PREFIX
#   VEGA_FAKE_UNAME       fake `uname -s` output
#   VEGA_FAKE_SYSTEMD=1|0 fake `systemctl --user` presence
#   VEGA_FAKE_CRONTAB     file standing in for the user crontab
#   VEGA_SYSTEMD_USER_DIR unit install dir (default ~/.config/systemd/user)
set -euo pipefail

PROG_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="./vega-root"
MODE="bootstrap"
CRON_MARKER="# vega-agent-persistence (bootstrap.sh)"
SERVICE_NAME="vega-agent.service"

PY_PROGS=(vega-watchdog.py vega-health.py vega-inbox.py vega-rollup.py vega-sensor.py vega-ask.py vega-cron.py vega-status.py)
SH_PROGS=(vega-logrotate.sh vega-talk.sh vega-restore.sh)

usage() {
  echo "usage: $0 [--root DIR] [--check | --install | --uninstall]"
  echo "       $0 --self-test"
  echo "  (no flags)  probe + create dirs + run the full self-test suite"
  echo "  --check     probe only: report what would happen, change nothing"
  echo "  --install   also install 24/7 persistence (systemd user unit, else cron @reboot)"
  echo "  --uninstall remove the persistence installed by --install"
}

# detect_distro: one of kali parrot debian ubuntu fedora arch termux macos unknown
detect_distro() {
  local prefix="${VEGA_FAKE_PREFIX:-${PREFIX:-}}"
  if [[ "$prefix" == *"com.termux"* ]]; then echo "termux"; return; fi
  local un=""; un="${VEGA_FAKE_UNAME:-$(uname -s)}"
  if [[ "$un" == "Darwin" ]]; then echo "macos"; return; fi
  local osr="${VEGA_FAKE_OS_RELEASE:-/etc/os-release}"
  local id="" idlike=""
  if [[ -r "$osr" ]]; then
    # NB: grep exits 1 on no match — the || true keeps set -e/pipefail alive
    id="$(grep -E '^ID=' "$osr" | head -1 | cut -d= -f2 | tr -d "\"'" || true)"
    idlike="$(grep -E '^ID_LIKE=' "$osr" | head -1 | cut -d= -f2 | tr -d "\"'" || true)"
  fi
  case "$id" in
    kali) echo "kali";;
    parrot) echo "parrot";;
    debian) echo "debian";;
    ubuntu) echo "ubuntu";;
    fedora) echo "fedora";;
    arch) echo "arch";;
    *)
      case "$idlike" in
        *debian*) echo "debian";;
        *fedora*|*rhel*|*centos*) echo "fedora";;
        *arch*) echo "arch";;
        *) echo "unknown";;
      esac;;
  esac
}

# check_python: print "3.12.3"-style version on success, return 1 if missing/old
check_python() {
  command -v python3 >/dev/null 2>&1 || return 1
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null || return 1
  python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'
}

has_systemd_user() {
  if [[ -n "${VEGA_FAKE_SYSTEMD:-}" ]]; then
    [[ "$VEGA_FAKE_SYSTEMD" == "1" ]]
    return
  fi
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user show-environment >/dev/null 2>&1
}

sysd_user_dir() { echo "${VEGA_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"; }

cron_read() {
  if [[ -n "${VEGA_FAKE_CRONTAB:-}" ]]; then
    [[ -f "$VEGA_FAKE_CRONTAB" ]] && cat "$VEGA_FAKE_CRONTAB" || true
  else
    crontab -l 2>/dev/null || true
  fi
}

cron_write() {  # reads new crontab on stdin
  if [[ -n "${VEGA_FAKE_CRONTAB:-}" ]]; then
    cat > "$VEGA_FAKE_CRONTAB"
  else
    crontab -
  fi
}

make_dirs() {  # make_dirs <root>
  mkdir -p "$1"/{run,state,logs,inbox,outbox}
}

# run_suite <prog_dir>: run every --self-test, print summary, return #failed
run_suite() {
  local home="$1" pass=0 fail=0 p
  echo "[bootstrap] running self-test suite from $home"
  for p in "${PY_PROGS[@]}"; do
    if [[ -f "$home/$p" ]] && python3 "$home/$p" --self-test >/dev/null 2>&1; then
      pass=$((pass+1)); echo "  [PASS] $p"
    else
      fail=$((fail+1)); echo "  [FAIL] $p"
    fi
  done
  for p in "${SH_PROGS[@]}"; do
    if [[ -f "$home/$p" ]] && bash "$home/$p" --self-test >/dev/null 2>&1; then
      pass=$((pass+1)); echo "  [PASS] $p"
    else
      fail=$((fail+1)); echo "  [FAIL] $p"
    fi
  done
  echo "[bootstrap] suite: $pass passed, $fail failed"
  return "$fail"
}

# probe_report: print the environment report; return 0 if python is usable
probe_report() {
  local distro pyver
  distro="$(detect_distro)"
  echo "[vega bootstrap]"
  echo "  distro:       $distro"
  if pyver="$(check_python)"; then
    echo "  python3:      $pyver (>= 3.10 ok)"
  else
    echo "  python3:      MISSING or < 3.10 — install python3 first"
    return 1
  fi
  if [[ "${VEGA_FAKE_PREFIX:-${PREFIX:-}}" == *"com.termux"* ]]; then
    echo "  termux:       yes"
  else
    echo "  termux:       no"
  fi
  if has_systemd_user; then
    echo "  persistence:  systemd user unit ($(sysd_user_dir))"
  else
    echo "  persistence:  cron @reboot (no systemd user session)"
  fi
  local n=0 p
  for p in "${PY_PROGS[@]}" "${SH_PROGS[@]}"; do
    [[ -f "$PROG_DIR/$p" ]] && n=$((n+1))
  done
  echo "  programs:     $n of $(( ${#PY_PROGS[@]} + ${#SH_PROGS[@]} )) found in $PROG_DIR"
  return 0
}

# ensure_jobs_json <root>: write a starter manifest only if none exists
ensure_jobs_json() {
  local root="$1" jf="$1/jobs.json"
  [[ -f "$jf" ]] && return 0
  ROOT="$root" JF="$jf" python3 - <<'EOF'
import json, os
root = os.environ["ROOT"]
manifest = {
    "jobs": [{
        "name": "inbox-dispatcher",
        "heartbeat_file": root + "/state/heartbeat.json",
        "expected_interval_s": 60,
        "max_missed": 3,
        "rearm": {"cmd": "note",
                  "args": {"text": "inbox dispatcher heartbeat missed — restart: python3 vega-inbox.py --root ROOT --loop 5"}}
    }],
    "inbox_dir": root + "/inbox",
    "status_file": root + "/logs/watchdog-jobs.json",
    "heartbeat_file": root + "/logs/watchdog.heartbeat",
    "_note": "starter manifest written by bootstrap.sh --install; edit freely",
}
tmp = os.environ["JF"] + ".tmp"
open(tmp, "w").write(json.dumps(manifest, indent=2) + "\n")
os.replace(tmp, os.environ["JF"])
EOF
  echo "[bootstrap] wrote starter $jf (edit it for your jobs)"
}

# ensure_schedules_json <root>: starter schedules for vega-cron.py, if none exists
ensure_schedules_json() {
  local root="$1" sf="$1/schedules.json"
  [[ -f "$sf" ]] && return 0
  ROOT="$root" SF="$sf" python3 - <<'EOF'
import json, os
root = os.environ["ROOT"]
schedules = {
    "schedules": [
        {"name": "hourly-health",
         "every_s": 3600,
         "cmd": {"cmd": "note",
                 "args": {"text": "hourly health sweep: run vega-health.py and vega-status.py"}},
         "catchup": True, "enabled": True},
        {"name": "daily-rollup",
         "every_s": 86400,
         "cmd": {"cmd": "note",
                 "args": {"text": "daily rollup: run vega-rollup.py --append WORKLOG.md"}},
         "catchup": True, "enabled": True},
    ],
    "inbox_dir": root + "/inbox",
    "state_file": root + "/state/cron.json",
    "heartbeat_file": root + "/state/cron.heartbeat",
    "_note": ("starter schedules written by bootstrap.sh --install; "
              "run: python3 vega-cron.py --schedules SCHED --root ROOT --loop 30; "
              "point a watchdog job at state/cron.heartbeat to supervise it"),
}
tmp = os.environ["SF"] + ".tmp"
open(tmp, "w").write(json.dumps(schedules, indent=2) + "\n")
os.replace(tmp, os.environ["SF"])
EOF
  echo "[bootstrap] wrote starter $sf (edit it for your schedules)"
}

do_install() {  # do_install <root>
  local root="$1" home="$PROG_DIR" py3 unit src
  mkdir -p "$root"
  root="$(cd "$root" && pwd)"  # absolute from here on
  py3="$(command -v python3)"
  ensure_jobs_json "$root"
  ensure_schedules_json "$root"
  if has_systemd_user; then
    local udir; udir="$(sysd_user_dir)"
    mkdir -p "$udir"
    src="$home/vega-agent.service"
    [[ -f "$src" ]] || { echo "[bootstrap] missing $src" >&2; return 1; }
    # escape sed metacharacters in paths (&, |, backslash)
    local home_esc root_esc py3_esc
    home_esc="$(printf '%s' "$home" | sed -e 's/[\\&|]/\\&/g')"
    root_esc="$(printf '%s' "$root" | sed -e 's/[\\&|]/\\&/g')"
    py3_esc="$(printf '%s' "$py3" | sed -e 's/[\\&|]/\\&/g')"
    sed -e "s|@VEGA_HOME@|$home_esc|g" -e "s|@VEGA_ROOT@|$root_esc|g" -e "s|@PY3@|$py3_esc|g" \
      "$src" > "$udir/$SERVICE_NAME.tmp"
    mv "$udir/$SERVICE_NAME.tmp" "$udir/$SERVICE_NAME"
    echo "[bootstrap] installed $udir/$SERVICE_NAME"
    if [[ -z "${VEGA_FAKE_SYSTEMD:-}" ]]; then
      if systemctl --user daemon-reload && systemctl --user enable --now "$SERVICE_NAME"; then
        echo "[bootstrap] enabled and started $SERVICE_NAME (systemctl --user)"
      else
        echo "[bootstrap] warning: systemctl --user failed — unit file is installed at $udir/$SERVICE_NAME; enable it by hand" >&2
      fi
    else
      echo "[bootstrap] (fake systemd: skipped daemon-reload/enable)"
    fi
  else
    if [[ -z "${VEGA_FAKE_CRONTAB:-}" ]] && ! command -v crontab >/dev/null 2>&1; then
      echo "[bootstrap] cannot install persistence: no systemd user session and no crontab(1) found" >&2
      return 1
    fi
    local line="@reboot $py3 $home/vega-watchdog.py --root $root --manifest $root/jobs.json --loop 60 >> $root/logs/watchdog.log 2>&1"
    if cron_read | grep -qF "$CRON_MARKER"; then
      echo "[bootstrap] cron persistence already installed — skipping (idempotent)"
    else
      { cron_read; echo "$CRON_MARKER"; echo "$line"; } | cron_write
      echo "[bootstrap] installed cron @reboot persistence"
    fi
  fi
  echo "[bootstrap] 24/7: vega-watchdog.py --loop 60 supervises from $root"
}

do_uninstall() {
  local removed=0 udir unit
  udir="$(sysd_user_dir)"; unit="$udir/$SERVICE_NAME"
  if [[ -f "$unit" ]]; then
    if [[ -z "${VEGA_FAKE_SYSTEMD:-}" ]] && has_systemd_user; then
      systemctl --user disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
      systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
    rm -f "$unit"
    echo "[bootstrap] removed $unit"; removed=1
  fi
  if cron_read | grep -qF "$CRON_MARKER"; then
    # remove exactly the marker line plus the one @reboot line it installed —
    # never another root's (or anyone else's) crontab lines
    cron_read | awk -v m="$CRON_MARKER" \
      '$0 == m { skip = 1; next } skip { skip = 0; next } { print }' \
      | cron_write
    echo "[bootstrap] removed cron @reboot persistence"; removed=1
  fi
  [[ "$removed" -eq 0 ]] && echo "[bootstrap] nothing to remove — no vega persistence found"
  return 0
}

self_test() {
  set +e  # tests intentionally provoke non-zero exits
  local pass=0 fail=0
  check() { # check <label> <expected> <actual>
    if [[ "$2" == "$3" ]]; then pass=$((pass+1)); echo "  PASS $1";
    else fail=$((fail+1)); echo "  FAIL $1 (expected [$2] got [$3])"; fi
  }
  echo "[bootstrap self-test]"
  local td; td="$(mktemp -d)"

  # --- distro detection fakes ---
  mk() { printf '%s\n' "$2" > "$td/os-$1"; }
  mk kali 'ID=kali
ID_LIKE=debian'
  # (direct function test via the hidden --print-distro helper)
  detect() { VEGA_FAKE_OS_RELEASE="$1" VEGA_FAKE_UNAME="${2:-Linux}" VEGA_FAKE_PREFIX="${3:-}" bash "$0" --print-distro; }
  check "kali detected" "kali" "$(detect "$td/os-kali")"
  mk parrot 'ID=parrot
ID_LIKE=debian'
  check "parrot detected" "parrot" "$(detect "$td/os-parrot")"
  mk ubuntu 'ID=ubuntu
ID_LIKE=debian'
  check "ubuntu detected" "ubuntu" "$(detect "$td/os-ubuntu")"
  mk deb12 'ID=debian'
  check "debian detected" "debian" "$(detect "$td/os-deb12")"
  mk rhel 'ID="rhel"
ID_LIKE="fedora"'
  check "fedora via ID_LIKE" "fedora" "$(VEGA_FAKE_OS_RELEASE="$td/os-rhel" bash "$0" --print-distro)"
  mk arch 'ID=arch'
  check "arch detected" "arch" "$(VEGA_FAKE_OS_RELEASE="$td/os-arch" bash "$0" --print-distro)"
  check "termux via fake PREFIX" "termux" "$(VEGA_FAKE_PREFIX=/data/data/com.termux/files/usr VEGA_FAKE_OS_RELEASE=/nonexistent bash "$0" --print-distro)"
  check "macos via fake uname" "macos" "$(VEGA_FAKE_UNAME=Darwin VEGA_FAKE_OS_RELEASE=/nonexistent bash "$0" --print-distro)"
  check "unknown when nothing matches" "unknown" "$(VEGA_FAKE_OS_RELEASE=/nonexistent VEGA_FAKE_UNAME=FreeBSD bash "$0" --print-distro)"

  # --- --check changes nothing ---
  out="$(bash "$0" --check --root "$td/probe-root" 2>&1)"; rc=$?
  check "--check exits 0" "0" "$rc"
  check "--check creates no dirs" "0" "$([[ -e "$td/probe-root" ]] && echo 1 || echo 0)"
  check "--check reports distro" "1" "$(echo "$out" | grep -c 'distro:')"
  check "--check reports python" "1" "$(echo "$out" | grep -c 'python3:')"
  check "--check says probe only" "1" "$(echo "$out" | grep -c 'probe only')"

  # --- full bootstrap creates dirs and runs the suite ---
  out="$(bash "$0" --root "$td/full-root" 2>&1)"; rc=$?
  check "bootstrap exits 0 when suite passes" "0" "$rc"
  for d in run state logs inbox outbox; do
    check "dir created: $d" "1" "$([[ -d "$td/full-root/$d" ]] && echo 1 || echo 0)"
  done
  check "suite summary printed" "1" "$(echo "$out" | grep -c 'suite:.*passed')"

  # --- --install via fake cron (no systemd), idempotent ---
  export VEGA_FAKE_SYSTEMD=0 VEGA_FAKE_CRONTAB="$td/crontab" VEGA_SYSTEMD_USER_DIR="$td/systemd"
  out="$(bash "$0" --install --root "$td/svc-root" 2>&1)"; rc=$?
  check "install exits 0" "0" "$rc"
  check "cron @reboot line installed" "1" "$(grep -c '@reboot.*vega-watchdog.py' "$td/crontab")"
  check "starter jobs.json written" "1" "$([[ -f "$td/svc-root/jobs.json" ]] && echo 1 || echo 0)"
  check "starter schedules.json written" "1" "$([[ -f "$td/svc-root/schedules.json" ]] && echo 1 || echo 0)"
  check "starter schedules.json has two schedules" "2" \
    "$(python3 -c "import json;d=json.load(open('$td/svc-root/schedules.json'));print(len(d['schedules']))")"
  check "starter schedules.json loads via vega-cron --once" "0" \
    "$(python3 vega-cron.py --schedules "$td/svc-root/schedules.json" --root "$td/svc-root" --once >/dev/null 2>&1; echo $?)"
  bash "$0" --install --root "$td/svc-root" >/dev/null 2>&1
  check "install is idempotent (one @reboot line)" "1" "$(grep -c '@reboot.*vega-watchdog.py' "$td/crontab")"
  check "install is idempotent (schedules.json untouched)" "1" \
    "$([[ -f "$td/svc-root/schedules.json" ]] && echo 1 || echo 0)"
  out="$(bash "$0" --uninstall 2>&1)"; rc=$?
  check "uninstall exits 0" "0" "$rc"
  check "cron line removed" "0" "$(grep -c 'vega-watchdog.py' "$td/crontab" || true)"

  # --- --install via fake systemd: unit file with substituted paths ---
  export VEGA_FAKE_SYSTEMD=1
  out="$(bash "$0" --install --root "$td/svc2-root" 2>&1)"; rc=$?
  check "systemd install exits 0" "0" "$rc"
  check "unit file written" "1" "$([[ -f "$td/systemd/$SERVICE_NAME" ]] && echo 1 || echo 0)"
  check "no unsubstituted placeholders" "0" "$(grep -c '@VEGA_' "$td/systemd/$SERVICE_NAME" || true)"
  check "unit points at watchdog" "1" "$(grep -c 'vega-watchdog.py' "$td/systemd/$SERVICE_NAME")"
  out="$(bash "$0" --uninstall 2>&1)"
  check "unit file removed" "0" "$([[ -f "$td/systemd/$SERVICE_NAME" ]] && echo 1 || echo 0)"

  # --- --install with neither systemd nor crontab: clean refusal, exit 1 ---
  unset VEGA_FAKE_SYSTEMD
  mkdir -p "$td/fakebin"
  for d in /usr/bin /bin; do
    [[ -d "$d" ]] || continue
    for b in "$d"/*; do
      n="${b##*/}"
      [[ "$n" == "crontab" ]] && continue
      [[ -e "$td/fakebin/$n" ]] || ln -s "$b" "$td/fakebin/$n"
    done
  done
  out="$(env -u VEGA_FAKE_CRONTAB PATH="$td/fakebin" VEGA_FAKE_SYSTEMD=0 \
    VEGA_FAKE_UNAME=Linux VEGA_FAKE_OS_RELEASE=/nonexistent \
    bash "$0" --install --root "$td/nocron-root" 2>&1)"; rc=$?
  check "install with no crontab refuses (exit 1)" "1" "$rc"
  check "install with no crontab explains why" "1" "$(echo "$out" | grep -c 'no crontab')"

  rm -rf "$td"
  echo "[bootstrap self-test] $pass passed, $fail failed"
  [[ "$fail" -eq 0 ]]
}

# hidden helper for tests: print distro and exit
if [[ "${1:-}" == "--print-distro" ]]; then detect_distro; exit 0; fi
if [[ "${1:-}" == "--self-test" ]]; then self_test; exit $?; fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT="$2"; shift 2;;
    --check) MODE="check"; shift;;
    --install) MODE="install"; shift;;
    --uninstall) MODE="uninstall"; shift;;
    -h|--help) usage; exit 0;;
    -*) echo "unknown arg: $1" >&2; usage; exit 2;;
    *) echo "unexpected arg: $1" >&2; usage; exit 2;;
  esac
done

case "$MODE" in
  check)
    probe_report
    rc=$?
    echo "  mode:         probe only — nothing was changed"
    exit "$rc";;
  uninstall)
    do_uninstall;;
  install)
    probe_report || { echo "[bootstrap] refusing install: environment probe failed" >&2; exit 1; }
    make_dirs "$ROOT"
    echo "[bootstrap] working dirs ready under $ROOT"
    if ! run_suite "$PROG_DIR"; then
      echo "[bootstrap] refusing install: self-tests failed" >&2; exit 1
    fi
    # --install defaults to a home-relative root so services survive cwd changes
    if [[ "$ROOT" == "./vega-root" ]]; then ROOT="$HOME/vega-root"; fi
    make_dirs "$ROOT"
    do_install "$ROOT";;
  bootstrap)
    probe_report || exit 1
    make_dirs "$ROOT"
    echo "[bootstrap] working dirs ready under $ROOT"
    run_suite "$PROG_DIR";;
esac
