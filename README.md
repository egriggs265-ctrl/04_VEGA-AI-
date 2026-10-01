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
- `--self-test` on everything. 211 fixture assertions, all passing (see below).

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

### vega-cron.py — portable job scheduler
cron(8) doesn't exist everywhere this toolkit runs (Termux, macOS,
minimal containers). vega-cron.py is the portable replacement: it reads
a schedule list and drops command files into `inbox/` when they're due,
for vega-inbox.py to dispatch. The wiring below says "hourly, via cron" —
this is what makes that true on machines without cron.

    python3 vega-cron.py --schedules schedules.json --root ./vega-root --loop 30
    python3 vega-cron.py --schedules schedules.json --root ./vega-root --once

Schedule file: `{"schedules": [{"name": "hourly-health", "every_s": 3600,
"cmd": {"cmd": "note", "args": {"text": "run the health sweep"}},
"catchup": true, "enabled": true}]}`. A schedule with no history fires on
the first cycle, then every `every_s` seconds. After an outage longer than
1.5x the interval, `catchup: true` queues exactly one command (never a
flood); `catchup: false` skips the missed window. At most one command per
schedule per cycle. State in `state/cron.json` (atomic), heartbeat in
`state/cron.heartbeat` — point a watchdog job at it to supervise the
scheduler itself. NB: the dispatcher's 10-minute dedup window applies, so
a schedule firing identical cmd+args more often than that should carry
something unique in args. `bootstrap.sh --install` writes a starter
`schedules.json` (hourly health note, daily rollup note) alongside the
starter `jobs.json`; edit freely.

### vega-status.py — one-glance status for the whole tree
Read-only aggregator: reads the JSON the other tools already write
(watchdog jobs, inbox heartbeat/counters, sensor scores/alerts, cron
schedules, snapshots) and answers "is everything alive?" Verdict `ok` /
`degraded` (reasons listed) / `idle` (nothing detected). Never writes,
never locks.

    python3 vega-status.py --root ./vega-root
    python3 vega-status.py --root ./vega-root --json   # machine-readable

It expects the daemon shape from "Wiring them together" (inbox as
`--loop`): a dispatcher that ran one-shot and exited shows as not-running
— vega-talk.sh would refuse to send through it too.

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
vega-health.py (hourly, via cron or vega-cron.py) # one snapshot for the autonomy sweep
vega-rollup.py (daily, via cron or vega-cron.py)  # digest appended to WORKLOG
vega-cron.py --loop 30              # portable scheduler: recurring commands -> inbox/
vega-status.py                      # one glance: jobs, dispatcher, sensor, cron, snapshots
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
| bootstrap.sh --self-test | 38 | 38 pass |
| vega-restore.sh --self-test | 13 | 13 pass |
| vega-ask.py --self-test | 11 | 11 pass |
| vega-cron.py --self-test | 21 | 21 pass |
| vega-status.py --self-test | 22 | 22 pass |
| **total** | **211** | **211 pass, 0 fail** |

## What's next (not in this batch)

- Curated extraction from Elliot's bigger systems (his call which pieces).
- Replit demo path for live buyer walkthroughs (needs his sign-in).
