#!/usr/bin/env python3
"""vega-health.py — one JSON health snapshot for the hourly autonomy sweep.

Runs a list of numbered probes from a JSON config (file-age checks, queue
depths, error counts, arbitrary shell commands with timeouts) and emits a
single JSON snapshot with per-probe pass/fail plus a top-level verdict.
One file for the sweep to read instead of N ad-hoc checks.

Lineage: sentinel-health (snapshot versions/ages into state) + sentinel-test
(numbered smoke checks). Improvements: JSON config (no hardcoded probes),
per-probe timeouts (a hung probe fails instead of hanging the sweep),
elapsed_ms per probe, atomic snapshot writes, --self-test.

Usage:
    vega-health.py --probes probes.json [--out health/snapshot.json]
    vega-health.py --self-test

Probe config format:
    {"probes": [
       {"name": "watchdog heartbeat fresh", "type": "file_age",
        "path": "/path/to/heartbeat.json", "max_age_s": 3600},
       {"name": "inbox drains", "type": "cmd",
        "cmd": "test -z \"$(ls /path/to/inbox)\"", "timeout_s": 5},
       {"name": "jobs manifest valid", "type": "json_key",
        "path": "/path/to/jobs.json", "key": "jobs"},
       {"name": "state dir exists", "type": "file_exists",
        "path": "/path/to/state"}
    ]}

Probe types: file_age | file_exists | cmd | json_key
Verdict: "healthy" if every probe passes, else "degraded".

Stdlib only. No network.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def run_probe(probe: dict):
    """Return (ok: bool, detail: str). Never raises."""
    ptype = probe.get("type", "")
    started = time.monotonic()
    try:
        if ptype == "file_age":
            p = Path(probe["path"])
            max_age = float(probe.get("max_age_s", 3600))
            if not p.exists():
                return False, "missing"
            age = time.time() - p.stat().st_mtime
            if age <= max_age:
                return True, f"age {age:.0f}s <= {max_age:.0f}s"
            return False, f"age {age:.0f}s > {max_age:.0f}s (stale)"
        if ptype == "file_exists":
            p = Path(probe["path"])
            return (True, "present") if p.exists() else (False, "missing")
        if ptype == "cmd":
            timeout = float(probe.get("timeout_s", 10))
            try:
                r = subprocess.run(probe["cmd"], shell=True, capture_output=True,
                                   text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return False, f"timeout after {timeout:g}s"
            if r.returncode == 0:
                out = (r.stdout or "").strip().splitlines()
                tail = out[-1] if out else ""
                return True, f"exit 0{': ' + tail[:120] if tail else ''}"
            err = (r.stderr or r.stdout or "").strip().splitlines()
            tail = err[-1] if err else ""
            return False, f"exit {r.returncode}{': ' + tail[:120] if tail else ''}"
        if ptype == "json_key":
            p = Path(probe["path"])
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                return False, f"unreadable/invalid json: {e}"
            key = probe.get("key")
            if key is None:
                return True, "valid json"
            if isinstance(data, dict) and key in data:
                return True, f"key '{key}' present"
            return False, f"key '{key}' missing"
        return False, f"unknown probe type '{ptype}'"
    except KeyError as e:
        return False, f"probe misconfigured (missing {e})"
    except Exception as e:  # noqa: BLE001 - a probe must never kill the snapshot
        return False, f"probe error: {e}"
    finally:
        pass
    # elapsed measured by caller


def run_all(probes):
    results = []
    for i, probe in enumerate(probes, 1):
        started = time.monotonic()
        ok, detail = run_probe(probe)
        elapsed_ms = (time.monotonic() - started) * 1000
        results.append({
            "n": i,
            "name": probe.get("name", f"probe_{i}"),
            "type": probe.get("type", "?"),
            "ok": ok,
            "detail": detail,
            "elapsed_ms": round(elapsed_ms, 1),
        })
    verdict = "healthy" if all(r["ok"] for r in results) else "degraded"
    return {"ts": time.time(), "verdict": verdict,
            "passed": sum(1 for r in results if r["ok"]),
            "failed": sum(1 for r in results if not r["ok"]),
            "probes": results}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probes", default="probes.json")
    ap.add_argument("--out", default="")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    cfg = json.loads(Path(args.probes).read_text(encoding="utf-8"))
    snapshot = run_all(cfg.get("probes", []))
    text = json.dumps(snapshot, indent=2) + "\n"
    if args.out:
        atomic_write(Path(args.out), text)
    print(text, end="")
    return 0 if snapshot["verdict"] == "healthy" else 2


def self_test() -> int:
    """Fixture probes: one passing, one failing, one timeout. Assert verdict."""
    passed = failed = 0

    def check(label, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS {label}")
        else:
            failed += 1
            print(f"  FAIL {label}")

    print("[vega-health self-test]")
    probes = [
        {"name": "always true", "type": "cmd", "cmd": "true", "timeout_s": 5},
        {"name": "always false", "type": "cmd", "cmd": "false", "timeout_s": 5},
        {"name": "hangs", "type": "cmd", "cmd": "sleep 30", "timeout_s": 1},
        {"name": "bad type", "type": "frobnicate"},
    ]
    snap = run_all(probes)
    by_name = {p["name"]: p for p in snap["probes"]}
    check("probes numbered 1..4", [p["n"] for p in snap["probes"]] == [1, 2, 3, 4])
    check("passing probe ok", by_name["always true"]["ok"] is True)
    check("failing probe not ok", by_name["always false"]["ok"] is False)
    check("timeout probe not ok", by_name["hangs"]["ok"] is False)
    check("timeout detail mentions timeout",
          "timeout" in by_name["hangs"]["detail"])
    check("timeout probe did not hang the suite",
          by_name["hangs"]["elapsed_ms"] < 5000)
    check("unknown type is a failed probe not a crash",
          by_name["bad type"]["ok"] is False)
    check("verdict degraded", snap["verdict"] == "degraded")
    check("passed/failed counts exact", snap["passed"] == 1 and snap["failed"] == 3)
    check("elapsed_ms present on all", all("elapsed_ms" in p for p in snap["probes"]))

    # file_age + json_key + file_exists paths
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        fresh = root / "fresh.json"
        fresh.write_text(json.dumps({"jobs": []}))
        old = root / "old.txt"
        old.write_text("x")
        ancient = 100000
        os.utime(old, (time.time() - ancient, time.time() - ancient))
        probes2 = [
            {"name": "fresh file", "type": "file_age", "path": str(fresh),
             "max_age_s": 3600},
            {"name": "stale file", "type": "file_age", "path": str(old),
             "max_age_s": 3600},
            {"name": "missing file", "type": "file_exists",
             "path": str(root / "nope")},
            {"name": "json key present", "type": "json_key",
             "path": str(fresh), "key": "jobs"},
            {"name": "json key absent", "type": "json_key",
             "path": str(fresh), "key": "nope"},
        ]
        snap2 = run_all(probes2)
        b2 = {p["name"]: p for p in snap2["probes"]}
        check("fresh file_age passes", b2["fresh file"]["ok"] is True)
        check("stale file_age fails", b2["stale file"]["ok"] is False)
        check("missing file_exists fails", b2["missing file"]["ok"] is False)
        check("json_key present passes", b2["json key present"]["ok"] is True)
        check("json_key absent fails", b2["json key absent"]["ok"] is False)

    print(f"[vega-health self-test] {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
