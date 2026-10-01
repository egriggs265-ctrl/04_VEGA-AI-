#!/usr/bin/env python3
"""vega-status.py — one-glance status for the whole vega tree.

The toolkit writes its state as JSON all over the working root;
vega-status.py reads it back and answers the only question that matters
at 3 AM: is everything alive? Read-only — it never writes, never locks,
never disturbs a running daemon.

Components checked (each optional; missing ones are reported as
"not configured", never as failures):
  watchdog   logs/watchdog-jobs.json (or health/jobs.json): per-job ok/stale/dead
             logs/watchdog.heartbeat: freshness
  inbox      state/heartbeat.json: ts + pid liveness (kill -0)
             state/state.json: processed/errors/deduped counters
             inbox/: pending command files
  sensor     state/scores.json: cumulative rule scores
             alerts/: alert files; findings.jsonl: line count
  cron       state/cron.json: schedules, last runs (vega-cron.py)
  snapshots  snapshots/: snapshot count

Verdict: "ok" (everything found is healthy), "degraded" (something is
stale/dead/failing — reasons listed), "idle" (no components detected).

Usage:
    vega-status.py --root ./vega-root
    vega-status.py --root ./vega-root --json
    vega-status.py --self-test

Exit codes: 0 ok or idle, 2 degraded.
Stdlib only. No network.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

STALE_HEARTBEAT_S = 300  # heartbeat older than this counts as stale


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, OSError, TypeError):
        return False


def hb_age(hb) -> float | None:
    """Seconds since a heartbeat dict's ts, or None if unusable."""
    try:
        ts = float(hb["ts"])
    except (TypeError, ValueError, KeyError):
        return None
    return time.time() - ts


def gather(root: Path) -> dict:
    components = {}
    problems = []

    # -- watchdog ------------------------------------------------------
    wd_status = None
    for cand in (root / "logs" / "watchdog-jobs.json",
                 root / "health" / "jobs.json"):
        data = read_json(cand)
        if isinstance(data, dict):
            wd_status = data
            break
    wd_hb = read_json(root / "logs" / "watchdog.heartbeat")
    if wd_status is not None or wd_hb is not None:
        jobs = wd_status.get("jobs", []) if isinstance(wd_status, dict) else []
        job_map = {}
        for j in jobs:
            if isinstance(j, dict) and j.get("name"):
                st = j.get("status", "?")
                job_map[j["name"]] = st
                if st in ("stale", "dead"):
                    problems.append(f"watchdog job '{j['name']}' is {st}")
        age = hb_age(wd_hb) if isinstance(wd_hb, dict) else None
        if age is None:
            problems.append("watchdog heartbeat missing/unreadable")
        elif age > STALE_HEARTBEAT_S:
            problems.append(f"watchdog heartbeat stale ({age:.0f}s old)")
        components["watchdog"] = {"jobs": job_map,
                                  "heartbeat_age_s": round(age, 1) if age is not None else None}

    # -- inbox dispatcher ----------------------------------------------
    ib_hb = read_json(root / "state" / "heartbeat.json")
    ib_state = read_json(root / "state" / "state.json")
    if ib_hb is not None or ib_state is not None:
        age = hb_age(ib_hb) if isinstance(ib_hb, dict) else None
        pid = ib_hb.get("pid") if isinstance(ib_hb, dict) else None
        alive = pid_alive(pid) if pid is not None else None
        if age is None:
            problems.append("inbox heartbeat missing/unreadable")
        elif age > STALE_HEARTBEAT_S:
            problems.append(f"inbox heartbeat stale ({age:.0f}s old)")
        elif alive is False:
            problems.append(f"inbox dispatcher pid {pid} is dead")
        counters = {}
        if isinstance(ib_state, dict):
            counters = {k: ib_state.get(k) for k in
                        ("processed", "errors", "deduped") if k in ib_state}
        pending = len(list((root / "inbox").glob("*.json"))) \
            if (root / "inbox").is_dir() else 0
        components["inbox"] = {"heartbeat_age_s": round(age, 1) if age is not None else None,
                               "pid": pid, "pid_alive": alive,
                               "counters": counters, "pending": pending}

    # -- sensor ----------------------------------------------------------
    scores = read_json(root / "state" / "scores.json")
    alerts_dir = root / "alerts"
    findings = root / "findings.jsonl"
    n_alerts = len(list(alerts_dir.glob("*.json"))) if alerts_dir.is_dir() else 0
    n_findings = None
    if findings.is_file():
        try:
            with open(findings, encoding="utf-8") as f:
                n_findings = sum(1 for _ in f)
        except OSError:
            n_findings = None
    if scores is not None or n_alerts or (root / "events").is_dir():
        components["sensor"] = {"scores": scores or {},
                                "alerts": n_alerts,
                                "findings_lines": n_findings}

    # -- scheduler -------------------------------------------------------
    cron_state = read_json(root / "state" / "cron.json")
    cron_hb = read_json(root / "state" / "cron.heartbeat")
    if isinstance(cron_state, dict) and cron_state.get("schedules"):
        scheds = {}
        for name, s in cron_state["schedules"].items():
            scheds[name] = {"last_run": s.get("last_run"),
                            "runs": s.get("runs", 0)}
        age = hb_age(cron_hb) if isinstance(cron_hb, dict) else None
        if age is not None and age > STALE_HEARTBEAT_S:
            problems.append(f"scheduler heartbeat stale ({age:.0f}s old)")
        components["cron"] = {"schedules": scheds,
                              "heartbeat_age_s": round(age, 1) if age is not None else None}

    # -- snapshots -------------------------------------------------------
    snap_dir = root / "snapshots"
    if snap_dir.is_dir():
        snaps = sorted(p.name for p in snap_dir.glob("*.tar.gz"))
        if snaps:
            components["snapshots"] = {"count": len(snaps), "names": snaps}

    verdict = "idle" if not components else ("degraded" if problems else "ok")
    return {"root": str(root), "ts": time.time(), "verdict": verdict,
            "problems": problems, "components": components}


def render_human(rep: dict) -> str:
    lines = [f"[vega-status] {rep['root']} — verdict: {rep['verdict'].upper()}"]
    for p in rep["problems"]:
        lines.append(f"  PROBLEM: {p}")
    c = rep["components"]
    if "watchdog" in c:
        w = c["watchdog"]
        jobs = ", ".join(f"{n}={s}" for n, s in w["jobs"].items()) or "no jobs"
        lines.append(f"  watchdog: {jobs} (heartbeat "
                     + (f"{w['heartbeat_age_s']:.0f}s ago)"
                        if w["heartbeat_age_s"] is not None else "missing)"))
    if "inbox" in c:
        i = c["inbox"]
        ctr = i["counters"]
        ctr_s = (f"processed={ctr.get('processed', '?')} "
                 f"errors={ctr.get('errors', '?')} deduped={ctr.get('deduped', '?')}")
        live = ("alive" if i["pid_alive"] else
                "dead" if i["pid_alive"] is False else "pid unknown")
        lines.append(f"  inbox: dispatcher {live} "
                     f"(hb {i['heartbeat_age_s']:.0f}s ago, pending={i['pending']}) "
                     f"{ctr_s}")
    if "sensor" in c:
        s = c["sensor"]
        lines.append(f"  sensor: alerts={s['alerts']} "
                     f"findings_lines={s['findings_lines']} scores={s['scores']}")
    if "cron" in c:
        cr = c["cron"]
        scheds = ", ".join(f"{n} (runs={v['runs']})"
                           for n, v in cr["schedules"].items())
        lines.append(f"  cron: {scheds}")
    if "snapshots" in c:
        lines.append(f"  snapshots: {c['snapshots']['count']}")
    if rep["verdict"] == "idle":
        lines.append("  (no vega components detected under this root)")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable JSON instead of the human summary")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    rep = gather(Path(args.root))
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(render_human(rep), end="")
    return 2 if rep["verdict"] == "degraded" else 0


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

    print("[vega-status self-test]")
    now = time.time()

    def wjson(path, obj):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj))

    # 1. idle: empty root
    with tempfile.TemporaryDirectory() as td:
        rep = gather(Path(td))
        check("empty root -> verdict idle", rep["verdict"] == "idle")
        check("empty root -> no components, no problems",
              rep["components"] == {} and rep["problems"] == [])
        check("idle exits 0", main_for_test(Path(td), False) == 0)

    # 2. ok: everything fresh and healthy
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wjson(root / "logs" / "watchdog-jobs.json",
              {"ts": now, "jobs": [
                  {"name": "a", "status": "ok", "age_s": 10},
                  {"name": "b", "status": "ok", "age_s": 20}]})
        wjson(root / "logs" / "watchdog.heartbeat",
              {"ts": now - 5, "pid": os.getpid(), "ok": True})
        wjson(root / "state" / "heartbeat.json",
              {"ts": now - 3, "pid": os.getpid(), "ok": True})
        wjson(root / "state" / "state.json",
              {"ts": now, "pid": os.getpid(), "processed": 42,
               "errors": 0, "deduped": 1})
        (root / "inbox").mkdir(parents=True, exist_ok=True)
        (root / "inbox" / "pending1.json").write_text("{}")
        wjson(root / "state" / "scores.json", {"ssh_auth_fail": 9})
        (root / "alerts").mkdir(parents=True, exist_ok=True)
        (root / "alerts" / "1_ssh.json").write_text("{}")
        (root / "findings.jsonl").write_text('{"a":1}\n{"a":2}\n')
        wjson(root / "state" / "cron.json",
              {"schedules": {"hourly": {"last_run": now - 60, "runs": 5,
                                        "last_file": "f.json"}}})
        wjson(root / "state" / "cron.heartbeat",
              {"ts": now - 10, "pid": os.getpid(), "ok": True,
               "fired_total": 5})
        (root / "snapshots").mkdir(parents=True, exist_ok=True)
        (root / "snapshots" / "gold.tar.gz").write_text("x")

        rep = gather(root)
        check("healthy tree -> verdict ok", rep["verdict"] == "ok")
        check("no problems when healthy", rep["problems"] == [])
        check("watchdog jobs parsed",
              rep["components"]["watchdog"]["jobs"] == {"a": "ok", "b": "ok"})
        check("inbox pending counted",
              rep["components"]["inbox"]["pending"] == 1)
        check("inbox counters parsed",
              rep["components"]["inbox"]["counters"]["processed"] == 42)
        check("inbox pid reported alive",
              rep["components"]["inbox"]["pid_alive"] is True)
        check("sensor scores/alerts/findings parsed",
              rep["components"]["sensor"]["scores"] == {"ssh_auth_fail": 9}
              and rep["components"]["sensor"]["alerts"] == 1
              and rep["components"]["sensor"]["findings_lines"] == 2)
        check("cron schedules parsed",
              rep["components"]["cron"]["schedules"]["hourly"]["runs"] == 5)
        check("snapshots counted",
              rep["components"]["snapshots"]["count"] == 1)
        check("ok exits 0", main_for_test(root, False) == 0)
        human = render_human(rep)
        check("human output names the verdict", "verdict: OK" in human)
        check("human output has all components",
              all(k in human for k in
                  ("watchdog:", "inbox:", "sensor:", "cron:", "snapshots:")))

    # 3. degraded: dead job + stale inbox heartbeat + dead pid
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wjson(root / "logs" / "watchdog-jobs.json",
              {"ts": now, "jobs": [{"name": "a", "status": "dead"}]})
        wjson(root / "logs" / "watchdog.heartbeat",
              {"ts": now - 5, "pid": os.getpid(), "ok": True})
        wjson(root / "state" / "heartbeat.json",
              {"ts": now - 9999, "pid": 99999999, "ok": True})
        rep = gather(root)
        check("dead job + stale hb -> verdict degraded",
              rep["verdict"] == "degraded")
        check("dead job named in problems",
              any("job 'a' is dead" in p for p in rep["problems"]))
        check("stale inbox heartbeat named in problems",
              any("inbox heartbeat stale" in p for p in rep["problems"]))
        check("degraded exits 2", main_for_test(root, False) == 2)
        human = render_human(rep)
        check("human output lists PROBLEM lines", "PROBLEM:" in human)

    # 4. corrupt JSON tolerated, not fatal
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "logs").mkdir(parents=True, exist_ok=True)
        (root / "logs" / "watchdog-jobs.json").write_text("{broken")
        rep = gather(root)
        check("corrupt jobs json -> treated as missing, not a crash",
              rep["verdict"] in ("idle", "degraded", "ok"))

    # 5. --json output parses and carries the verdict
    with tempfile.TemporaryDirectory() as td:
        rep = gather(Path(td))
        check("json mode round-trips",
              json.loads(json.dumps(rep))["verdict"] == "idle")

    print(f"[vega-status self-test] {passed} passed, {failed} failed")
    return 1 if failed else 0


def main_for_test(root: Path, as_json: bool) -> int:
    """Exercise main()'s exit-code logic without argparse."""
    rep = gather(root)
    if as_json:
        json.dumps(rep)
    else:
        render_human(rep)
    return 2 if rep["verdict"] == "degraded" else 0


if __name__ == "__main__":
    sys.exit(main())
