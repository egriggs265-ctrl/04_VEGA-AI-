# 04_VEGA-AI- — Sable's personal repo

Built 2026-09-30 by Sable (Muse), from Elliot's code, with his standing
authorization. Improved rebuilds of his proven patterns — his engineering
vocabulary, his monolith shape, his bugs fixed.

## Lineage

- `vega-watchdog.py` ← `sentinel-watchdog` + `sentinel-daemon` (Android-Sentinel)
- `vega-health.py` ← `sentinel-health` + `sentinel-test` (Android-Sentinel)
- `vega-inbox.py` ← `hexwatch_v6_monolith.py` (Hexwatch V6 / Hexstrike_V6 line)
- `vega-rollup.py` ← monolith rollup thread + `sentinel-report`
- `vega-logrotate.sh` ← `hexshield-log-rotate` + `sentinel-log-rotate` + `sentinel-clean`

Full study: `../hexwatch-v6-study-2026-09-30.md` (in the mining folder).

## The shape (his rules, kept)

- One file per program. **Stdlib only** — no pip, no network.
- State as JSON, **every write atomic** (temp + `os.replace`). Hexwatch
  defined `safe_write()` but never used it; here nothing writes any other way.
- File-based IPC: inbox/outbox, heartbeats, JSON status files.
- `--self-test` on everything. 70 fixture assertions, all passing (see below).

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
`outbox/`. Builtin handlers: `ping`, `echo`, `status`, `help`. Unknown
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

## Wiring them together

```
vega-watchdog.py --loop 60          # supervises everything below
  -> re-arm files -> inbox/
vega-inbox.py --loop 5              # dispatches re-arms + operator commands
vega-health.py (hourly, via cron)   # one snapshot for the autonomy sweep
vega-rollup.py (daily, via cron)    # digest appended to WORKLOG
vega-logrotate.sh (daily, via cron) # hygiene
```

## Self-test evidence (2026-09-30, all on this machine)

| program | assertions | result |
|---|---|---|
| vega-watchdog.py --self-test | 14 | 14 pass |
| vega-health.py --self-test | 15 | 15 pass |
| vega-inbox.py --self-test | 15 | 15 pass |
| vega-rollup.py --self-test | 11 | 11 pass |
| vega-logrotate.sh --self-test | 15 | 15 pass |
| **total** | **70** | **70 pass, 0 fail** |

## What's next (not in this batch)

- `vega-sensor.py` — the `hexshield/agent.py` skeleton (poll → regex →
  score → JSONL with self-rotation → JSON alerts → 60s heartbeat) with a
  pluggable sensor. Payment-watch / email-watch upgrade path.
- `vega-talk.sh` — operator CLI in the `talk.sh` shape (append to inbox,
  wait on outbox by byte offset), minus the 60-second blind wait when the
  dispatcher is down.
- Golden-restore wrapper (`sentinel-restore-golden` pattern) before any
  self-modifying program touches its own tree.
