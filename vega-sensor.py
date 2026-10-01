#!/usr/bin/env python3
"""vega-sensor.py — hexshield-style event sensor.

Polls a directory of event files, regex-scores each new line against a rule
set, appends JSONL findings, keeps cumulative per-rule scores, emits a JSON
alert file per high-scoring event, and heartbeats. Portable hexshield: no
logcat/dmesg, no Termux — the input is any directory of text event files,
so it works as a generic log monitor, a payment-watch sensor, or an
email-watch sensor (drop the watcher's output files in events/).

Lineage: hexshield/agent.py (Android-Sentinel) — poll loop, regex pattern
match with ignore-list filtering, cumulative threat scores in JSON state,
JSONL findings with self-rotation (128KB / 400 lines), per-event JSON
alerts, 60s heartbeat, exception-safe loop, KeyboardInterrupt handling.
Fixes vs the original:
  - hardcoded ~/hexshield paths -> --root / VEGA_ROOT, defaulting to cwd.
  - patterns hardcoded in source -> --sensors JSON config (or builtins).
  - agent.py's process-tracking (ps -A) was Android-specific; dropped here —
    sensors are line rules only, which is the portable core.

Usage:
    vega-sensor.py --root ./vega-root              # one pass, exit
    vega-sensor.py --root ./vega-root --loop 8     # poll every 8s
    vega-sensor.py --root ./vega-root --sensors sensors.json
    vega-sensor.py --self-test

Sensor rule (sensors.json, a JSON list):
    {"name": "ssh_auth_fail",
     "pattern": "Failed password|authentication failure",
     "score": 3,
     "ignore": ["from 10\\.0\\.0\\."],
     "alert_at": 3}

Each new line is tested against every rule in order; on a match (and no
ignore-pattern match) a finding is appended. If the rule's score >=
alert_at (default 5), a JSON alert file lands in alerts/.

Layout under root:
    events/            input event files (top level; *.log / *.txt, plus
                       extensionless files)
    findings.jsonl     appended findings: {ts, file, rule, score, line}
    findings.jsonl.1..5  rotated archives
    alerts/            one JSON file per alert-worthy event
    state/offsets.json per-file byte offsets (no re-reads, no losses)
    state/scores.json  cumulative {"rule": total_score}
    state/heartbeat.json

Stdlib only. No network.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROTATE_BYTES = 128 * 1024
ROTATE_LINES = 400
ROTATE_KEEP = 5
HEARTBEAT_INTERVAL_S = 60
DEFAULT_ALERT_AT = 5

DEFAULT_RULES = [
    {"name": "ssh_auth_fail", "pattern": r"Failed password|authentication failure",
     "score": 3, "ignore": [], "alert_at": 3},
    {"name": "sudo_fail", "pattern": r"sudo:.*authentication failure|sudo:.*incorrect password",
     "score": 3, "ignore": [], "alert_at": 3},
    {"name": "su_attempt", "pattern": r"\bsu:\s|su\[",
     "score": 2, "ignore": [], "alert_at": 4},
    {"name": "selinux_denied", "pattern": r"avc:\s+denied",
     "score": 2, "ignore": [], "alert_at": 6},
    {"name": "oom_killed", "pattern": r"out of memory|Killed process",
     "score": 2, "ignore": [], "alert_at": 6},
    {"name": "segfault", "pattern": r"segfault|Segmentation fault",
     "score": 1, "ignore": [], "alert_at": 8},
]


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def compile_rules(raw_rules) -> list:
    compiled = []
    for r in raw_rules:
        if not isinstance(r, dict) or not r.get("name") or not r.get("pattern"):
            raise ValueError(f"bad rule: {r!r}")
        compiled.append({
            "name": str(r["name"]),
            "pattern": re.compile(str(r["pattern"])),
            "score": int(r.get("score", 1)),
            "ignore": [re.compile(p) for p in r.get("ignore", []) or []],
            "alert_at": int(r.get("alert_at", DEFAULT_ALERT_AT)),
        })
    return compiled


class Sensor:
    def __init__(self, root: Path, rules: list):
        self.root = root
        self.rules = rules
        self.events = root / "events"
        self.alerts = root / "alerts"
        self.state_dir = root / "state"
        self.findings = root / "findings.jsonl"
        self.offsets_path = self.state_dir / "offsets.json"
        self.scores_path = self.state_dir / "scores.json"
        self.hb_path = self.state_dir / "heartbeat.json"
        for d in (self.events, self.alerts, self.state_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.counters = {"lines": 0, "findings": 0, "alerts": 0,
                         "started_at": time.time()}
        self._last_hb = 0.0

    # -- state ---------------------------------------------------------
    def load_json(self, path: Path, default):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, type(default)) else default
        except (OSError, ValueError):
            return default

    def save_json(self, path: Path, obj) -> None:
        atomic_write(path, json.dumps(obj, indent=2) + "\n")

    # -- rotation ------------------------------------------------------
    def rotate_findings(self) -> None:
        """hexshield rule: >128KB or >400 lines -> archive copy, keep 400."""
        try:
            size = self.findings.stat().st_size
        except OSError:
            return
        nlines = 0
        if size > 0:
            with open(self.findings, "r", encoding="utf-8") as f:
                for _ in f:
                    nlines += 1
        if size <= ROTATE_BYTES and nlines <= ROTATE_LINES:
            return
        for i in range(ROTATE_KEEP - 1, 0, -1):
            older = self.root / f"findings.jsonl.{i}"
            newer = self.root / f"findings.jsonl.{i + 1}"
            if older.exists():
                os.replace(older, newer)
        archive = self.root / "findings.jsonl.1"
        with open(self.findings, "r", encoding="utf-8") as f:
            lines = f.readlines()
        atomic_write(archive, "".join(lines))
        atomic_write(self.findings, "".join(lines[-ROTATE_LINES:]))

    # -- core ----------------------------------------------------------
    def heartbeat(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_hb >= HEARTBEAT_INTERVAL_S:
            self._last_hb = now
            self.save_json(self.hb_path, {"ts": now, "pid": os.getpid(),
                                          "ok": True, **self.counters})

    def scan_line(self, line: str, src: str) -> list:
        """Return findings for one line: [(rule_name, score, alert_at)]."""
        hits = []
        for rule in self.rules:
            if not rule["pattern"].search(line):
                continue
            if any(p.search(line) for p in rule["ignore"]):
                continue
            hits.append((rule["name"], rule["score"], rule["alert_at"]))
        return hits

    def cycle(self) -> dict:
        outcomes = {"files": 0, "lines": 0, "findings": 0, "alerts": 0}
        offsets = self.load_json(self.offsets_path, {})
        scores = self.load_json(self.scores_path, {})
        for path in sorted(self.events.glob("*")):
            if not path.is_file():
                continue
            if path.suffix not in (".log", ".txt", ""):
                continue
            outcomes["files"] += 1
            key = path.name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            off = offsets.get(key, 0)
            if off > size:  # file was truncated/rotated: re-read from start
                off = 0
            if off == size:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(off)
                for line in f:
                    outcomes["lines"] += 1
                    self.counters["lines"] += 1
                    line = line.rstrip("\n")
                    for name, score, alert_at in self.scan_line(line, key):
                        finding = {"ts": time.time(), "file": key,
                                   "rule": name, "score": score,
                                   "line": line[:500]}
                        with open(self.findings, "a",
                                  encoding="utf-8") as jf:
                            jf.write(json.dumps(finding) + "\n")
                        outcomes["findings"] += 1
                        self.counters["findings"] += 1
                        scores[name] = scores.get(name, 0) + score
                        if score >= alert_at:
                            alert = dict(finding)
                            alert["cumulative"] = scores[name]
                            # ms timestamp: two cycles can share a second
                            aname = (f"{int(time.time() * 1000)}_{name}_"
                                     f"{outcomes['alerts']}.json")
                            atomic_write(self.alerts / aname,
                                         json.dumps(alert, indent=2) + "\n")
                            outcomes["alerts"] += 1
                            self.counters["alerts"] += 1
                offsets[key] = f.tell()
        self.save_json(self.offsets_path, offsets)
        self.save_json(self.scores_path, scores)
        self.rotate_findings()
        self.heartbeat()
        return outcomes


def load_rules(sensors_path: str | None) -> list:
    if sensors_path:
        raw = json.loads(Path(sensors_path).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("sensors.json must be a JSON list of rules")
        return compile_rules(raw)
    return compile_rules(DEFAULT_RULES)


def acquire_lock(lock_path: Path) -> bool:
    """Pidfile lock with kill -0 stale detection (same as watchdog/inbox)."""
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
    ap.add_argument("--root", default=os.environ.get("VEGA_ROOT", "."))
    ap.add_argument("--loop", type=int, default=0)
    ap.add_argument("--sensors", default=None)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    try:
        rules = load_rules(args.sensors)
    except (OSError, ValueError) as e:
        print(f"[vega-sensor] bad sensor config: {e}", file=sys.stderr)
        return 2

    root = Path(args.root)
    sensor = Sensor(root, rules)
    sensor.heartbeat(force=True)

    if args.loop > 0:
        lock_path = root / "run" / "sensor.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if not acquire_lock(lock_path):
            print("[vega-sensor] another instance holds the lock; exiting",
                  file=sys.stderr)
            return 1
        try:
            while True:
                try:
                    out = sensor.cycle()
                    if out["findings"]:
                        print(f"[vega-sensor] {out}", flush=True)
                except Exception as e:  # noqa: BLE001 - sensor must not die
                    print(f"[vega-sensor] cycle failed: {e}",
                          file=sys.stderr)
                    time.sleep(5)
                time.sleep(args.loop)
        except KeyboardInterrupt:
            pass
        finally:
            release_lock(lock_path)
        return 0

    out = sensor.cycle()
    print(f"[vega-sensor] {out}")
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

    print("[vega-sensor self-test]")
    rules = compile_rules([
        {"name": "auth_fail", "pattern": r"Failed password",
         "score": 3, "ignore": [r"from 10\.0\.0\."], "alert_at": 3},
        {"name": "su_note", "pattern": r"su: pam_unix",
         "score": 2, "ignore": [], "alert_at": 5},
    ])
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        s = Sensor(root, rules)
        (root / "events" / "auth.log").write_text(
            "Oct 1 09:00:01 server sshd[1]: Accepted key for elliot\n"
            "Oct 1 09:01:12 server sshd[2]: Failed password for root from 1.2.3.4\n"
            "Oct 1 09:02:33 server sshd[3]: Failed password for root from 10.0.0.9\n"
            "Oct 1 09:03:44 server su: pam_unix(su:auth): authentication failure\n")

        out = s.cycle()
        check("scanned 1 file, 4 lines", out["files"] == 1 and out["lines"] == 4)
        check("2 findings (accepted-key + ignored-subnet lines excluded)",
              out["findings"] == 2)
        check("1 alert (auth_fail score 3 >= alert_at 3)",
              out["alerts"] == 1)
        findings = (root / "findings.jsonl").read_text().strip().splitlines()
        check("findings.jsonl has 2 lines", len(findings) == 2)
        f0 = json.loads(findings[0])
        check("finding carries rule/file/score",
              f0["rule"] == "auth_fail" and f0["file"] == "auth.log"
              and f0["score"] == 3 and "ts" in f0)
        scores = json.loads((root / "state" / "scores.json").read_text())
        check("cumulative scores", scores == {"auth_fail": 3, "su_note": 2})
        alerts = list((root / "alerts").glob("*.json"))
        check("alert file written", len(alerts) == 1)
        check("alert carries cumulative",
              json.loads(alerts[0].read_text())["cumulative"] == 3)

        # offsets: second cycle reads nothing new
        out2 = s.cycle()
        check("rerun reads zero new lines",
              out2["lines"] == 0 and out2["findings"] == 0)

        # append more lines: only the new ones are scored
        with open(root / "events" / "auth.log", "a") as f:
            f.write("Oct 1 09:05:55 server sshd[4]: Failed password for admin from 9.9.9.9\n")
        out3 = s.cycle()
        check("append: exactly 1 new line scored",
              out3["lines"] == 1 and out3["findings"] == 1)
        scores3 = json.loads((root / "state" / "scores.json").read_text())
        check("scores accumulate across cycles", scores3["auth_fail"] == 6)

        # truncated file: offsets reset, content re-read
        (root / "events" / "auth.log").write_text(
            "Oct 1 10:00:00 server sshd[9]: Failed password for root from 8.8.8.8\n")
        out4 = s.cycle()
        check("truncated file re-read from start", out4["lines"] == 1)

        # rotation: 500 finding lines -> archive + keep 400
        with open(root / "findings.jsonl", "w") as f:
            for i in range(500):
                f.write(json.dumps({"ts": i, "n": i}) + "\n")
        s.rotate_findings()
        kept = (root / "findings.jsonl").read_text().strip().splitlines()
        check("rotation keeps 400 lines", len(kept) == 400)
        check("rotation keeps the newest 400",
              json.loads(kept[0])["n"] == 100)
        check("rotation archives full copy",
              len((root / "findings.jsonl.1").read_text().strip().splitlines()) == 500)
        check("no .tmp files survive", list(root.rglob("*.tmp")) == [])

        hb = json.loads((root / "state" / "heartbeat.json").read_text())
        check("heartbeat advances", abs(hb["ts"] - time.time()) < 60 and hb["ok"])

    # bad rule config fails loudly, not silently
    try:
        compile_rules([{"name": "x"}])
        check("malformed rule raises", False)
    except ValueError:
        check("malformed rule raises", True)

    print(f"[vega-sensor self-test] {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
