#!/usr/bin/env python3
"""vega-cron.py — portable job scheduler for the vega toolkit.

cron(8) doesn't exist everywhere this toolkit runs — Termux, macOS,
minimal containers (bootstrap.sh even refuses persistence where neither
systemd nor crontab exists). vega-cron.py is the portable replacement:
it reads a schedule list and drops command files into the inbox dir when
they're due, for vega-inbox.py to dispatch. Hourly health sweeps, daily
rollups, weekly reports — all as inbox commands, all supervisable by the
watchdog through the scheduler's own heartbeat.

Schedule file (schedules.json):
    {"schedules": [
       {"name": "hourly-health",
        "every_s": 3600,
        "cmd": {"cmd": "note", "args": {"text": "run the hourly health sweep"}},
        "catchup": true,
        "enabled": true}],
     "inbox_dir": "/path/to/inbox",
     "state_file": "/path/to/state/cron.json",
     "heartbeat_file": "/path/to/state/cron.heartbeat"}

Rules:
  - a schedule with no history fires on the first cycle (so you see it
    work), then every every_s seconds after its last run.
  - if the scheduler was down longer than 1.5x the interval: catchup=true
    queues exactly ONE command for the missed window (never a flood);
    catchup=false skips the missed runs silently. Either way last_run
    advances to now.
  - at most one queued command per schedule per cycle.
  - NB: the dispatcher's dedup window suppresses identical commands
    within 10 minutes — a schedule firing the same cmd+args more often
    than that should carry something unique (e.g. a timestamp) in args.

Usage:
    vega-cron.py --schedules schedules.json --root ./vega-root [--once | --loop 30]
    vega-cron.py --self-test

State (atomic): state/cron.json
    {"schedules": {"hourly-health": {"last_run": <epoch|null>,
                                     "runs": 7, "last_file": "..."}}}
Heartbeat (for the watchdog to supervise this scheduler):
    state/cron.heartbeat {"ts", "pid", "ok": true, "fired_total": n}

Exit codes: 0 ok, 2 bad schedule config.
Stdlib only. No network.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

MISSED_FACTOR = 1.5  # downtime beyond every_s * this counts as "missed"


def atomic_write(path: Path, text: str) -> None:
    """Write file atomically: temp + os.replace. Never a torn state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def safe_name(name: str) -> str:
    """Make a schedule name safe for use in a filename."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


class ConfigError(ValueError):
    pass


def validate_schedule(raw, idx: int, seen: set) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError(f"schedule #{idx}: must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"schedule #{idx}: 'name' must be a non-empty string")
    if name in seen:
        raise ConfigError(f"schedule #{idx}: duplicate name '{name}'")
    seen.add(name)
    every = raw.get("every_s")
    if isinstance(every, bool) or not isinstance(every, (int, float)) or every <= 0:
        raise ConfigError(f"schedule '{name}': 'every_s' must be a positive number")
    cmd = raw.get("cmd")
    if not isinstance(cmd, dict) or not isinstance(cmd.get("cmd"), str):
        raise ConfigError(f"schedule '{name}': 'cmd' must be an object with a 'cmd' key")
    return {
        "name": name,
        "every_s": float(every),
        "cmd": {"cmd": cmd["cmd"], "args": cmd.get("args", {}) or {}},
        "catchup": bool(raw.get("catchup", True)),
        "enabled": bool(raw.get("enabled", True)),
    }


def load_config(path: Path):
    """Return (schedules, cfg). Raises ConfigError on anything malformed."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}")
    except ValueError as e:
        raise ConfigError(f"bad JSON in {path}: {e}")
    if not isinstance(raw, dict) or not isinstance(raw.get("schedules"), list):
        raise ConfigError(f"{path}: top level must be an object with a 'schedules' list")
    seen = set()
    schedules = [validate_schedule(s, i, seen)
                 for i, s in enumerate(raw["schedules"])]
    return schedules, raw


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def fire_command(sched: dict, inbox_dir: Path, now: float) -> str:
    """Queue one command file for a due schedule. Returns the filename."""
    fname = f"cron-{safe_name(sched['name'])}-{int(now)}.json"
    obj = {"id": f"cron-{safe_name(sched['name'])}-{int(now)}",
           "cmd": sched["cmd"]["cmd"],
           "args": sched["cmd"]["args"]}
    atomic_write(inbox_dir / fname, json.dumps(obj, indent=2) + "\n")
    return fname


def run_once(schedules: list, inbox_dir: Path, state_path: Path,
             now: float = None):
    """Fire every due schedule. Returns (fired, skipped)."""
    now = time.time() if now is None else now
    inbox_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path)
    sched_state = state.get("schedules", {})
    if not isinstance(sched_state, dict):
        sched_state = {}
    fired, skipped = [], []
    for sched in schedules:
        name = sched["name"]
        if not sched["enabled"]:
            skipped.append((name, "disabled"))
            continue
        prev = sched_state.get(name, {})
        last_run = prev.get("last_run")
        if last_run is not None and (now - last_run) < sched["every_s"]:
            continue  # not due yet
        missed = (last_run is not None
                  and (now - last_run) > sched["every_s"] * MISSED_FACTOR)
        if missed and not sched["catchup"]:
            skipped.append((name, "missed window, catchup=false"))
            fname = None
        else:
            fname = fire_command(sched, inbox_dir, now)
            fired.append(name)
        sched_state[name] = {"last_run": now,
                             "runs": int(prev.get("runs", 0)) + 1,
                             "last_file": fname}
    state["schedules"] = sched_state
    state["ts"] = now
    atomic_write(state_path, json.dumps(state, indent=2) + "\n")
    return fired, skipped


def acquire_lock(lock_path: Path) -> bool:
    """Pidfile lock with kill -0 stale detection (watchdog/inbox pattern)."""
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        pass
    try:
        old_pid = int(lock_path.read_text(encoding="utf-8").strip().split()[0])
        os.kill(old_pid, 0)
        return False  # live owner
    except (ValueError, OSError):
        pass  # stale or unreadable -> take over
    try:
        lock_path.unlink()
    except OSError:
        pass
    return acquire_lock(lock_path)


def release_lock(lock_path: Path) -> None:
    try:
        if lock_path.read_text(encoding="utf-8").strip().split()[0] == str(os.getpid()):
            lock_path.unlink()
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedules", default="schedules.json")
    ap.add_argument("--root", default=".")
    ap.add_argument("--once", action="store_true",
                    help="fire due schedules once and exit")
    ap.add_argument("--loop", type=int, default=0,
                    help="daemon: check every N seconds")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    root = Path(args.root)
    try:
        schedules, cfg = load_config(Path(args.schedules))
    except ConfigError as e:
        print(f"[vega-cron] bad schedule config: {e}", file=sys.stderr)
        return 2

    inbox_dir = Path(cfg.get("inbox_dir", str(root / "inbox")))
    state_path = Path(cfg.get("state_file", str(root / "state" / "cron.json")))
    hb_path = Path(cfg.get("heartbeat_file",
                           str(root / "state" / "cron.heartbeat")))
    lock_path = root / "run" / "cron.lock"
    fired_total = 0

    def heartbeat():
        atomic_write(hb_path, json.dumps(
            {"ts": time.time(), "pid": os.getpid(), "ok": True,
             "fired_total": fired_total}) + "\n")

    def cycle():
        nonlocal fired_total
        fired, skipped = run_once(schedules, inbox_dir, state_path)
        fired_total += len(fired)
        heartbeat()
        if fired or skipped:
            print(f"[vega-cron] fired={fired} skipped={[s[0] for s in skipped]}",
                  flush=True)
        return fired, skipped

    if args.loop > 0:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if not acquire_lock(lock_path):
            print("[vega-cron] another instance holds the lock; exiting",
                  file=sys.stderr)
            return 1
        try:
            while True:
                try:
                    cycle()
                except Exception as e:  # noqa: BLE001 - scheduler must not die
                    print(f"[vega-cron] cycle failed: {e}", file=sys.stderr)
                    time.sleep(5)
                time.sleep(args.loop)
        except KeyboardInterrupt:
            pass
        finally:
            release_lock(lock_path)
        return 0

    cycle()
    return 0


def self_test() -> int:
    import tempfile
    passed = failed = 0

    def check(label, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS {label}")
        else:
            failed += 1
            print(f"  FAIL {label}")

    print("[vega-cron self-test]")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        inbox = root / "inbox"
        state = root / "state" / "cron.json"
        cfg_path = root / "schedules.json"
        cfg_path.write_text(json.dumps({"schedules": [
            {"name": "hourly", "every_s": 3600,
             "cmd": {"cmd": "note", "args": {"text": "hourly sweep"}}},
            {"name": "disabled-one", "every_s": 60, "enabled": False,
             "cmd": {"cmd": "note", "args": {"text": "never"}}},
        ]}))
        schedules, cfg = load_config(cfg_path)
        t0 = 1_700_000_000.0

        # 1. first run: no history -> fires immediately
        fired, skipped = run_once(schedules, inbox, state, now=t0)
        check("first run fires schedule with no history", fired == ["hourly"])
        check("disabled schedule skipped, not fired",
              skipped == [("disabled-one", "disabled")]
              and not list(inbox.glob("*disabled*")))
        files = sorted(inbox.glob("*.json"))
        check("exactly one command file queued", len(files) == 1)
        cmd = json.loads(files[0].read_text())
        check("queued file carries the schedule's cmd/args",
              cmd["cmd"] == "note" and cmd["args"] == {"text": "hourly sweep"}
              and cmd["id"].startswith("cron-hourly-"))
        st = json.loads(state.read_text())
        check("state records last_run + run count",
              st["schedules"]["hourly"]["last_run"] == t0
              and st["schedules"]["hourly"]["runs"] == 1)

        # 2. immediate rerun: not due -> nothing fires
        fired2, _ = run_once(schedules, inbox, state, now=t0 + 10)
        check("rerun before interval: nothing fires",
              fired2 == [] and len(list(inbox.glob("*.json"))) == 1)

        # 3. after the interval: fires again
        fired3, _ = run_once(schedules, inbox, state, now=t0 + 3600)
        check("run after every_s fires again", fired3 == ["hourly"])
        check("run counter increments",
              json.loads(state.read_text())["schedules"]["hourly"]["runs"] == 2)

        # 4. catchup=true after a long outage: exactly ONE catch-up command
        before = len(list(inbox.glob("*.json")))
        fired4, _ = run_once(schedules, inbox, state,
                             now=t0 + 3600 + 3 * 3600 + 1)
        after = len(list(inbox.glob("*.json")))
        check("3 missed intervals -> exactly one catch-up command",
              fired4 == ["hourly"] and after - before == 1)

        # 5. catchup=false: missed window skipped, last_run still advances
        cfg2_path = root / "schedules2.json"
        cfg2_path.write_text(json.dumps({"schedules": [
            {"name": "daily", "every_s": 86400, "catchup": False,
             "cmd": {"cmd": "note", "args": {"text": "daily"}}},
        ]}))
        schedules2, _ = load_config(cfg2_path)
        state2 = root / "state" / "cron2.json"
        run_once(schedules2, inbox, state2, now=t0)
        n_before = len(list(inbox.glob("*.json")))
        fired5, skipped5 = run_once(schedules2, inbox, state2,
                                    now=t0 + 3 * 86400 + 1)
        n_after = len(list(inbox.glob("*.json")))
        check("catchup=false: missed window queues nothing",
              fired5 == [] and n_after == n_before)
        check("catchup=false: skip reason recorded",
              skipped5 == [("daily", "missed window, catchup=false")])
        st2 = json.loads(state2.read_text())
        check("catchup=false: last_run still advances",
              st2["schedules"]["daily"]["last_run"] == t0 + 3 * 86400 + 1)

        # 6. normal due fire with catchup=false still works (not a missed window)
        fired6, _ = run_once(schedules2, inbox, state2,
                             now=t0 + 4 * 86400 + 2)
        check("catchup=false: ordinary due run still fires", fired6 == ["daily"])

        # 7. odd names are filename-safe
        cfg3_path = root / "schedules3.json"
        cfg3_path.write_text(json.dumps({"schedules": [
            {"name": "weird name/100%", "every_s": 60,
             "cmd": {"cmd": "ping", "args": {}}},
        ]}))
        schedules3, _ = load_config(cfg3_path)
        fired7, _ = run_once(schedules3, inbox, root / "state" / "cron3.json",
                             now=t0)
        check("odd schedule name fires", fired7 == ["weird name/100%"])
        check("odd name sanitized in filename",
              (inbox / f"cron-weird_name_100_-{int(t0)}.json").exists())

        # 8. no .tmp files survive a run
        check("no .tmp files left", list(root.rglob("*.tmp")) == [])

    # 9. config validation fails cleanly (exit 2, no traceback)
    import subprocess
    with tempfile.TemporaryDirectory() as td:
        cases = {
            "missing-name": {"schedules": [{"every_s": 60,
                                            "cmd": {"cmd": "ping"}}]},
            "dup-name": {"schedules": [
                {"name": "a", "every_s": 60, "cmd": {"cmd": "ping"}},
                {"name": "a", "every_s": 60, "cmd": {"cmd": "ping"}}]},
            "zero-interval": {"schedules": [{"name": "a", "every_s": 0,
                                             "cmd": {"cmd": "ping"}}]},
            "cmd-without-cmd": {"schedules": [{"name": "a", "every_s": 60,
                                               "cmd": {"args": {}}}]},
            "not-a-list": {"schedules": {}},
        }
        for label, bad in cases.items():
            p = Path(td) / f"{label}.json"
            p.write_text(json.dumps(bad))
            r = subprocess.run(
                [sys.executable, __file__, "--schedules", str(p),
                 "--root", td, "--once"],
                capture_output=True, text=True, timeout=30)
            ok = (r.returncode == 2 and "bad schedule config" in r.stderr
                  and "Traceback" not in r.stderr)
            check(f"bad config '{label}' -> exit 2, clean message", ok)

    print(f"[vega-cron self-test] {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
