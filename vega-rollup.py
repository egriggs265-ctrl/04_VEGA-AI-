#!/usr/bin/env python3
"""vega-rollup.py — daily digest from run logs.

Scans a directory of per-run log files, extracts exact counts from explicit
markers, and prints (and optionally appends) a markdown digest in the
established WORKLOG format.

Lineage: the Hexwatch monolith's rollup thread (identity + file inventory +
log tails) + sentinel-report (bundle health/test/log-tail into a timestamped
report). Improvement: explicit marker protocol instead of free-text
grep-counting, so the numbers are exact and fixture-testable.

Marker protocol (one per line, anywhere in a log file):
    [OK]     a check/pass event
    [FAIL]   a failed check
    [ERROR]  an error line
    [ITEM]   one item actioned/secured/followed-up
    [SKIP]   a deliberately skipped step (counted, not an error)

Usage:
    vega-rollup.py --log-dir ./hidden_files --date 2026-09-30
    vega-rollup.py --log-dir ./hidden_files --date 2026-09-30 --append WORKLOG.md
    vega-rollup.py --self-test

Files scanned: *<date>*.md, *<date>*.log and *<date>*.txt under --log-dir
(top level only).
Output digest:
    ## Rollup 2026-09-30
    - log files scanned: 3
    - [OK]: 12 | [FAIL]: 1 | [ERROR]: 2 | [ITEM]: 7 | [SKIP]: 0

Stdlib only. No network.
"""
import argparse
import re
import sys
import time
from pathlib import Path

MARKERS = ("OK", "FAIL", "ERROR", "ITEM", "SKIP")
MARKER_RE = re.compile(r"\[(OK|FAIL|ERROR|ITEM|SKIP)\]")


def rollup(log_dir: Path, date: str):
    files = sorted(
        [p for p in log_dir.iterdir()
         if p.is_file() and date in p.name
         and p.suffix in (".md", ".log", ".txt")],
        key=lambda p: p.name,
    )
    counts = {m: 0 for m in MARKERS}
    per_file = {}
    for f in files:
        fc = {m: 0 for m in MARKERS}
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            per_file[f.name] = {"unreadable": True, **fc}
            continue
        for line in text.splitlines():
            for m in MARKER_RE.findall(line):
                counts[m] += 1
                fc[m] += 1
        per_file[f.name] = fc
    return {
        "date": date,
        "ts": time.time(),
        "log_dir": str(log_dir),
        "files_scanned": len(files),
        "counts": counts,
        "per_file": per_file,
    }


def digest_markdown(data: dict) -> str:
    c = data["counts"]
    lines = [
        f"## Rollup {data['date']}",
        f"- log files scanned: {data['files_scanned']}",
        "- " + " | ".join(f"[{m}]: {c[m]}" for m in MARKERS),
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", default=".")
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--append", default="")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    log_dir = Path(args.log_dir)
    if not log_dir.is_dir():
        print(f"[vega-rollup] not a directory: {log_dir}", file=sys.stderr)
        return 2
    data = rollup(log_dir, args.date)
    digest = digest_markdown(data)
    print(digest, end="")
    if args.append:
        with open(args.append, "a", encoding="utf-8") as f:
            f.write("\n" + digest)
        print(f"[vega-rollup] appended to {args.append}")
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

    print("[vega-rollup self-test]")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "run-2026-09-30-0800.md").write_text(
            "[OK] sweep done\n[OK] email watch\n[ITEM] secured nothing bundt\n"
            "[ERROR] ko-fi 403\n[SKIP] sephora parked\n")
        (root / "run-2026-09-30-0900.md").write_text(
            "[OK] payment watch\n[FAIL] redbubble upload\n[ERROR] timeout x\n"
            "[ERROR] timeout y\n[ITEM] tryazon confirmed\n[ITEM] krispy kreme\n")
        (root / "run-2026-09-29-0800.md").write_text(
            "[OK] should not be counted\n")
        (root / "notes.txt").write_text("no markers here\n")

        data = rollup(root, "2026-09-30")
        check("scans only matching date (2 files)", data["files_scanned"] == 2)
        check("OK count exact", data["counts"]["OK"] == 3)
        check("FAIL count exact", data["counts"]["FAIL"] == 1)
        check("ERROR count exact", data["counts"]["ERROR"] == 3)
        check("ITEM count exact", data["counts"]["ITEM"] == 3)
        check("SKIP count exact", data["counts"]["SKIP"] == 1)
        pf = data["per_file"]["run-2026-09-30-0900.md"]
        check("per-file breakdown exact",
              pf["OK"] == 1 and pf["FAIL"] == 1 and pf["ERROR"] == 2
              and pf["ITEM"] == 2 and pf["SKIP"] == 0)
        digest = digest_markdown(data)
        check("digest has date header", "## Rollup 2026-09-30" in digest)
        check("digest has all markers",
              all(f"[{m}]:" in digest for m in MARKERS))

        wl = root / "WORKLOG.md"
        wl.write_text("# Worklog\n")
        # exercise the --append path directly
        with open(wl, "a", encoding="utf-8") as f:
            f.write("\n" + digest)
        content = wl.read_text()
        check("append adds digest", "## Rollup 2026-09-30" in content
              and content.count("## Rollup") == 1)

        empty = rollup(root, "2099-01-01")
        check("no files -> zero counts",
              empty["files_scanned"] == 0
              and all(v == 0 for v in empty["counts"].values()))

    print(f"[vega-rollup self-test] {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
