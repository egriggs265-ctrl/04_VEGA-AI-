#!/usr/bin/env python3
"""vega-watchdog.py — heartbeat supervisor for Sable's recurring jobs.

Watches every job's heartbeat file, classifies each job as ok / stale / dead,
writes a status JSON, and drops exactly one re-arm command per stale/dead job
into the inbox dir for vega-inbox.py to dispatch.

Lineage: sentinel-watchdog (supervisor loop, pidfile + kill -0 stale-lock,
backoff after repeated failures) + sentinel-daemon (epoch heartbeat file).
Improvements over the originals: manifest-driven (no hardcoded job list),
re-arm dedup (one pending re-arm per job max), atomic status writes,
--self-test with fixture assertions.

Usage:
    vega-watchdog.py --manifest jobs.json [--once | --loop 60]
    vega-watchdog.py --self-test

Manifest format:
    {
      "jobs": [
        {"name": "income-sweep",
         "heartbeat_file": "/path/to/heartbeat.json",
         "expected_interval_s": 14400,
         "max_missed": 2,
         "rearm": {"cmd": "note", "args": {"text": "income-sweep missed heartbeat"}}}
      ],
      "inbox_dir": "/path/to/inbox",
      "status_file": "/path/to/health/jobs.json",
      "heartbeat_file": "/path/to/health/watchdog.heartbeat"
    }

Heartbeat files may be JSON {"ts": <epoch>} or a plain epoch number;
a missing/unparseable heartbeat, or falling back to file mtime, is
reported in the job's detail field.

Stdlib only. No network.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

STALE_FACTOR = 3  # stale window = interval * max_missed * STALE_FACTOR


def atomic_write(path: Path, text: str) -> None:
    """Write file atomically: temp + os.replace. Never a torn state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_heartbeat(hb_path: Path):
    """Return (epoch_ts or None, detail str). Accepts JSON {"ts"} or bare epoch."""
    try:
        raw = hb_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, "heartbeat file missing"
    except OSError as e:
        return None, f"heartbeat unreadable: {e}"
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "ts" in data:
            return float(data["ts"]), "json ts"
    except (ValueError, TypeError):
        pass
    try:
        return float(raw.split()[0]), "bare epoch"
    except (ValueError, IndexError):
        pass
    try:
        return hb_path.stat().st_mtime, "fallback: file mtime"
    except OSError as e:
        return None, f"heartbeat unparseable: {e}"


def classify(age_s, interval_s, max_missed):
    """ok / stale / dead from heartbeat age."""
    if age_s <= interval_s * max_missed:
        return "ok"
    if age_s <= interval_s * max_missed * STALE_FACTOR:
        return "stale"
    return "dead"


def acquire_lock(lock_path: Path) -> bool:
    """Pidfile lock with kill -0 stale detection (sentinel-lock pattern)."""
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


def run_once(manifest: dict, root: Path):
    jobs = manifest.get("jobs", [])
    inbox_dir = Path(manifest.get("inbox_dir", str(root / "inbox")))
    inbox_dir.mkdir(parents=True, exist_ok=True)
    results = []
    rearmed = []
    for job in jobs:
        name = job.get("name", "unnamed")
        hb = Path(job["heartbeat_file"])
        interval = int(job.get("expected_interval_s", 3600))
        max_missed = int(job.get("max_missed", 2))
        ts, detail = read_heartbeat(hb)
        if ts is None:
            status, age = "dead", None
        else:
            age = time.time() - ts
            status = classify(age, interval, max_missed)
        entry = {"name": name, "status": status,
                 "age_s": round(age, 1) if age is not None else None,
                 "detail": detail}
        results.append(entry)
        if status in ("stale", "dead"):
            rearm_file = inbox_dir / f"rearm-{name}.json"
            if not rearm_file.exists():
                cmd = dict(job.get("rearm", {"cmd": "note",
                                             "args": {"text": f"{name} heartbeat {status}"}}))
                cmd.setdefault("id", f"rearm-{name}-{int(time.time())}")
                atomic_write(rearm_file, json.dumps(cmd, indent=2) + "\n")
                rearmed.append(name)
                entry["rearm"] = "queued"
            else:
                entry["rearm"] = "already pending"
    return results, rearmed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="jobs.json")
    ap.add_argument("--root", default=".")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="supervise every N seconds")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    root = Path(args.root)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    status_file = Path(manifest.get("status_file", str(root / "health" / "jobs.json")))
    hb_file = Path(manifest.get("heartbeat_file",
                                str(root / "health" / "watchdog.heartbeat")))
    lock_path = root / "run" / "watchdog.lock"

    def cycle():
        results, rearmed = run_once(manifest, root)
        atomic_write(status_file, json.dumps(
            {"ts": time.time(), "jobs": results}, indent=2) + "\n")
        atomic_write(hb_file, json.dumps(
            {"ts": time.time(), "pid": os.getpid(), "ok": True}) + "\n")
        line = (f"[watchdog] {len(results)} jobs: "
                + ", ".join(f"{r['name']}={r['status']}" for r in results))
        if rearmed:
            line += f" | re-armed: {', '.join(rearmed)}"
        print(line, flush=True)

    if args.loop > 0:
        if not acquire_lock(lock_path):
            print("[watchdog] another instance holds the lock; exiting", file=sys.stderr)
            return 1
        failures = 0
        try:
            while True:
                try:
                    cycle()
                    failures = 0
                except Exception as e:  # noqa: BLE001 - supervisor must not die
                    failures += 1
                    print(f"[watchdog] cycle failed ({failures}): {e}", file=sys.stderr)
                    if failures > 5:
                        time.sleep(120)  # backoff, sentinel-watchdog pattern
                        failures = 0
                time.sleep(args.loop)
        except KeyboardInterrupt:
            pass
        finally:
            release_lock(lock_path)
        return 0

    cycle()
    return 0


def self_test() -> int:
    """Fixture-based: fresh/stale/dead/missing heartbeats, re-arm dedup."""
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

    print("[vega-watchdog self-test]")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        now = time.time()
        (root / "hb_ok.json").write_text(json.dumps({"ts": now - 100}))
        (root / "hb_stale.json").write_text(json.dumps({"ts": now - 5000}))
        (root / "hb_dead.json").write_text(json.dumps({"ts": now - 100000}))
        manifest = {
            "jobs": [
                {"name": "fresh", "heartbeat_file": str(root / "hb_ok.json"),
                 "expected_interval_s": 3600, "max_missed": 2,
                 "rearm": {"cmd": "note", "args": {"text": "fresh missed"}}},
                {"name": "stale", "heartbeat_file": str(root / "hb_stale.json"),
                 "expected_interval_s": 3600, "max_missed": 1,
                 "rearm": {"cmd": "note", "args": {"text": "stale missed"}}},
                {"name": "dead", "heartbeat_file": str(root / "hb_dead.json"),
                 "expected_interval_s": 3600, "max_missed": 1,
                 "rearm": {"cmd": "note", "args": {"text": "dead missed"}}},
                {"name": "ghost", "heartbeat_file": str(root / "hb_missing.json"),
                 "expected_interval_s": 3600, "max_missed": 1,
                 "rearm": {"cmd": "note", "args": {"text": "ghost missed"}}},
            ],
            "inbox_dir": str(root / "inbox"),
            "status_file": str(root / "health" / "jobs.json"),
        }
        results, rearmed = run_once(manifest, root)
        atomic_write(root / "health" / "jobs.json",
                     json.dumps({"ts": time.time(), "jobs": results}, indent=2) + "\n")
        by_name = {r["name"]: r for r in results}
        check("fresh job classified ok", by_name["fresh"]["status"] == "ok")
        check("stale job classified stale", by_name["stale"]["status"] == "stale")
        check("dead job classified dead", by_name["dead"]["status"] == "dead")
        check("missing heartbeat classified dead", by_name["ghost"]["status"] == "dead")
        check("no re-arm for ok job", not (root / "inbox" / "rearm-fresh.json").exists())
        check("re-arm queued for stale", (root / "inbox" / "rearm-stale.json").exists())
        check("re-arm queued for dead", (root / "inbox" / "rearm-dead.json").exists())
        check("re-arm queued for ghost", (root / "inbox" / "rearm-ghost.json").exists())
        check("re-armed set exact", sorted(rearmed) == ["dead", "ghost", "stale"])
        rearm_cmd = json.loads((root / "inbox" / "rearm-stale.json").read_text())
        check("re-arm carries manifest cmd", rearm_cmd["cmd"] == "note"
              and rearm_cmd["args"]["text"] == "stale missed")
        # second run: dedup — no duplicate re-arm files
        before = sorted(p.name for p in (root / "inbox").iterdir())
        results2, rearmed2 = run_once(manifest, root)
        after = sorted(p.name for p in (root / "inbox").iterdir())
        check("no duplicate re-arms on rerun", before == after and rearmed2 == [])
        check("status file written atomically (no .tmp left)",
              (root / "health" / "jobs.json").exists()
              and not (root / "health" / "jobs.json.tmp").exists())
        status = json.loads((root / "health" / "jobs.json").read_text())
        check("status file has all jobs", len(status["jobs"]) == 4)
        # bare-epoch heartbeat accepted
        (root / "hb_bare.txt").write_text(str(int(now - 50)) + "\n")
        ts, detail = read_heartbeat(root / "hb_bare.txt")
        check("bare epoch heartbeat parsed", ts is not None and abs(ts - (now - 50)) < 2)

    print(f"[vega-watchdog self-test] {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
