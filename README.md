# 04_VEGA-AI- — Sable's personal repo

[![verify](https://github.com/egriggs265-ctrl/04_VEGA-AI-/actions/workflows/verify.yml/badge.svg)](https://github.com/egriggs265-ctrl/04_VEGA-AI-/actions/workflows/verify.yml)

Built 2026-09-30 by Sable (Muse), from Elliot's code, with his standing
authorization. Improved rebuilds of his proven patterns — his engineering
vocabulary, his monolith shape, his bugs fixed.

## Quick start — one command

    bash bootstrap.sh

That's it. The bootstrapper probes the machine (distro, python3, systemd),
creates the working dirs (`run/ state/ logs/ inbox/ outbox/` under
`./vega-root`), and runs every program's `--self-test` with a PASS/FAIL
summary. Universal code: no hardcoded paths, no sudo, no network —
everything is detected, never assumed.

    bash bootstrap.sh --check                 # probe only: report, change nothing
    bash bootstrap.sh --install [--root DIR]  # + 24/7 persistence (see below)
    bash bootstrap.sh --uninstall             # remove the 24/7 persistence
    bash bootstrap.sh --self-test             # test the bootstrapper itself

## Supported environments

| Environment | Detected as | Notes |
|---|---|---|
| Debian / Ubuntu | `debian` / `ubuntu` | primary CI target |
| Kali Linux | `kali` | pentest distro — stdlib only, nothing to install |
| Parrot Security OS | `parrot` | pentest distro — same |
| Fedora / RHEL family | `fedora` | via `ID_LIKE` fallback |
| Arch Linux | `arch` | via `ID_LIKE` fallback |
| Termux (Android) | `termux` | via `$PREFIX`; cron fallback, no systemd |
| macOS | `macos` | via `uname`; cron fallback, no systemd |

Anything else reports `unknown` but still runs — the probe is advisory,
the suite is the proof. Python 3.10+ is the only requirement.

## Run 24/7

    bash bootstrap.sh --install

Installs persistence as a normal user (never sudo):

- **systemd user unit** where `systemctl --user` works —
  `~/.config/systemd/user/vega-agent.service`, keeps
  `vega-watchdog.py --loop 60` alive with `Restart=always`.
- **cron `@reboot` entry** everywhere else (Termux, macOS, containers
  without a user session) — same watchdog command, idempotent install.

`--install` writes a starter `jobs.json` in the root if none exists (edit
it for your jobs) and refuses to install if the self-test suite fails.
`--uninstall` removes whichever persistence was installed.

## Verification

Every push and pull request runs the full self-test suite via GitHub
Actions (`.github/workflows/verify.yml`): each `vega-*.py --self-test`
plus `bash -n` and `--self-test` on the shell programs. The suite is
hermetic — no network, no pip installs, stdlib only. A failing program
fails the job.

## Lineage

- `vega-watchdog.py` ← `sentinel-watchdog` + `sentinel-daemon` (Android-Sentinel)
- `vega-health.py` ← `sentinel-health` + `sentinel-test` (Android-Sentinel)
- `vega-inbox.py` ← `hexwatch_v6_monolith.py` (Hexwatch V6 / Hexstrike_V6 line)
- `vega-rollup.py` ← monolith rollup thread + `sentinel-report`
- `vega-logrotate.sh` ← `hexshield-log-rotate` + `sentinel-log-rotate` + `sentinel-clean`
- `vega-sensor.py` ← `hexshield/agent.py` (Android-Sentinel)
- `vega-talk.sh` ← hexwatch `bin/talk.sh` (Hexwatch V6)
- `bootstrap.sh` ← new (Elliot's bootloader directive: probe → fill gaps → run)
- `vega-agent.service` ← new (systemd user unit for the watchdog)
- `vega-restore.sh` ← `sentinel-restore-golden` (golden/latest restoration)
- `vega-ask.py` ← hexwatch's Ollama layer (rebuilt on the HTTP API, failure contract explicit)

Full study: `../hexwatch-v6-study-2026-09-30.md` (in the mining folder).

## The shape (his rules, kept)

- One file per program. **Stdlib only** — no pip, no network.
- State as JSON, **every write atomic** (temp + `os.replace`). Hexwatch
  defined `safe_write()` but never used it; here nothing writes any other way.
- File-based IPC: inbox/outbox, heartbeats, JSON status files.
- `--self-test` on everything. 164 fixture assertions, all passing (see below).

## Programs

### vega-watchdog.py — heartbeat supervisor
Reads a job manifest (`jobs.json`): each job's heartbeat file,
`expected_interval_s`, `max_missed`, and a re-arm command. Classifies every
job ok / stale / dead, writes `health/jobs.json`, and drops exactly one
re-arm command per stale/dead job into the inbox dir (dedup: one pending
re-arm per job max). Heartbeat files may be JSON `{"ts"}` or a bare epoch.

    python3 vega-watchdog.py --manifest jobs.json          # one pass
    python3 vega-watchdog.py --manifest jobs.json --loop 60 # supervise

### vega-health.py — one JSON health snapshot
Runs numbered probes from a JSON config — `file_age`, `file_exists`, `cmd`
(with timeout; a hung probe fails instead of hanging the sweep), `json_key` —
and emits one snapshot: per-probe pass/fail + `elapsed_ms`, top-level
verdict `healthy`/`degraded`. Exit code 2 when degraded.

    python3 vega-health.py --probes probes.json --out health/snapshot.json

### vega-inbox.py — inbox/outbox command dispatcher
Drop `{"cmd", "args"}` JSON files in `inbox/`; get exactly one reply JSON in
`outbox/` — including for duplicate-suppressed commands, so a waiting
client never hangs. Builtin handlers: `ping`, `echo`, `status`, `help`,
`note` (the watchdog's re-arm command). Unknown
commands and malformed files get error replies — never a crash, never
silence. Claim-by-rename (`inbox/` → `processing/` → `done/`) plus SHA-256
dedup makes double-replies structurally impossible. Pid guard with
`kill -0` stale-lock detection. Heartbeat + state JSON every cycle.

    python3 vega-inbox.py --root ./vega-root          # drain once, exit
    python3 vega-inbox.py --root ./vega-root --loop 5 # daemon

### vega-rollup.py — daily digest from run logs
Scans `*<date>*.md|log` files, counts explicit markers
(`[OK]` `[FAIL]` `[ERROR]` `[ITEM]` `[SKIP]`) — exact numbers, no
grep-guessing — and prints/appends a markdown digest.

    python3 vega-rollup.py --log-dir ./hidden_files --date 2026-09-30
    python3 vega-rollup.py --log-dir ./hidden_files --date 2026-09-30 --append WORKLOG.md

### vega-logrotate.sh — size + age log rotation
Deletes logs older than `--keep-days` (default 14), gzips logs older than
`--compress-after` (default 7). Never touches `*.gz` or its own manifest.
`--dry-run` changes nothing. Writes `rotate-manifest.json` atomically.

    bash vega-logrotate.sh ./hidden_files --keep-days 14 --compress-after 7 --dry-run

### vega-sensor.py — hexshield-style event sensor
Polls a directory of event files, regex-scores each new line against a
rule set (`--sensors sensors.json`, or built-in auth/security rules),
appends JSONL findings, keeps cumulative per-rule scores, emits a JSON
alert per high-scoring event, and heartbeats. Findings self-rotate at
128KB / 400 lines (hexshield's rule). The portable core of
`hexshield/agent.py` — drop any watcher's output files in `events/`.

    python3 vega-sensor.py --root ./vega-root              # one pass
    python3 vega-sensor.py --root ./vega-root --loop 8     # poll every 8s

### vega-talk.sh — operator client for the inbox/outbox protocol
Sends one command to `vega-inbox.py` and waits for its reply. Fixes
`talk.sh`'s blind 60s wait: refuses to send (exit 3) when
`state/heartbeat.json` is missing, stale, or its pid is dead.

    bash vega-talk.sh --root ./vega-root ping
    bash vega-talk.sh --root ./vega-root echo text=hello
    bash vega-talk.sh --root ./vega-root --json '{"cmd":"status"}'

### bootstrap.sh — the one startup script
Detects the distro (Kali, Parrot, Debian/Ubuntu, Fedora, Arch, Termux,
macOS), checks python3 >= 3.10, creates the working dirs, and runs the
whole suite. `--check` probes without changing anything; `--install`
adds 24/7 persistence (systemd user unit, else cron `@reboot`);
`--uninstall` removes it. No sudo, no network.

    bash bootstrap.sh
    bash bootstrap.sh --install

### vega-agent.service — systemd user unit (template)
Keeps `vega-watchdog.py --loop 60` alive (`Restart=always`). Installed by
`bootstrap.sh --install`, which substitutes the `@VEGA_HOME@`,
`@VEGA_ROOT@`, `@PY3@` placeholders — never edit the installed copy by hand.

### vega-restore.sh — golden snapshot/restore
Snapshots `run/` + `state/` into `snapshots/<name>.tar.gz` with a JSON
manifest, atomically (temp + mv). `restore` puts them back the same way.
Snapshot before any self-modifying program touches its own tree.

    bash vega-restore.sh --root ./vega-root snapshot gold
    bash vega-restore.sh --root ./vega-root restore gold
    bash vega-restore.sh --root ./vega-root list

### vega-ask.py — offline-LLaMA ask layer
POSTs a prompt to Ollama's `/api/generate` on 127.0.0.1:11434 and prints
the response. Model from `VEGA_MODEL` (default `phi3:mini`). Stdlib
`urllib` only. Explicit failure contract: Ollama down → prints
`ollama unavailable` to stderr, exits 2 (distinct from a model error,
exit 1).

    python3 vega-ask.py "summarize the overnight findings"
    echo "is the dispatcher alive?" | python3 vega-ask.py --model llama3.2:3b

## Wiring them together

```
bash bootstrap.sh                        # one command: dirs + full self-test suite
bash bootstrap.sh --install              # 24/7: watchdog via systemd unit or cron @reboot
vega-watchdog.py --loop 60          # supervises everything below
  -> re-arm files -> inbox/
vega-inbox.py --loop 5              # dispatches re-arms + operator commands
vega-talk.sh ping                   # operator CLI against the dispatcher
vega-ask.py "..."                   # ask the offline LLaMA (Ollama on localhost)
vega-sensor.py --loop 8             # scores event files -> findings.jsonl
vega-health.py (hourly, via cron)   # one snapshot for the autonomy sweep
vega-rollup.py (daily, via cron)    # digest appended to WORKLOG
vega-logrotate.sh (daily, via cron) # hygiene
vega-restore.sh snapshot gold       # golden snapshot before risky changes
```

## Self-test evidence (2026-09-30, all on this machine)

| program | assertions | result |
|---|---|---|
| vega-watchdog.py --self-test | 16 | 16 pass |
| vega-health.py --self-test | 15 | 15 pass |
| vega-inbox.py --self-test | 16 | 16 pass |
| vega-rollup.py --self-test | 11 | 11 pass |
| vega-logrotate.sh --self-test | 15 | 15 pass |
| vega-sensor.py --self-test | 18 | 18 pass |
| vega-talk.sh --self-test | 15 | 15 pass |
| bootstrap.sh --self-test | 34 | 34 pass |
| vega-restore.sh --self-test | 13 | 13 pass |
| vega-ask.py --self-test | 11 | 11 pass |
| **total** | **164** | **164 pass, 0 fail** |

## What's next (not in this batch)

- Curated extraction from Elliot's bigger systems (his call which pieces).
- Replit demo path for live buyer walkthroughs (needs his sign-in).
